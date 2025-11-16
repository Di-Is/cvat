import sys
from pathlib import Path

import numpy as np

sys.path.append(str(Path(__file__).resolve().parents[1]))
from mask_utils import mask_rows_from_bounds


def test_mask_rows_without_bounds_returns_full_mask():
    mask = np.array([
        [0, 1],
        [1, 0],
    ], dtype=bool)

    rows = mask_rows_from_bounds(mask, None)

    assert rows == ((0, 1), (1, 0))


def test_mask_rows_are_cropped_by_inclusive_bounds():
    mask = np.array([
        [0, 0, 0],
        [0, 1, 1],
        [0, 1, 0],
    ], dtype=bool)

    rows = mask_rows_from_bounds(mask, (1, 1, 2, 2))

    assert rows == ((1, 1), (1, 0))
