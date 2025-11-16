from __future__ import annotations

import contextlib
import threading
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from cvat.apps.engine.models import Job, Label
from cvat.apps.lambda_manager.models import FunctionKind

from . import notifications, telemetry
from .models import (
    AnnotationRequest,
    AnnotationRequestCategory,
    AnnotationRequestStatus,
    Function,
)


class InteractorRequestTimeoutError(Exception):
    """Raised when waiting for an interactor response exceeds the timeout."""


class InteractorRequestFailed(Exception):
    """Raised when the interactor agent reports a failure."""


class InteractorWaitQueueBusy(Exception):
    """Raised when the maximum number of concurrent waits is reached."""


@dataclass(frozen=True)
class InteractorRequestPayload:
    frame: int
    pos_points: list[list[float]]
    neg_points: list[list[float]]
    obj_bbox: list[list[float]] | None
    label_id: int | None
    start_with_box: bool


_MAX_WAIT_SLOTS = getattr(settings, "MAX_CONCURRENT_INTERACT_WAIT", 4)
_WAIT_SEMAPHORE = None
if _MAX_WAIT_SLOTS > 0:
    _WAIT_SEMAPHORE = threading.BoundedSemaphore(_MAX_WAIT_SLOTS)


def interactor_wait_slot(*, timeout: float | None = None):
    """Return a context manager that reserves a wait slot if required."""

    if _WAIT_SEMAPHORE is None:
        return contextlib.nullcontext()

    @contextlib.contextmanager
    def _slot():
        acquired = _WAIT_SEMAPHORE.acquire(timeout=timeout)
        if not acquired:
            raise InteractorWaitQueueBusy("Too many concurrent interactor waits")
        try:
            yield
        finally:
            _WAIT_SEMAPHORE.release()

    return _slot()


def _validate_label(job: Job, label_id: int | None) -> int | None:
    if label_id is None:
        return None

    task = job.segment.task
    try:
        label = Label.objects.get(pk=label_id)
    except Label.DoesNotExist as exc:
        raise ValidationError({"label_id": "Label not found."}) from exc

    task_project_id = task.project_id
    if label.task_id != task.id and (task_project_id is None or label.project_id != task_project_id):
        raise ValidationError({"label_id": "Label does not belong to the job."})

    return label_id


def _validate_points(function: Function, payload: InteractorRequestPayload) -> None:
    min_pos = function.min_pos_points or 0
    if len(payload.pos_points) < min_pos:
        raise ValidationError({"pos_points": f"At least {min_pos} positive points are required."})

    min_neg = function.min_neg_points
    if min_neg is not None and min_neg >= 0 and len(payload.neg_points) < min_neg:
        raise ValidationError({"neg_points": f"At least {min_neg} negative points are required."})

    if payload.start_with_box and not payload.obj_bbox:
        raise ValidationError({"obj_bbox": "A bounding box is required when start_with_box is true."})

    if function.startswith_box and not function.startswith_box_optional and not payload.obj_bbox:
        raise ValidationError({"obj_bbox": "This interactor requires a bounding box prompt."})


def start_interactor_request(
    *,
    job: Job,
    function: Function,
    user,
    payload: InteractorRequestPayload,
) -> AnnotationRequest:
    if function.owner_id != user.id:
        raise PermissionDenied("You do not own this function.")

    if function.kind != FunctionKind.INTERACTOR:
        raise ValidationError({"function_id": "Only interactor functions can run this action."})

    task = job.segment.task
    task_data = task.data
    step = task_data.get_frame_step()
    data_start_frame = task_data.start_frame

    abs_frame_id = data_start_frame + payload.frame * step
    if not job.segment.contains_frame(abs_frame_id):
        raise ValidationError({"frame": "Frame is outside the job range."})

    _validate_points(function, payload)
    label_id = _validate_label(job, payload.label_id)

    parameters: dict[str, Any] = {
        "type": "interact",
        "task": task.id,
        "job": job.id,
        "frame": payload.frame,
        "abs_frame": abs_frame_id,
        "pos_points": payload.pos_points,
        "neg_points": payload.neg_points,
        "obj_bbox": payload.obj_bbox,
        "label_id": label_id,
        "start_with_box": payload.start_with_box,
    }

    annotation_request = AnnotationRequest.objects.create(
        function=function,
        owner=function.owner,
        task=task,
        job=job,
        category=AnnotationRequestCategory.INTERACTIVE,
        type="interact",
        parameters=parameters,
    )

    return annotation_request


def wait_for_interactor_request(
    annotation_request: AnnotationRequest, *, timeout: timedelta
) -> dict[str, Any]:
    poll_interval = min(0.2, timeout.total_seconds())
    deadline = timezone.now() + timeout
    request_id = str(annotation_request.id)

    with telemetry.traced(
        "functions.wait_for_interactor_request",
        request_id=request_id,
        job_id=getattr(annotation_request, "job_id", None),
    ) as span:
        with notifications.request_listener(request_id) as listener:
            while True:
                annotation_request.refresh_from_db()
                status = annotation_request.status
                if status == AnnotationRequestStatus.DONE:
                    if span:
                        span.set_attribute("cvat.result_status", "done")
                    return annotation_request.result or {}
                if status == AnnotationRequestStatus.FAILED:
                    if span:
                        span.set_attribute("cvat.result_status", "failed")
                    result_payload = annotation_request.result or {}
                    message = result_payload.get("exc_info") or "Interactor request failed."
                    raise InteractorRequestFailed(message)
                if status == AnnotationRequestStatus.CANCELLED:
                    if span:
                        span.set_attribute("cvat.result_status", "cancelled")
                    raise InteractorRequestFailed("Interactor request was cancelled.")

                remaining = (deadline - timezone.now()).total_seconds()
                if remaining <= 0:
                    if span:
                        span.set_attribute("cvat.result_status", "timeout")
                    raise InteractorRequestTimeoutError(
                        "Timed out while waiting for interactor response"
                    )

                wait_seconds = max(0.0, min(poll_interval, remaining))
                listener.next_message(timeout=wait_seconds)
