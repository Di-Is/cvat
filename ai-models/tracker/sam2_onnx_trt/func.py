# Copyright (C) CVAT.ai Corporation
#
# SPDX-License-Identifier: MIT

import collections
import contextlib
import contextvars
import dataclasses
import json
import logging
import os
import sys
import time
import types
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, TypedDict

import cv2
import numpy as np
import PIL.Image
import torch
import torchvision.transforms
from sam2.sam2_video_predictor import SAM2VideoPredictorVOS
from sam2.utils.misc import fill_holes_in_mask_scores

import cvat_sdk.auto_annotation as cvataa
from cvat_sdk.masks import decode_mask, encode_mask

_MODULE_DIR = Path(__file__).resolve().parent
_UTILS_DIR = _MODULE_DIR / "utils"
sys.path.append(str(_MODULE_DIR))
sys.path.append(str(_UTILS_DIR))
from preprocess import FastPreprocessConfig, convert_rgb_image  # type: ignore  # noqa: E402


def _env_flag(name: str) -> bool:
    value = os.getenv(name)
    if value is None:
        return False

    normalized = value.strip().lower()
    return normalized not in {"", "0", "false", "off", "no"}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


_SAM2_TRACKER_VERBOSE = _env_flag("SAM2_TRACKER_VERBOSE")
_SAM2_TRACKER_VOS_OPTIMIZED = _env_flag("SAM2_TRACKER_VOS_OPTIMIZED")
_SAM2_TRACKER_FAST_PREPROCESS = _env_flag("SAM2_TRACKER_FAST_PREPROCESS")
_SAM2_TRACKER_WARMUP_FRAMES = _env_int("SAM2_TRACKER_WARMUP_FRAMES", 1)
# Allow disabling triton cudagraphs via env for stability.
torch._inductor.config.triton.cudagraphs = _env_flag("SAM2_TRACKER_USE_CUDAGRAPHS")


class _CvatSAM2VideoPredictorVOS(SAM2VideoPredictorVOS):
    """Patch SAM2VideoPredictorVOS to recompile memory attention without cuda graphs.

    The upstream implementation enables cuda graphs inside torch.compile for
    `memory_attention.forward`, which currently crashes under TorchInductor +
    RTX 4080 in our tracker containers. Replacing the compile options with
    `max-autotune-no-cudagraphs` follows
    https://github.com/facebookresearch/sam2/issues/501#issuecomment-3540254359.
    """

    def _compile_all_components(self):
        print("Compiling all components for VOS setting (CVAT patched build).")
        self.memory_encoder.forward = torch.compile(
            self.memory_encoder.forward,
            mode="max-autotune",
            fullgraph=True,
            dynamic=False,
        )

        self.memory_attention.forward = torch.compile(
            self.memory_attention.forward,
            mode="max-autotune-no-cudagraphs",
            fullgraph=True,
            dynamic=True,
        )

        self.sam_prompt_encoder.forward = torch.compile(
            self.sam_prompt_encoder.forward,
            mode="max-autotune",
            fullgraph=True,
            dynamic=False,  # Accuracy regression on True
        )

        self.sam_mask_decoder.forward = torch.compile(
            self.sam_mask_decoder.forward,
            mode="max-autotune",
            fullgraph=True,
            dynamic=False,  # Accuracy regression on True
        )


