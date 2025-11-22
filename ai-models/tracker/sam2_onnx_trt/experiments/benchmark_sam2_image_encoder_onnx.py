#!/usr/bin/env python3
"""
Benchmark SAM2 image encoder: PyTorch vs ONNX.

This script measures per-frame latency of the SAM2 image encoder
implemented as:

- PyTorch: `SAM2ImageEncoder` wrapper (see export_sam2_image_encoder_onnx.py)
- ONNX:   `sam2_image_encoder.onnx` exported from the same wrapper

Input frames are taken from a directory of JPEG images, resized and
normalized in the same way as the tracker (`func.py`) does.
"""

from __future__ import annotations

import argparse
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Tuple

import numpy as np
import PIL.Image
import torch
import onnxruntime as ort
from sam2.build_sam import build_sam2_hf
from export_sam2_image_encoder_onnx import SAM2ImageEncoder


_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)


@dataclass
class TimingStats:
    label: str
    count: int
    avg_ms: float
    p95_ms: float
    total_ms: float


def _iter_frames(frames_dir: Path, limit: int) -> List[Path]:
    paths = sorted(
        p
        for p in frames_dir.iterdir()
        if p.suffix.lower() in {".jpg", ".jpeg", ".png"}
    )
    if not paths:
        raise RuntimeError(f"No frames found under {frames_dir}")
    return paths[:limit] if limit > 0 else paths


def _preprocess_image(path: Path, image_size: int) -> np.ndarray:
    """Load and normalize image to NCHW float32, matching tracker preprocessing."""
    img = PIL.Image.open(path).convert("RGB")
    img = img.resize((image_size, image_size), PIL.Image.BILINEAR)
    arr = np.asarray(img, dtype=np.float32) / 255.0  # HWC, [0, 1]
    arr = (arr - _MEAN) / _STD
    arr = np.transpose(arr, (2, 0, 1))  # C, H, W
    return arr[None, ...]  # 1, C, H, W


def _summarize(label: str, samples_ms: Iterable[float]) -> TimingStats:
    samples = list(samples_ms)
    if not samples:
        return TimingStats(label=label, count=0, avg_ms=float("nan"), p95_ms=float("nan"), total_ms=0.0)
    count = len(samples)
    avg = statistics.fmean(samples)
    total = sum(samples)
    p95 = float(np.percentile(np.array(samples, dtype=np.float64), 95))
    return TimingStats(label=label, count=count, avg_ms=avg, p95_ms=p95, total_ms=total)


def _print_stats(stats: TimingStats) -> None:
    print(
        f"{stats.label:20s} | count={stats.count:4d} | "
        f"avg={stats.avg_ms:8.3f} ms | p95={stats.p95_ms:8.3f} ms | total={stats.total_ms:8.1f} ms"
    )


def _benchmark_torch(
    encoder: torch.nn.Module,
    frames: List[Path],
    image_size: int,
    device: torch.device,
    warmup: int,
) -> TimingStats:
    times: List[float] = []
    encoder.eval()
    use_cuda = device.type == "cuda"

    with torch.inference_mode():
        # Warmup
        for path in frames[:warmup]:
            arr = _preprocess_image(path, image_size)
            x = torch.from_numpy(arr).to(device)
            if use_cuda:
                torch.cuda.synchronize(device)
            _ = encoder(x)
            if use_cuda:
                torch.cuda.synchronize(device)

        # Timed runs
        for path in frames:
            arr = _preprocess_image(path, image_size)
            x = torch.from_numpy(arr).to(device)
            if use_cuda:
                torch.cuda.synchronize(device)
            start = time.perf_counter()
            _ = encoder(x)
            if use_cuda:
                torch.cuda.synchronize(device)
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            times.append(elapsed_ms)

    return _summarize("torch_encoder", times)


