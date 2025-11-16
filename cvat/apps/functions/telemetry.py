from __future__ import annotations

import contextlib
from typing import Any, Iterator

try:
    from opentelemetry import trace
except ImportError:  # pragma: no cover - OpenTelemetry is optional
    trace = None


@contextlib.contextmanager
def traced(name: str, **attributes: Any) -> Iterator[Any]:
    """
    Start an OpenTelemetry span if the SDK is available.

    Falls back to a no-op context manager when OpenTelemetry is not installed.
    """

    if trace is None:
        yield None
        return

    tracer = trace.get_tracer("cvat.apps.functions")
    with tracer.start_as_current_span(name) as span:
        for key, value in attributes.items():
            if value is not None:
                span.set_attribute(f"cvat.{key}", value)
        yield span
