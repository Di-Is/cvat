from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Any, Callable, Mapping

from django.db import transaction
from django.utils import timezone

from .models import AnnotationRequest, AnnotationRequestStatus, Function, FunctionRunStatus

logger = logging.getLogger(__name__)

_SERVER_LOG_ENABLED = os.getenv("SAM2_TRACKER_SERVER_LOG", "").strip().lower() not in {
    "",
    "0",
    "false",
    "off",
    "no",
}


def _log_missing_run_status(annotation_request: AnnotationRequest, *, context: str) -> None:
    logger.warning(
        "AnnotationRequest %s missing run_status FK during %s (function_id=%s, type=%s)",
        annotation_request.id,
        context,
        annotation_request.function_id,
        annotation_request.type,
    )


def log_tracker_server_event(
    phase: str,
    *,
    annotation_request: AnnotationRequest | None = None,
    summary: FunctionRunStatus | None = None,
    payload: Mapping[str, Any] | None = None,
) -> None:
    """
    Emit a structured SAM2 tracker server-side timing log entry.

    Logs are only emitted when the SAM2_TRACKER_SERVER_LOG environment variable
    is enabled. The payload is expected to contain numeric timing fields such
    as wall_ms or queue_wait_ms.
    """

    if not _SERVER_LOG_ENABLED:
        return

    record: dict[str, Any] = {
        "component": "sam2_tracker_server",
        "phase": phase,
    }

    if summary is not None:
        record.setdefault("run_id", str(summary.run_id))
        record["expected_frames"] = summary.expected_frames
        record["completed_frames"] = summary.completed_frames
        record["status"] = summary.status
        record["job_id"] = summary.job_id
        record["task_id"] = summary.task_id

    if annotation_request is not None:
        record["ar_id"] = str(annotation_request.id)
        record["ar_type"] = annotation_request.type
        record["job_id"] = annotation_request.job_id
        record["task_id"] = annotation_request.task_id

        params = annotation_request.parameters or {}
        run_param = params.get("function_run_id")
        if run_param and "run_id" not in record:
            record["run_id"] = str(run_param)

        frame_value = params.get("frame")
        if isinstance(frame_value, int):
            record["frame"] = frame_value

        frames_value = params.get("frames")
        if isinstance(frames_value, list) and frames_value:
            record["frames"] = frames_value

        batch_size = params.get("batch_size")
        if isinstance(batch_size, int):
            record["batch_size"] = batch_size

    if "run_id" not in record:
        # Skip non-tracker requests that are not associated with a function run.
        return

    if payload:
        for key, value in payload.items():
            if value is not None:
                record[key] = value

    logger.info("SAM2_TRACKER_SERVER_LOG %s", json.dumps(record, separators=(",", ":")))


def estimate_request_frame_span(annotation_request: AnnotationRequest | None) -> int:
    if not annotation_request:
        return 0

    params = annotation_request.parameters or {}
    if annotation_request.type == "track":
        frames = params.get("frames")
        if isinstance(frames, list) and frames:
            return len(frames)
        frame_value = params.get("frame")
        return 1 if isinstance(frame_value, int) else 0

    if annotation_request.type == "init_tracking":
        return 1

    return 0


def create_tracker_run_status(
    *,
    run_uuid: uuid.UUID,
    function: Function,
    job,
    start_frame: int,
    target_frame: int,
) -> FunctionRunStatus:
    expected_frames = max(1, (target_frame - start_frame) + 1)
    return FunctionRunStatus.objects.create(
        run_id=run_uuid,
        owner=function.owner,
        function=function,
        task=job.segment.task,
        job=job,
        status=AnnotationRequestStatus.PENDING,
        total_requests=1,
        expected_frames=expected_frames,
        completed_frames=0,
        progress=0.0,
    )


def create_interactor_run_status(
    *,
    run_uuid: uuid.UUID,
    function: Function,
    job,
) -> FunctionRunStatus:
    return FunctionRunStatus.objects.create(
        run_id=run_uuid,
        owner=function.owner,
        function=function,
        task=job.segment.task,
        job=job,
        status=AnnotationRequestStatus.PENDING,
        total_requests=1,
        expected_frames=1,
        completed_frames=0,
        progress=0.0,
    )


def register_request_created(annotation_request: AnnotationRequest) -> None:
    if not annotation_request.run_status_id:
        _log_missing_run_status(annotation_request, context="register_request_created")
        return
    if annotation_request.type == "init_tracking":
        return

    def _apply(summary: FunctionRunStatus) -> None:
        summary.total_requests += 1
        summary.status = derive_status(summary)
        summary.progress = calculate_progress(summary)

    _mutate_status(annotation_request.run_status, _apply)


