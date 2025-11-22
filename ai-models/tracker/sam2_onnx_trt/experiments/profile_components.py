#!/usr/bin/env python3
"""
Experimental profiler for SAM2 tracker components.

This script runs the `_Sam2Tracker` implementation on a local directory of
frames and records per-call timings for the major SAM2 modules (image encoder,
memory encoder, memory attention, prompt encoder, mask decoder) as well as
high-level preprocess / track phases. It is intended for container-side
diagnostics and does not integrate with CVAT APIs.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import statistics
import sys
import time
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
import PIL.Image
import torch


def _load_tracker_module(func_path: Path):
    spec = importlib.util.spec_from_file_location("sam2_tracker_func", func_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to import tracker func module from {func_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@dataclass
class _SimpleShape:
    type: str
    points: list[float]


class _ComponentTimer:
    def __init__(self, device: torch.device):
        self._device = device
        self._records: dict[str, list[float]] = defaultdict(list)

    def _sync(self) -> None:
        if self._device.type == "cuda":
            torch.cuda.synchronize(self._device)

    @contextmanager
    def measure(self, label: str):
        self._sync()
        start = time.perf_counter()
        try:
            yield
        finally:
            self._sync()
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            self._records[label].append(elapsed_ms)

    def wrap_attr(self, obj: object, attr: str, label: str) -> None:
        original = getattr(obj, attr)

        def wrapped(*args, **kwargs):
            with self.measure(label):
                return original(*args, **kwargs)

        setattr(obj, attr, wrapped)

    def summary(self) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for label, samples in sorted(self._records.items()):
            count = len(samples)
            avg = statistics.fmean(samples) if samples else math.nan
            total = sum(samples)
            p95 = (
                float(np.percentile(samples, 95)) if samples else math.nan
            )  # numpy for stable percentile
            rows.append(
                {
                    "label": label,
                    "count": count,
                    "avg_ms": avg,
                    "total_ms": total,
                    "p95_ms": p95,
                }
            )
        return rows


def _build_shape(width: int, height: int, pad_ratio: float) -> _SimpleShape:
    pad_w = width * pad_ratio
    pad_h = height * pad_ratio
    left = pad_w
    top = pad_h
    right = width - pad_w
    bottom = height - pad_h
    points = [left, top, right, top, right, bottom, left, bottom]
    return _SimpleShape(type="polygon", points=points)


def _iter_frame_paths(frames_dir: Path, limit: int) -> list[Path]:
    paths = sorted(
        [
            path
            for path in frames_dir.iterdir()
            if path.suffix.lower() in {".jpg", ".jpeg", ".png"}
        ]
    )
    if not paths:
        raise RuntimeError(f"No frames were found under {frames_dir}")
    return paths[:limit] if limit > 0 else paths


def _summarize_rows(rows: Iterable[dict[str, object]]) -> str:
    if not rows:
        return "No samples collected."
    col_headers = ("Label", "Count", "Avg ms", "P95 ms", "Total ms")
    lines = [" | ".join(f"{header:>12}" for header in col_headers)]
    lines.append("-" * len(lines[0]))
    for row in rows:
        lines.append(
            " | ".join(
                [
                    f"{row['label']:>12}",
                    f"{row['count']:12d}",
                    f"{row['avg_ms']:12.3f}",
                    f"{row['p95_ms']:12.3f}",
                    f"{row['total_ms']:12.3f}",
                ]
            )
        )
    return "\n".join(lines)


@torch.inference_mode()
def _attach_encoded_bytes(image: PIL.Image.Image, path: Path) -> PIL.Image.Image:
    try:
        encoded = path.read_bytes()
    except Exception:
        return image
    if not hasattr(image, "info"):
        image.info = {}
    image.info["_encoded_bytes"] = encoded
    return image


def _run_profile(
    *,
    tracker,
    timer: _ComponentTimer,
    frames: list[Path],
    pad_ratio: float,
    preprocess_only: bool,
) -> None:
    first_image = _attach_encoded_bytes(PIL.Image.open(frames[0]), frames[0])
    dummy_shape = _build_shape(first_image.width, first_image.height, pad_ratio)

    if preprocess_only:
        for frame_path in frames:
            image = _attach_encoded_bytes(PIL.Image.open(frame_path), frame_path)
            with timer.measure("preprocess_total"):
                tracker.preprocess_image(None, image)
        return

    with timer.measure("preprocess_total"):
        pp_image = tracker.preprocess_image(None, first_image)
    state = tracker.init_tracking_state(None, pp_image, dummy_shape)

    for frame_idx, frame_path in enumerate(frames[1:], start=1):
        image = _attach_encoded_bytes(PIL.Image.open(frame_path), frame_path)
        with timer.measure("preprocess_total"):
            pp_image = tracker.preprocess_image(None, image)
        state.frame_idx += 1
        with timer.measure("track_step_total"):
            current_out = tracker._call_predictor(
                pp_image=pp_image,
                frame_idx=state.frame_idx,
                is_init_cond_frame=False,
                mask_inputs=None,
                output_dict=state.predictor_outputs,
            )
        non_cond = state.predictor_outputs["non_cond_frame_outputs"]
        non_cond[state.frame_idx] = current_out
        while len(non_cond) > tracker._predictor.num_maskmem:
            non_cond.popitem(last=False)


def main() -> None:
    parser = argparse.ArgumentParser(description="Profile SAM2 tracker component timings.")
    parser.add_argument(
        "--frames-dir",
        type=Path,
        required=True,
        help="Directory containing extracted frames (sorted lexicographically).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=128,
        help="Maximum number of frames to process (default: 128, set 0 for all).",
    )
    parser.add_argument(
        "--model-id",
        default="facebook/sam2.1-hiera-small",
        help="Model identifier passed to `_Sam2Tracker`.",
    )
    parser.add_argument(
        "--device",
        default="cuda",
        help="Torch device (e.g. 'cuda' or 'cpu').",
    )
    parser.add_argument(
        "--function-file",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "func.py",
        help="Path to ai-models/tracker/sam2/func.py (for importing `_Sam2Tracker`).",
    )
    parser.add_argument(
        "--vos-optimized",
        action="store_true",
        help="Force `_Sam2Tracker` instantiation with vos_optimized=True.",
    )
    parser.add_argument(
        "--pad-ratio",
        type=float,
        default=0.1,
        help="Fractional padding when constructing the dummy polygon (default: 0.1).",
    )
    parser.add_argument(
        "--allow-cudagraphs",
        action="store_true",
        help="Do not disable TorchInductor CUDA graphs (default: disable to avoid runtime errors).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional JSON file to store raw timing samples.",
    )
    parser.add_argument(
        "--profile-preprocess",
        action="store_true",
        help="Profile preprocess components (vision backbone). If neither this nor --profile-track-components is specified, both groups are profiled.",
    )
    parser.add_argument(
        "--profile-track-components",
        action="store_true",
        help="Profile tracker components (memory encoder/attention, prompt encoder, mask decoder).",
    )
    parser.add_argument(
        "--preprocess-only",
        action="store_true",
        help="Only run preprocess_image (skip tracking). Useful for measuring backbone throughput independently.",
    )
    args = parser.parse_args()

    module = _load_tracker_module(args.function_file)
    if not args.allow_cudagraphs:
        try:
            from torch._inductor import config as _inductor_config  # type: ignore[attr-defined]
        except Exception:
            _inductor_config = None
        if _inductor_config is not None:
            setattr(_inductor_config.triton, "cudagraphs", False)
            if getattr(_inductor_config, "use_cuda_graphs", None):
                _inductor_config.use_cuda_graphs = False

    tracker = module._Sam2Tracker(
        model_id=args.model_id,
        device=args.device,
        vos_optimized=args.vos_optimized,
    )

    # Default behavior mirrors previous version: profile everything unless flags restrict it.
    timer = _ComponentTimer(tracker._device)
    predictor = tracker._predictor
    profile_pre = args.profile_preprocess
    profile_track = args.profile_track_components
    if not (profile_pre or profile_track):
        profile_pre = profile_track = True
    if profile_pre:
        timer.wrap_attr(predictor, "forward_image", "vision_backbone")
    if profile_track:
        timer.wrap_attr(predictor.memory_encoder, "forward", "memory_encoder")
        timer.wrap_attr(predictor.memory_attention, "forward", "memory_attention")
        timer.wrap_attr(predictor.sam_prompt_encoder, "forward", "prompt_encoder")
        timer.wrap_attr(predictor.sam_mask_decoder, "forward", "mask_decoder")

    frames = _iter_frame_paths(args.frames_dir, args.limit)
    with torch.no_grad():
        _run_profile(
            tracker=tracker,
            timer=timer,
            frames=frames,
            pad_ratio=args.pad_ratio,
            preprocess_only=args.preprocess_only,
        )

    rows = timer.summary()
    print(_summarize_rows(rows))
    if args.output:
        payload = {"frames": len(frames), "samples": rows}
        args.output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"Wrote timing JSON to {args.output}")


if __name__ == "__main__":
    main()
