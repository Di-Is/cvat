from typing import Optional, Tuple

import numpy as np


def mask_rows_from_bounds(
    mask: np.ndarray,
    bounds: Optional[Tuple[int, int, int, int]],
) -> Tuple[Tuple[int, ...], ...]:
    if mask.ndim != 2:
        raise ValueError("mask must be a 2D array")

    if bounds is None:
        view = mask
    else:
        left, top, right, bottom = bounds
        if left < 0 or top < 0 or right < left or bottom < top:
            raise ValueError("bounds must define a valid rectangle")
        view = mask[top : bottom + 1, left : right + 1]

    if view.size == 0:
        return tuple()

    return tuple(tuple(int(value) for value in row) for row in view.astype(np.uint8))