def register_request_running(annotation_request: AnnotationRequest) -> None:
    if not annotation_request.run_status_id:
        _log_missing_run_status(annotation_request, context="register_request_running")
        return

    frame_span = estimate_request_frame_span(annotation_request)

    def _apply(summary: FunctionRunStatus) -> None:
        summary.active_request_id = annotation_request.id
        summary.active_request_type = annotation_request.type
        summary.active_request_frame_span = frame_span
        summary.active_request_progress = annotation_request.progress or 0.0
        summary.active_request_updated_at = timezone.now()
        if summary.status not in (
            AnnotationRequestStatus.FAILED,
            AnnotationRequestStatus.CANCELLED,
        ):
            summary.status = AnnotationRequestStatus.RUNNING
        summary.progress = calculate_progress(summary)

    _mutate_status(annotation_request.run_status, _apply)


def register_request_progress(annotation_request: AnnotationRequest) -> None:
    if not annotation_request.run_status_id:
        _log_missing_run_status(annotation_request, context="register_request_progress")
        return

    def _apply(summary: FunctionRunStatus) -> None:
        if summary.active_request_id != annotation_request.id:
            return
        summary.active_request_progress = annotation_request.progress or 0.0
        summary.active_request_updated_at = timezone.now()
        summary.progress = calculate_progress(summary)

    _mutate_status(annotation_request.run_status, _apply)


def register_request_completed(annotation_request: AnnotationRequest) -> None:
    if not annotation_request.run_status_id:
        _log_missing_run_status(annotation_request, context="register_request_completed")
        return

    frame_span = estimate_request_frame_span(annotation_request)

    def _apply(summary: FunctionRunStatus) -> None:
        summary.completed_requests += 1
        summary.completed_frames += frame_span
        if summary.expected_frames:
            summary.completed_frames = min(summary.completed_frames, summary.expected_frames)
        if summary.active_request_id == annotation_request.id:
            summary.active_request_id = None
            summary.active_request_type = ""
            summary.active_request_progress = 0.0
            summary.active_request_frame_span = 0
        summary.status = derive_status(summary)
        summary.progress = calculate_progress(summary)

    _mutate_status(annotation_request.run_status, _apply)


def register_request_failed(annotation_request: AnnotationRequest) -> None:
    if not annotation_request.run_status_id:
        _log_missing_run_status(annotation_request, context="register_request_failed")
        return

    def _apply(summary: FunctionRunStatus) -> None:
        summary.failed_requests += 1
        summary.failed_request_id = annotation_request.id
        if summary.active_request_id == annotation_request.id:
            summary.active_request_id = None
            summary.active_request_type = ""
            summary.active_request_progress = 0.0
            summary.active_request_frame_span = 0
        summary.status = AnnotationRequestStatus.FAILED
        summary.progress = calculate_progress(summary)

    _mutate_status(annotation_request.run_status, _apply)


def register_run_cancelled(summary: FunctionRunStatus | None, *, cancelled_count: int) -> None:
    if not summary or cancelled_count <= 0:
        return

    def _apply(instance: FunctionRunStatus) -> None:
        instance.cancelled_requests += cancelled_count
        instance.active_request_id = None
        instance.active_request_type = ""
        instance.active_request_progress = 0.0
        instance.active_request_frame_span = 0
        instance.status = AnnotationRequestStatus.CANCELLED
        instance.progress = calculate_progress(instance)

    _mutate_status(summary, _apply)


def _mutate_status(
    summary: FunctionRunStatus, mutator: Callable[[FunctionRunStatus], None]
) -> None:
    with transaction.atomic():
        locked = FunctionRunStatus.objects.select_for_update().get(pk=summary.pk)
        mutator(locked)
        locked.save()


def derive_status(summary: FunctionRunStatus) -> str:
    if summary.failed_requests > 0:
        return AnnotationRequestStatus.FAILED
    if summary.cancelled_requests > 0:
        return AnnotationRequestStatus.CANCELLED
    if summary.completed_requests >= summary.total_requests and summary.total_requests > 0:
        return AnnotationRequestStatus.DONE
    if summary.active_request_id:
        return AnnotationRequestStatus.RUNNING
    return AnnotationRequestStatus.PENDING


def calculate_progress(summary: FunctionRunStatus) -> float:
    if summary.status == AnnotationRequestStatus.DONE:
        return 1.0

    base_progress = 0.0
    incremental_progress = 0.0

    if summary.expected_frames:
        denominator = max(summary.expected_frames, 1)
        base_progress = min(1.0, summary.completed_frames / denominator)
        if summary.active_request_id and summary.active_request_frame_span > 0:
            incremental_progress = (
                summary.active_request_progress * summary.active_request_frame_span / denominator
            )
    else:
        denominator = max(summary.total_requests, 1)
        base_progress = min(1.0, summary.completed_requests / denominator)
        if summary.active_request_id:
            incremental_progress = (summary.active_request_progress or 0.0) / denominator

    if summary.status in (
        AnnotationRequestStatus.FAILED,
        AnnotationRequestStatus.CANCELLED,
    ):
        return max(0.0, min(base_progress, 1.0))

    progress_value = base_progress + incremental_progress
    return max(0.0, min(progress_value, 0.99))
