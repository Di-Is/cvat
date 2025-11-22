#!/usr/bin/env python3
"""
Convert a SAM2 image encoder ONNX model to FP16.

This uses `onnxconverter_common.float16.convert_float_to_float16` to
convert all float tensors in the graph to float16 while keeping the
input/output types as float32 for easier integration.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import onnx
from onnxconverter_common import float16


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert SAM2 image encoder ONNX to FP16.")
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("experiments/data/sam2_image_encoder.onnx"),
        help="Path to the float32 ONNX model.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("experiments/data/sam2_image_encoder_fp16.onnx"),
        help="Path to the output float16 ONNX model.",
    )
    args = parser.parse_args()

    model = onnx.load(args.input.as_posix())
    model_fp16 = float16.convert_float_to_float16(
        model,
        keep_io_types=True,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    onnx.save(model_fp16, args.output.as_posix())
    print(f"Converted FP32 -> FP16: {args.input} -> {args.output}")


if __name__ == "__main__":
    main()

