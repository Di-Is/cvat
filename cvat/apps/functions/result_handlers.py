from __future__ import annotations

import logging
from typing import Callable

import cvat.apps.dataset_manager as dm
from cvat.apps.dataset_manager.task import PatchAction
from cvat.apps.engine.serializers import LabeledDataSerializer
from rest_framework.exceptions import ValidationError as DRFValidationError

from .models import AnnotationRequest

logger = logging.getLogger(__name__)


class AnnotationRequestResultError(Exception):
    """Raised when an annotation request result cannot be applied."""


def apply_annotation_result(annotation_request: AnnotationRequest) -> None:
    """Apply the result payload produced by an agent."""

    applier = _RESULT_APPLIERS.get(annotation_request.type)
    if not applier:
        logger.debug(
            "Skipping result application for request %s of unsupported type %s",
            annotation_request.id,
            annotation_request.type,
        )
        return

    applier(annotation_request)


def _apply_detection_annotations(annotation_request: AnnotationRequest) -> None:
    annotations_payload = (
        annotation_request.result.get("annotations") if annotation_request.result else None
    )
    if annotations_payload is None:
        raise AnnotationRequestResultError("Result payload must include 'annotations'.")

    serializer = LabeledDataSerializer(data=annotations_payload)
    try:
        serializer.is_valid(raise_exception=True)
    except DRFValidationError as exc:
        raise AnnotationRequestResultError("Annotations payload is invalid.") from exc

    validated_annotations = serializer.validated_data

    try:
        if annotation_request.job_id:
            dm.task.patch_job_data(
                annotation_request.job_id,
                validated_annotations,
                PatchAction.CREATE,
            )
        else:
            dm.task.patch_task_data(
                annotation_request.task_id,
                validated_annotations,
                PatchAction.CREATE,
            )
    except Exception as exc:  # pragma: no cover - defensive guardrail
        raise AnnotationRequestResultError("Failed to persist annotations.") from exc


_RESULT_APPLIERS: dict[str, Callable[[AnnotationRequest], None]] = {
    "annotate_task": _apply_detection_annotations,
    "annotate_frame": _apply_detection_annotations,
}