class _PerfLogger:
    def __init__(self, device: torch.device, enabled: bool) -> None:
        self._device = device
        self._enabled = enabled
        self._logger = logging.getLogger("cvat.ai_models.tracker.sam2")
        if enabled:
            self._logger.setLevel(logging.INFO)
            self._logger.propagate = True
        self._context = contextvars.ContextVar("sam2_tracker_perf_context", default={})
        self._supports_gpu_timing = enabled and device.type == "cuda" and torch.cuda.is_available()
        self._start_ts: float | None = None
        self._frame_start_ts: dict[int, float] = {}

    @property
    def enabled(self) -> bool:
        return self._enabled

    @contextlib.contextmanager
    def scoped_context(self, **extra: object):
        """Attach contextual fields (e.g. frame index) to all nested phases."""
        if not self._enabled:
            yield
            return

        current = self._context.get()
        merged = {**current, **{k: v for k, v in extra.items() if v is not None}}
        token = self._context.set(merged)
        try:
            yield
        finally:
            self._context.reset(token)

    @contextlib.contextmanager
    def phase(self, phase: str, **extra: object):
        if not self._enabled:
            yield
            return
        if self._start_ts is None:
            self._start_ts = time.perf_counter()

        start = time.perf_counter()
        start_event = end_event = None
        if self._supports_gpu_timing:
            with torch.cuda.device(self._device):
                start_event = torch.cuda.Event(enable_timing=True)
                end_event = torch.cuda.Event(enable_timing=True)
                start_event.record()

        try:
            yield
        finally:
            end = time.perf_counter()
            gpu_ms: float | None = None
            if start_event is not None and end_event is not None:
                with torch.cuda.device(self._device):
                    end_event.record()
                    torch.cuda.synchronize()
                gpu_ms = start_event.elapsed_time(end_event)

            wall_ms = (end - start) * 1000
            payload: dict[str, object] = {
                "component": "sam2_tracker",
                "phase": phase,
                "wall_ms": round(wall_ms, 3),
            }

            current_context = self._context.get()
            payload.update(current_context)

            if self._start_ts is not None:
                t_ms = (end - self._start_ts) * 1000
                payload["t_ms"] = round(t_ms, 3)

            frame_idx = current_context.get("frame_idx")
            if isinstance(frame_idx, int):
                frame_start = self._frame_start_ts.get(frame_idx)
                if frame_start is None:
                    frame_start = end
                    self._frame_start_ts[frame_idx] = frame_start
                frame_t_ms = (end - frame_start) * 1000
                payload["frame_t_ms"] = round(frame_t_ms, 3)

            if gpu_ms is not None:
                payload["gpu_ms"] = round(gpu_ms, 3)

            for key, value in extra.items():
                if value is not None:
                    payload[key] = value

            self._logger.info("SAM2_TRACKER_LOG %s", json.dumps(payload, separators=(",", ":")))


@dataclasses.dataclass(frozen=True, kw_only=True)
class _PreprocessedImage:
    original_width: int
    original_height: int
    vision_feats: list[torch.Tensor]
    vision_pos_embeds: list[torch.Tensor]
    feat_sizes: list[tuple[int, int]]
    ready_event: torch.cuda.Event | None = None


class _PredictorOutputs(TypedDict):
    # We always keep 1 cond_frame_outputs and up to num_maskmem non_cond_frame_outputs.

    cond_frame_outputs: dict[int, dict]
    # We make this an OrderedDict to make popping old elements easier.
    non_cond_frame_outputs: collections.OrderedDict[int, dict]


