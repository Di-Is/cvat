import collections
import dataclasses
import os
from pathlib import Path
from typing import Optional
import sys

import cvat_sdk.auto_annotation as cvataa
import numpy as np
import PIL.Image
import torch
from cvat_sdk.masks import encode_mask
from sam2.sam2_image_predictor import SAM2ImagePredictor

_MODULE_DIR = Path(__file__).resolve().parent
if str(_MODULE_DIR) not in sys.path:
    sys.path.append(str(_MODULE_DIR))

from mask_utils import mask_rows_from_bounds

_CACHE_SIZE_ENV = "SAM2_INTERACTOR_CACHE_FRAMES"


@dataclasses.dataclass(frozen=True)
class _PredictionResult:
    mask_rle: tuple[int, ...]
    bounds: Optional[tuple[int, int, int, int]]
    mask_rows: tuple[tuple[int, ...], ...]
    low_res_mask_input: Optional[np.ndarray]


@dataclasses.dataclass(frozen=True)
class _FrameCacheKey:
    task_id: int
    frame_index: int
    job_id: Optional[int]


@dataclasses.dataclass
class _CachedFrameState:
    image_embed: torch.Tensor
    high_res_feats: tuple[torch.Tensor, ...]
    orig_hw: tuple[tuple[int, int], ...]
    mask_input: Optional[np.ndarray]


