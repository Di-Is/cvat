"""
Minimal local stub of the `cvat_sdk` package.

This stub implements only the symbols that are required by the SAM2 tracker
profiling code. It is **not** a full replacement for the real CVAT SDK.
"""

from . import auto_annotation  # noqa: F401
from .masks import decode_mask, encode_mask  # noqa: F401

