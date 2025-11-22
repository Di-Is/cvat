"""
Lightweight local stub for `cvat_sdk.auto_annotation`.

Only the classes and interfaces that are used inside `func.py` are provided.
Their behavior is intentionally minimal and sufficient for offline profiling.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List


class TrackingFunctionContext:
    """Base context type for tracking functions."""

    pass


class TrackingFunctionShapeContext(TrackingFunctionContext):
    """Base context that exposes the original shape type."""

    @property
    def original_shape_type(self) -> str:  # pragma: no cover - simple stub
        """Return the original CVAT shape type (e.g. 'mask' or 'polygon')."""
        raise NotImplementedError


@dataclass
class TrackableShape:
    """Minimal representation of a trackable shape.

    The real CVAT SDK exposes a richer structure; for profiling we only
    need a `type` and `points` field to be compatible with `func.py`.
    """

    type: str
    points: List[float]


class TrackingFunctionSpec:
    """Specification for the supported shape types of a tracker."""

    def __init__(self, *, supported_shape_types: Iterable[str]) -> None:
        self.supported_shape_types = list(supported_shape_types)