class _Sam2Interactor:
    spec = cvataa.InteractorFunctionSpec(
        min_pos_points=1,
        min_neg_points=0,
        startswith_box=True,
        startswith_box_optional=True,
        help_message="Place positive/negative points or draw a box to guide SAM2.1.",
        animated_gif="",
        version=2,
    )

    def __init__(
        self,
        *,
        model_id: str,
        device: Optional[str] = None,
        mask_threshold: float = 0.0,
        max_hole_area: float = 0.0,
        max_sprinkle_area: float = 0.0,
        **kwargs,
    ) -> None:
        predictor_kwargs = dict(
            mask_threshold=mask_threshold,
            max_hole_area=max_hole_area,
            max_sprinkle_area=max_sprinkle_area,
            **kwargs,
        )
        if device:
            predictor_kwargs["device"] = device
        self._predictor = SAM2ImagePredictor.from_pretrained(
            model_id,
            **predictor_kwargs,
        )
        self._device = self._predictor.device
        self._frame_cache_size = self._resolve_cache_size()
        self._frame_cache: collections.OrderedDict[_FrameCacheKey, _CachedFrameState] = (
            collections.OrderedDict()
        )

        if self._device.type == "cuda":
            torch.set_autocast_enabled(True)
            torch.set_autocast_gpu_dtype(torch.bfloat16)
            if torch.cuda.get_device_properties(self._device).major >= 8:
                torch.backends.cuda.matmul.allow_tf32 = True
                torch.backends.cudnn.allow_tf32 = True

    def interact(
        self,
        context: cvataa.InteractorFunctionContext,
        image: PIL.Image.Image,
        prompt: cvataa.InteractionPrompt,
    ) -> cvataa.MaskPrediction:
        rgb_image = image.convert("RGB")
        frame_key = self._make_cache_key(context)
        cached_state = self._frame_cache.get(frame_key)

        if cached_state is None:
            self._predictor.set_image(rgb_image)
            cached_state = self._capture_frame_state()
            self._frame_cache[frame_key] = cached_state
            self._evict_unused_frames()
        else:
            self._restore_cached_state(cached_state)

        result = self._predict(prompt, mask_input=cached_state.mask_input)
        cached_state.mask_input = result.low_res_mask_input
        self._frame_cache.move_to_end(frame_key)

        return cvataa.MaskPrediction(
            mask=result.mask_rows,
            mask_rle=result.mask_rle,
            bounds=result.bounds,
        )

    def _predict(
        self,
        prompt: cvataa.InteractionPrompt,
        *,
        mask_input: Optional[np.ndarray],
    ) -> _PredictionResult:
        point_coords, point_labels = self._prepare_points(prompt)
        box = self._prepare_box(prompt.bounding_box)

        masks, ious, low_res_masks = self._predictor.predict(
            point_coords=point_coords,
            point_labels=point_labels,
            box=box,
            mask_input=mask_input,
            multimask_output=True,
            return_logits=False,
            normalize_coords=True,
        )

        best_idx = int(np.argmax(ious))
        best_mask_tensor = masks[best_idx]
        if isinstance(best_mask_tensor, torch.Tensor):
            best_mask_np = best_mask_tensor.detach().cpu().numpy()
        else:
            best_mask_np = np.asarray(best_mask_tensor)
        best_mask = (best_mask_np >= 0.5).astype(np.bool_)

        encoded_mask = encode_mask(best_mask)
        if len(encoded_mask) < 4:
            raise ValueError("Encoded SAM2 mask is missing bounding box metadata")
        mask_rle = tuple(int(v) for v in encoded_mask[:-4])
        bounds = tuple(int(v) for v in encoded_mask[-4:]) if encoded_mask else None

        mask_rows = mask_rows_from_bounds(best_mask, bounds)

        low_res_mask_input: Optional[np.ndarray] = None
        low_res_masks_np = np.asarray(low_res_masks)
        if low_res_masks_np.size:
            low_res_mask_input = low_res_masks_np[best_idx : best_idx + 1].astype(np.float32)

        return _PredictionResult(
            mask_rle=mask_rle,
            bounds=bounds,
            mask_rows=mask_rows,
            low_res_mask_input=low_res_mask_input,
        )

    @staticmethod
    def _resolve_cache_size() -> int:
        raw_value = os.getenv(_CACHE_SIZE_ENV)
        if raw_value is None:
            return 1
        try:
            parsed = int(raw_value)
        except ValueError:
            return 1
        return max(1, parsed)

    @staticmethod
    def _make_cache_key(context: cvataa.InteractorFunctionContext) -> _FrameCacheKey:
        return _FrameCacheKey(
            task_id=context.task_id,
            frame_index=context.frame_index,
            job_id=context.job_id,
        )

    def _capture_frame_state(self) -> _CachedFrameState:
        features = self._predictor._features
        orig_hw = self._predictor._orig_hw
        if features is None or orig_hw is None:
            raise RuntimeError("SAM2 predictor must be initialized before caching")
        return _CachedFrameState(
            image_embed=features["image_embed"],
            high_res_feats=tuple(features["high_res_feats"]),
            orig_hw=tuple((int(h), int(w)) for h, w in orig_hw),
            mask_input=None,
        )

    def _restore_cached_state(self, state: _CachedFrameState) -> None:
        self._predictor._features = {
            "image_embed": state.image_embed,
            "high_res_feats": list(state.high_res_feats),
        }
        self._predictor._orig_hw = [tuple(hw) for hw in state.orig_hw]
        self._predictor._is_image_set = True
        self._predictor._is_batch = False

    def _evict_unused_frames(self) -> None:
        while len(self._frame_cache) > self._frame_cache_size:
            self._frame_cache.popitem(last=False)

    @staticmethod
    def _prepare_points(
        prompt: cvataa.InteractionPrompt,
    ) -> tuple[Optional[np.ndarray], Optional[np.ndarray]]:
        coords: list[tuple[float, float]] = []
        labels: list[int] = []

        coords.extend(prompt.positive_points)
        labels.extend([1] * len(prompt.positive_points))

        coords.extend(prompt.negative_points)
        labels.extend([0] * len(prompt.negative_points))

        if not coords:
            return None, None

        return (
            np.asarray(coords, dtype=np.float32),
            np.asarray(labels, dtype=np.int32),
        )

    @staticmethod
    def _prepare_box(
        box: Optional[tuple[tuple[float, float], tuple[float, float]]]
    ) -> Optional[np.ndarray]:
        if box is None:
            return None
        (x_min, y_min), (x_max, y_max) = box
        return np.asarray([x_min, y_min, x_max, y_max], dtype=np.float32)


create = _Sam2Interactor
