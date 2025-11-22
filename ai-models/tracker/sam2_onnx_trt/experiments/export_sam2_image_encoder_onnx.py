#!/usr/bin/env python3
"""
Export the SAM2 image encoder to ONNX.

This script is inspired by the public notebook:
https://gist.github.com/jeremyfix/178eed7d69a33d7b5f062156d3870aa7

It loads a SAM2 backbone (SAM2Base) and exposes only the image encoder
path as a small wrapper module suitable for ONNX export.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Dict

import torch

from sam2.build_sam import build_sam2_hf
from sam2.modeling.sam2_base import SAM2Base


class SAM2ImageEncoder(torch.nn.Module):
    """Wrapper module exposing only the SAM2 image encoder.

    The forward pass mirrors the logic used in SAM2Base + SAM2VideoPredictor
    to prepare backbone features for the mask decoder, but restricted to the
    image encoder part. It returns three feature maps:

    - high_res_feats_0: highest-resolution feature map
    - high_res_feats_1: second highest-resolution feature map
    - image_embed: lowest-resolution image embedding
    """

    def __init__(self, sam_model: SAM2Base) -> None:
        super().__init__()
        self.model = sam_model
        self.image_encoder = sam_model.image_encoder
        self.no_mem_embed = sam_model.no_mem_embed

    def forward(self, x: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        """Run SAM2 image encoder and return three feature maps."""
        backbone_out = self.image_encoder(x)

        # Precompute projected level 0 and 1 features as in SAM2VideoPredictorVOS.
        backbone_out["backbone_fpn"][0] = self.model.sam_mask_decoder.conv_s0(
            backbone_out["backbone_fpn"][0]
        )
        backbone_out["backbone_fpn"][1] = self.model.sam_mask_decoder.conv_s1(
            backbone_out["backbone_fpn"][1]
        )

        feature_maps = backbone_out["backbone_fpn"][-self.model.num_feature_levels :]
        vision_pos_embeds = backbone_out["vision_pos_enc"][-self.model.num_feature_levels :]

        feat_sizes = [(t.shape[-2], t.shape[-1]) for t in vision_pos_embeds]

        # Flatten NxCxHxW to HWxNxC as in `_prepare_backbone_features`.
        vision_feats = [t.flatten(2).permute(2, 0, 1) for t in feature_maps]
        vision_pos_embeds = [t.flatten(2).permute(2, 0, 1) for t in vision_pos_embeds]

        # Add no-memory embed to the coarsest level.
        vision_feats[-1] = vision_feats[-1] + self.no_mem_embed

        # Convert back to [B, C, H, W] for each feature level.
        feats = [
            feat.permute(1, 2, 0).reshape(1, -1, *feat_size)
            for feat, feat_size in zip(vision_feats[::-1], feat_sizes[::-1])
        ][::-1]

        # Return three feature maps: high_res_0, high_res_1, image_embed.
        return feats[0], feats[1], feats[2]


def _build_model(model_id: str, device: str) -> SAM2Base:
    """Instantiate a SAM2 backbone suitable for ONNX export."""
    model = build_sam2_hf(
        model_id,
        device=device,
        mode="eval",
        hydra_overrides_extra=["++model.compile_image_encoder=false"],
    )
    model.eval()
    return model


def _build_dynamic_axes(output_names: List[str]) -> Dict[str, Dict[int, str]]:
    """Build dynamic axes mapping for ONNX export (dynamic batch size)."""
    axes: Dict[str, Dict[int, str]] = {"image": {0: "batch"}}
    for name in output_names:
        axes[name] = {0: "batch"}
    return axes


def main() -> None:
    parser = argparse.ArgumentParser(description="Export SAM2 image encoder to ONNX.")
    parser.add_argument(
        "--model-id",
        default="facebook/sam2.1-hiera-small",
        help="Hugging Face model id for the SAM2 backbone.",
    )
    parser.add_argument(
        "--device",
        default="cpu",
        help="Torch device for export (e.g. 'cuda' or 'cpu').",
    )
    parser.add_argument(
        "--opset",
        type=int,
        default=17,
        help="ONNX opset version to use for export.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=1,
        help="Dummy batch size used during export.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Path to the output ONNX file.",
    )
    args = parser.parse_args()

    device = torch.device(args.device)
    sam_model = _build_model(args.model_id, device=args.device)
    image_size: int = int(getattr(sam_model, "image_size"))

    encoder = SAM2ImageEncoder(sam_model).to(device)
    encoder.eval()

    dummy_input = torch.randn(
        args.batch_size,
        3,
        image_size,
        image_size,
        device=device,
        dtype=torch.float32,
    )

    output_names = ["high_res_feats_0", "high_res_feats_1", "image_embed"]
    dynamic_axes = _build_dynamic_axes(output_names)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    export_kwargs = dict(
        export_params=True,
        opset_version=args.opset,
        do_constant_folding=True,
        input_names=["image"],
        output_names=output_names,
        dynamic_axes=dynamic_axes,
    )
    # Prefer the legacy exporter when available to avoid
    # current limitations in the dynamo-based exporter.
    import inspect

    if "dynamo" in inspect.signature(torch.onnx.export).parameters:
        export_kwargs["dynamo"] = False

    torch.onnx.export(
        encoder,
        dummy_input,
        args.output.as_posix(),
        **export_kwargs,
    )
    print(f"Exported SAM2 image encoder to {args.output}")


if __name__ == "__main__":
    main()