def _install_predictor_perf_hooks(
    predictor: SAM2VideoPredictorVOS, perf_logger: _PerfLogger
) -> None:
    if not perf_logger.enabled:
        return
    if getattr(predictor, "_cvat_perf_hooks_installed", False):
        return

    def _wrap_method(
        instance: object,
        attribute: str,
        phase: str,
        extra_factory: Callable[..., dict[str, object]] | None = None,
    ) -> None:
        if not hasattr(instance, attribute):
            return
        sentinel_name = f"__cvat_perf_wrapped_{attribute}"
        if getattr(instance, sentinel_name, False):
            return
        original = getattr(instance, attribute)

        def _build_extra(args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, object]:
            if extra_factory is None:
                return {}
            try:
                extra = extra_factory(*args, **kwargs)
            except Exception:
                return {}
            if not extra:
                return {}
            return {k: v for k, v in extra.items() if v is not None}

        def wrapper(self, *args, **kwargs):
            extra = _build_extra(args, kwargs)
            with perf_logger.phase(phase, **extra):
                return original(*args, **kwargs)

        setattr(instance, attribute, types.MethodType(wrapper, instance))
        setattr(instance, sentinel_name, True)

    _wrap_method(
        predictor,
        "_prepare_memory_conditioned_features",
        "memory_conditioning",
        extra_factory=lambda frame_idx, is_init_cond_frame, *_args, **kwargs: {
            "is_init_cond_frame": is_init_cond_frame,
            "track_in_reverse": kwargs.get("track_in_reverse", False),
        },
    )
    _wrap_method(
        predictor,
        "_encode_new_memory",
        "memory_encode_prepare",
        extra_factory=lambda _current_feats,
        _feat_sizes,
        _pred_masks,
        _obj_logits,
        is_mask_from_pts: {"mask_from_points": is_mask_from_pts},
    )
    _wrap_method(
        predictor.memory_attention,
        "forward",
        "memory_attention",
    )
    _wrap_method(
        predictor.memory_encoder,
        "forward",
        "memory_encoder",
        extra_factory=lambda _pix, _mask, *_, **kwargs: {
            "skip_mask_sigmoid": kwargs.get("skip_mask_sigmoid")
        },
    )
    _wrap_method(
        predictor.sam_prompt_encoder,
        "forward",
        "sam_prompt_encoder",
        extra_factory=lambda points=None, boxes=None, masks=None: {
            "has_points": bool(
                points is not None
                and isinstance(points, tuple)
                and len(points) == 2
                and isinstance(points[1], torch.Tensor)
                and torch.any(points[1] >= 0).item()
            ),
            "has_masks": masks is not None,
        },
    )
    _wrap_method(
        predictor.sam_mask_decoder,
        "forward",
        "sam_mask_decoder",
        extra_factory=lambda *_, **kwargs: {"multimask_output": kwargs.get("multimask_output")},
    )

    predictor._cvat_perf_hooks_installed = True


@dataclasses.dataclass(kw_only=True)
class _TrackingState:
    frame_idx: int
    predictor_outputs: _PredictorOutputs


class _WarmupShapeContext(cvataa.TrackingFunctionShapeContext):
    def __init__(self, original_shape_type: str) -> None:
        self._original_shape_type = original_shape_type

    @property
    def original_shape_type(self) -> str:
        return self._original_shape_type