def _benchmark_onnx(
    onnx_path: Path,
    frames: List[Path],
    image_size: int,
    warmup: int,
) -> Tuple[TimingStats, str]:
    available = ort.get_available_providers()
    if "CUDAExecutionProvider" in available:
        providers: List[ort.ProviderType] = ["CUDAExecutionProvider", "CPUExecutionProvider"]
        provider_str = "CUDAExecutionProvider"
    else:
        providers = ["CPUExecutionProvider"]
        provider_str = "CPUExecutionProvider"

    session = ort.InferenceSession(onnx_path.as_posix(), providers=providers)
    input_name = session.get_inputs()[0].name

    times: List[float] = []

    # Warmup
    for path in frames[:warmup]:
        arr = _preprocess_image(path, image_size).astype(np.float32)
        _ = session.run(None, {input_name: arr})

    # Timed runs
    for path in frames:
        arr = _preprocess_image(path, image_size).astype(np.float32)
        start = time.perf_counter()
        _ = session.run(None, {input_name: arr})
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        times.append(elapsed_ms)

    return _summarize("onnx_encoder", times), provider_str


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark SAM2 image encoder (PyTorch vs ONNX).")
    parser.add_argument(
        "--frames-dir",
        type=Path,
        default=Path("experiments/data/XXXX225-02_frames_500"),
        help="Directory containing test frames.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=500,
        help="Maximum number of frames to use (0 for all).",
    )
    parser.add_argument(
        "--model-id",
        default="facebook/sam2.1-hiera-small",
        help="Hugging Face model id for loading the SAM2 backbone.",
    )
    parser.add_argument(
        "--onnx",
        type=Path,
        default=Path("experiments/data/sam2_image_encoder.onnx"),
        help="Path to the exported ONNX image encoder.",
    )
    parser.add_argument(
        "--onnx-fp16",
        type=Path,
        default=Path("experiments/data/sam2_image_encoder_fp16.onnx"),
        help="Optional path to the FP16 ONNX image encoder.",
    )
    parser.add_argument(
        "--device",
        default="cuda",
        help="Torch device for the PyTorch baseline (e.g. 'cuda' or 'cpu').",
    )
    parser.add_argument(
        "--warmup",
        type=int,
        default=10,
        help="Number of warmup frames for each backend.",
    )
    args = parser.parse_args()

    frames = _iter_frames(args.frames_dir, args.limit)
    print(f"Using {len(frames)} frames from {args.frames_dir}")

    # Load SAM2 backbone and wrap image encoder.
    device = torch.device(args.device)
    sam_model = build_sam2_hf(
        args.model_id,
        device=args.device,
        mode="eval",
        hydra_overrides_extra=["++model.compile_image_encoder=false"],
    )
    image_size: int = int(getattr(sam_model, "image_size"))
    encoder = SAM2ImageEncoder(sam_model).to(device)

    print(f"Image size: {image_size}, device: {device}")

    torch_stats = _benchmark_torch(
        encoder=encoder,
        frames=frames,
        image_size=image_size,
        device=device,
        warmup=args.warmup,
    )
    _print_stats(torch_stats)

    if not args.onnx.is_file():
        raise RuntimeError(f"ONNX model not found: {args.onnx}")

    onnx_stats, provider = _benchmark_onnx(
        onnx_path=args.onnx,
        frames=frames,
        image_size=image_size,
        warmup=args.warmup,
    )
    print(f"ONNX (FP32) provider: {provider}")
    _print_stats(onnx_stats)

    # Optional: TensorRT Execution Provider (FP16) benchmark using the same ONNX.
    available_providers = ort.get_available_providers()
    if "TensorrtExecutionProvider" in available_providers:
        trt_cache = Path("experiments/data/trt_cache")
        trt_cache.mkdir(parents=True, exist_ok=True)
        trt_providers: List[ort.ProviderType] = [
            (
                "TensorrtExecutionProvider",
                {
                    "trt_fp16_enable": "1",
                    "trt_engine_cache_enable": "1",
                    "trt_engine_cache_path": trt_cache.as_posix(),
                },
            ),
            "CUDAExecutionProvider",
            "CPUExecutionProvider",
        ]
        session = ort.InferenceSession(
            args.onnx.as_posix(),
            providers=trt_providers,
        )
        input_name = session.get_inputs()[0].name
        times: List[float] = []

        # Warmup
        for path in frames[: args.warmup]:
            arr = _preprocess_image(path, image_size).astype(np.float32)
            _ = session.run(None, {input_name: arr})

        # Timed runs
        for path in frames:
            arr = _preprocess_image(path, image_size).astype(np.float32)
            start = time.perf_counter()
            _ = session.run(None, {input_name: arr})
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            times.append(elapsed_ms)

        trt_stats = _summarize("onnx_trt_fp16_encoder", times)
        print("ONNX (TensorRT FP16) provider: TensorrtExecutionProvider")
        _print_stats(trt_stats)


if __name__ == "__main__":
    main()
