from __future__ import annotations

import io
from dataclasses import dataclass
import logging
from typing import Iterable

import PIL.Image
import torch
import torchvision.transforms.functional as F
from torchvision.io import ImageReadMode, decode_image


@dataclass(frozen=True)
class FastPreprocessConfig:
    mean: Iterable[float]
    std: Iterable[float]
    image_size: int
    device: torch.device
    channels_last: bool = False


def _extract_encoded_bytes(image: PIL.Image.Image) -> bytes | None:
    logger = logging.getLogger("cvat.ai_models.tracker.sam2")
    # Log re-encode fallback only once to avoid noisy output.
    fallback_logged = getattr(_extract_encoded_bytes, "_fallback_logged", False)

    info = getattr(image, "info", None)
    if info and "_encoded_bytes" in info:
        encoded = info["_encoded_bytes"]
        if isinstance(encoded, (bytes, bytearray, memoryview)):
            return bytes(encoded)
    fp = getattr(image, "fp", None)
    if fp is not None and not isinstance(fp, io.BytesIO):
        try:
            pos = fp.tell()
            fp.seek(0)
            data = fp.read()
            fp.seek(pos)
            if data:
                return data
        except Exception:
            pass
    if isinstance(fp, io.BytesIO):
        return fp.getbuffer().tobytes()
    try:
        buffer = io.BytesIO()
        image.save(buffer, format=image.format or "PNG")
        # Skip warning for synthetic images (e.g. PIL.Image.new) that have no source bytes.
        if not fallback_logged and (fp is not None or image.format):
            logger.warning(
                "SAM2 fast preprocess: re-encoding image because encoded bytes were missing; "
                "ensure _encoded_bytes is set to avoid extra CPU work."
            )
            setattr(_extract_encoded_bytes, "_fallback_logged", True)
        return buffer.getvalue()
    except Exception:
        # As a last resort, fall back to the caller.
        return None
    return None


def convert_rgb_image(
    image: PIL.Image.Image, *, config: FastPreprocessConfig, target_dtype: torch.dtype
) -> torch.Tensor:
    encoded = _extract_encoded_bytes(image)
    if encoded is None:
        raise RuntimeError("encoded image bytes unavailable")

    # Use frombuffer to avoid an extra copy before decode; `decode_image` copies into its
    # output tensor anyway.
    encoded_tensor = torch.frombuffer(memoryview(encoded), dtype=torch.uint8)
    tensor = decode_image(
        encoded_tensor,
        mode=ImageReadMode.RGB,
    ).to(device=config.device, non_blocking=True)

    tensor = tensor.unsqueeze(0).float().div_(255.0)
    tensor = F.resize(
        tensor,
        [config.image_size, config.image_size],
        interpolation=F.InterpolationMode.BICUBIC,
        antialias=True,
    )
    tensor = F.normalize(tensor, mean=config.mean, std=config.std)
    tensor = tensor.to(dtype=target_dtype)
    if config.channels_last:
        tensor = tensor.to(memory_format=torch.channels_last)
    return tensor
