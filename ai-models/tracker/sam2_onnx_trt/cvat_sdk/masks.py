"""
Local stub for `cvat_sdk.masks`.

These helpers provide a simple encode/decode round‑trip for binary masks
that is sufficient for the SAM2 tracker implementation used in profiling.
They do **not** implement the full CVAT mask format.
"""

from __future__ import annotations

import numpy as np


def encode_mask(mask: np.ndarray) -> list[int]:
    """Encode a binary mask into a flat list of integers.

    The real CVAT implementation uses run‑length encoding; here we simply
    flatten the array to keep the implementation compact for local tests.
    """

    if not isinstance(mask, np.ndarray):
        raise TypeError("encode_mask expects a NumPy ndarray")
    return mask.astype(int).ravel().tolist()


def decode_mask(points: list[int], *, image_width: int, image_height: int) -> np.ndarray:
    """Decode a flat integer list back into a 2D mask.

    Parameters are compatible with the call sites in `func.py`, but the
    encoding format is intentionally simplified for offline profiling.
    """

    flat = np.asarray(points, dtype=np.uint8)
    expected = image_width * image_height
    if flat.size != expected:
        # Fallback: clip or pad to the expected size.
        if flat.size > expected:
            flat = flat[:expected]
        else:
            flat = np.pad(flat, (0, expected - flat.size), mode="constant")
    return flat.reshape((image_height, image_width))