class _Sam2Tracker:
    def __init__(self, model_id: str, device: str = "cpu", **kwargs) -> None:
        self._device = torch.device(device)
        self._perf_logger = _PerfLogger(self._device, _SAM2_TRACKER_VERBOSE)
        requested_vos_opt = kwargs.pop("vos_optimized", None)
        if requested_vos_opt is None:
            self._vos_optimized = _SAM2_TRACKER_VOS_OPTIMIZED
        else:
            self._vos_optimized = bool(requested_vos_opt)

        if self._device.type == "cuda":
            torch.set_autocast_enabled(True)
            torch.set_autocast_gpu_dtype(torch.bfloat16)
            if torch.cuda.get_device_properties(self._device).major >= 8:
                torch.backends.cuda.matmul.allow_tf32 = True
                torch.backends.cudnn.allow_tf32 = True

        predictor_cls = (
            _CvatSAM2VideoPredictorVOS if self._vos_optimized else SAM2VideoPredictorVOS
        )
        self._predictor = predictor_cls.from_pretrained(
            model_id, device=self._device, vos_optimized=self._vos_optimized, **kwargs
        )
        _install_predictor_perf_hooks(self._predictor, self._perf_logger)
        if self._vos_optimized:
            logging.getLogger("cvat.ai_models.tracker.sam2").info(
                "SAM2 tracker started with vos_optimized=True (torch.compile enabled, patched memory attention)"
            )
        self._use_fast_preprocess = _SAM2_TRACKER_FAST_PREPROCESS
        self._use_async_preprocess = bool(int(os.getenv("SAM2_TRACKER_ASYNC_PREPROCESS", "0")))
        self._transform = torchvision.transforms.Compose(
            [
                torchvision.transforms.Resize(
                    (self._predictor.image_size, self._predictor.image_size)
                ),
                torchvision.transforms.ToTensor(),
                torchvision.transforms.Normalize(
                    mean=(0.485, 0.456, 0.406),
                    std=(0.229, 0.224, 0.225),
                ),
            ]
        )
        self._fast_preprocess_fallback_logged = False
        if self._use_fast_preprocess:
            self._fast_preprocess = FastPreprocessConfig(
                mean=(0.485, 0.456, 0.406),
                std=(0.229, 0.224, 0.225),
                image_size=self._predictor.image_size,
                device=self._device,
                channels_last=False,
            )
            logging.getLogger("cvat.ai_models.tracker.sam2").info(
                "SAM2 tracker fast preprocess enabled (GPU decode)"
            )
        self._preprocess_stream: torch.cuda.Stream | None = None
        if self._use_async_preprocess:
            with torch.cuda.device(self._device):
                self._preprocess_stream = torch.cuda.Stream()
            logging.getLogger("cvat.ai_models.tracker.sam2").info(
                "SAM2 tracker async preprocess enabled (separate CUDA stream)"
            )

        warmup_frames = max(_SAM2_TRACKER_WARMUP_FRAMES, 0)
        if warmup_frames:
            logging.getLogger("cvat.ai_models.tracker.sam2").info(
                "SAM2 tracker startup warmup starting (frames=%s)", warmup_frames
            )
            try:
                self._run_startup_warmup(num_frames=warmup_frames)
            except Exception as exc:
                logging.getLogger("cvat.ai_models.tracker.sam2").warning(
                    "SAM2 tracker warmup skipped due to error", exc_info=exc
                )

    spec = cvataa.TrackingFunctionSpec(supported_shape_types=["mask", "polygon"])

    @torch.inference_mode()
    def preprocess_image(
        self, context: cvataa.TrackingFunctionContext, image: PIL.Image.Image
    ) -> _PreprocessedImage:
        with self._perf_logger.phase("preprocess"):
            if self._use_fast_preprocess:
                try:
                    if self._use_async_preprocess and self._preprocess_stream is not None:
                        with torch.cuda.stream(self._preprocess_stream):
                            image_tensor = convert_rgb_image(
                                image,
                                config=self._fast_preprocess,
                                target_dtype=torch.float32,
                            )
                    else:
                        image_tensor = convert_rgb_image(
                            image,
                            config=self._fast_preprocess,
                            target_dtype=torch.float32,
                        )
                except RuntimeError as exc:
                    if not self._fast_preprocess_fallback_logged:
                        logging.getLogger("cvat.ai_models.tracker.sam2").warning(
                            "Fast preprocess fallback to CPU transforms",
                            exc_info=exc,
                        )
                        self._fast_preprocess_fallback_logged = True
                    image = image.convert("RGB")
                    image_tensor = (
                        self._transform(image)
                        .unsqueeze(0)
                        .to(device=self._device, non_blocking=self._use_async_preprocess)
                    )
            else:
                image = image.convert("RGB")
                if self._use_async_preprocess and self._preprocess_stream is not None:
                    with torch.cuda.stream(self._preprocess_stream):
                        image_tensor = (
                            self._transform(image)
                            .unsqueeze(0)
                            .to(device=self._device, non_blocking=True)
                        )
                else:
                    image_tensor = self._transform(image).unsqueeze(0).to(device=self._device)

            ready_event: torch.cuda.Event | None = None
            if self._use_async_preprocess and self._preprocess_stream is not None:
                with torch.cuda.stream(self._preprocess_stream):
                    backbone_out = self._predictor.forward_image(image_tensor)
                    ready_event = torch.cuda.Event()
                    ready_event.record(stream=self._preprocess_stream)
            else:
                backbone_out = self._predictor.forward_image(image_tensor)
            vision_feats = backbone_out["backbone_fpn"][-self._predictor.num_feature_levels :]
            vision_pos_embeds = backbone_out["vision_pos_enc"][
                -self._predictor.num_feature_levels :
            ]

            return _PreprocessedImage(
                original_width=image.width,
                original_height=image.height,
                vision_feats=[x.flatten(2).permute(2, 0, 1) for x in vision_feats],
                vision_pos_embeds=[x.flatten(2).permute(2, 0, 1) for x in vision_pos_embeds],
                feat_sizes=[(x.shape[-2], x.shape[-1]) for x in vision_pos_embeds],
                ready_event=ready_event,
            )

    def _call_predictor(self, *, pp_image: _PreprocessedImage, frame_idx: int, **kwargs) -> dict:
        with self._perf_logger.scoped_context(frame_idx=frame_idx):
            if self._use_async_preprocess and pp_image.ready_event is not None:
                # Ensure preprocess work on a dedicated stream finishes before tracking.
                torch.cuda.current_stream().wait_event(pp_image.ready_event)
            with self._perf_logger.phase("track_step"):
                if self._vos_optimized:
                    mark_step = getattr(
                        getattr(torch, "compiler", None), "cudagraph_mark_step_begin", None
                    )
                    if callable(mark_step):
                        mark_step()
                out = self._predictor.track_step(
                    current_vision_feats=pp_image.vision_feats,
                    current_vision_pos_embeds=pp_image.vision_pos_embeds,
                    feat_sizes=pp_image.feat_sizes,
                    point_inputs=None,
                    frame_idx=frame_idx,
                    num_frames=frame_idx + 1,
                    **kwargs,
                )

        return {
            "maskmem_features": out["maskmem_features"],
            "maskmem_pos_enc": out["maskmem_pos_enc"][-1:],
            "pred_masks": fill_holes_in_mask_scores(
                out["pred_masks"], self._predictor.fill_hole_area
            ),
            "obj_ptr": out["obj_ptr"],
        }

    def _shape_to_mask(
        self, pp_image: _PreprocessedImage, shape: cvataa.TrackableShape
    ) -> np.ndarray:
        with self._perf_logger.phase("shape_to_mask", shape_type=shape.type):
            if shape.type == "mask":
                return decode_mask(
                    shape.points,
                    image_width=pp_image.original_width,
                    image_height=pp_image.original_height,
                )

            if shape.type == "polygon":
                mask = np.zeros(
                    (pp_image.original_height, pp_image.original_width), dtype=np.uint8
                )
                points_array = np.array(shape.points, dtype=np.int32).reshape((-1, 2))
                cv2.fillPoly(mask, [points_array], 1)
                return mask

            assert False, f"unexpected shape type {shape.type!r}"

    @torch.inference_mode()
    def init_tracking_state(
        self,
        context: cvataa.TrackingFunctionShapeContext,
        pp_image: _PreprocessedImage,
        shape: cvataa.TrackableShape,
    ) -> _TrackingState:
        mask = torch.from_numpy(self._shape_to_mask(pp_image, shape))

        resized_mask = torch.nn.functional.interpolate(
            mask.float()[None, None],  # add batch and channel dimensions
            (self._predictor.image_size, self._predictor.image_size),
            mode="bilinear",
            align_corners=False,
        )
        resized_mask = (resized_mask >= 0.5).float().to(device=self._device)

        current_out = self._call_predictor(
            pp_image=pp_image,
            frame_idx=0,
            is_init_cond_frame=True,
            mask_inputs=resized_mask,
            output_dict={},
        )

        return _TrackingState(
            frame_idx=0,
            predictor_outputs={
                "cond_frame_outputs": {0: current_out},
                "non_cond_frame_outputs": collections.OrderedDict(),
            },
        )

    def _mask_to_shape(
        self, context: cvataa.TrackingFunctionShapeContext, mask: torch.Tensor
    ) -> cvataa.TrackableShape | None:
        with self._perf_logger.phase("mask_to_shape", target_type=context.original_shape_type):
            if context.original_shape_type == "mask":
                return cvataa.TrackableShape(type="mask", points=encode_mask(mask))

            if context.original_shape_type == "polygon":
                mask_np = np.asarray(mask, dtype=np.uint8)
                contours, _ = cv2.findContours(mask_np, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                if not contours:
                    return None

                largest_contour = max(contours, key=cv2.contourArea)
                approx_contour = cv2.approxPolyDP(largest_contour, epsilon=1.0, closed=True)
                if approx_contour.shape[0] < 3:
                    return None

                return cvataa.TrackableShape(
                    type="polygon", points=approx_contour.flatten().tolist()
                )

            assert False, f"unexpected shape type {context.original_shape_type!r}"

    @torch.inference_mode()
    def track(
        self,
        context: cvataa.TrackingFunctionShapeContext,
        pp_image: _PreprocessedImage,
        state: _TrackingState,
    ) -> cvataa.TrackableShape | None:
        state.frame_idx += 1

        current_out = self._call_predictor(
            pp_image=pp_image,
            frame_idx=state.frame_idx,
            is_init_cond_frame=False,
            mask_inputs=None,
            output_dict=state.predictor_outputs,
        )

        non_cond_frame_outputs = state.predictor_outputs["non_cond_frame_outputs"]
        non_cond_frame_outputs[state.frame_idx] = current_out

        # discard old outputs as the predictor uses up to num_maskmem elements
        while len(non_cond_frame_outputs) > self._predictor.num_maskmem:
            non_cond_frame_outputs.popitem(last=False)

        output_mask = (
            torch.nn.functional.interpolate(
                current_out["pred_masks"],
                size=(pp_image.original_height, pp_image.original_width),
                align_corners=False,
                mode="bilinear",
                antialias=True,
            )[0, 0]
            > 0
        )

        if output_mask.any():
            return self._mask_to_shape(context, output_mask.cpu())
        return None

    @torch.inference_mode()
    def _run_startup_warmup(self, *, num_frames: int) -> None:
        logger = logging.getLogger("cvat.ai_models.tracker.sam2")
        started_at = time.perf_counter()
        dummy_image = PIL.Image.new(
            "RGB",
            (self._predictor.image_size, self._predictor.image_size),
            color=(0, 0, 0),
        )
        context = _WarmupShapeContext("polygon")
        dummy_shape = cvataa.TrackableShape(
            type="polygon",
            points=[
                0.0,
                0.0,
                float(self._predictor.image_size),
                0.0,
                float(self._predictor.image_size),
                float(self._predictor.image_size),
                0.0,
                float(self._predictor.image_size),
            ],
        )

        def _compiler_disabled():
            # torch.compiler.disable currently does not support context-manager usage.
            # Keep the hook for future versions but default to a no-op.
            return contextlib.nullcontext()

        with _compiler_disabled():
            pp_image = self.preprocess_image(context, dummy_image)
        state = self.init_tracking_state(context, pp_image, dummy_shape)

        for frame_idx in range(max(num_frames, 1)):
            # Reuse the dummy frame to trigger torch.compile and stream setup.
            with self._perf_logger.scoped_context(phase="warmup", frame_idx=frame_idx):
                if frame_idx == 0:
                    with _compiler_disabled():
                        warmup_pp = self.preprocess_image(context, dummy_image)
                else:
                    with self._perf_logger.phase("warmup_preprocess"), _compiler_disabled():
                        warmup_pp = self.preprocess_image(context, dummy_image)

                with self._perf_logger.phase("warmup_track"):
                    self.track(context, warmup_pp, state)

        logger.info(
            "SAM2 tracker startup warmup finished (frames=%s, wall_ms=%.3f)",
            num_frames,
            (time.perf_counter() - started_at) * 1000,
        )

    @torch.inference_mode()
    def track_batch(
        self,
        batch: Sequence[tuple[cvataa.TrackingFunctionShapeContext, _TrackingState]],
        pp_image: _PreprocessedImage,
    ) -> list[cvataa.TrackableShape | None]:
        """Execute tracker steps for multiple tracked objects sharing the same frame.

        Default implementation simply calls `track()` for every entry, but the hook
        makes it possible to specialize batch execution (e.g. share CUDA streams).
        """
        results: list[cvataa.TrackableShape | None] = []
        for context, state in batch:
            results.append(self.track(context, pp_image, state))
        return results


create = _Sam2Tracker
