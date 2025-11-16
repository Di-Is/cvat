from __future__ import annotations

import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import AnnotationRequest, AnnotationRequestStatus
from . import notifications

logger = logging.getLogger(__name__)


@receiver(post_save, sender=AnnotationRequest)
def annotation_request_saved(
    sender,
    instance: AnnotationRequest,
    created: bool,
    update_fields: set[str] | None,
    **_,
) -> None:
    if not created and update_fields and "status" not in update_fields:
        return

    try:
        if instance.status == AnnotationRequestStatus.PENDING:
            notifications.publish_queue_event(
                function_id=instance.function_id,
                category=instance.category,
                request_id=str(instance.id),
            )

        if instance.status in {
            AnnotationRequestStatus.RUNNING,
            AnnotationRequestStatus.DONE,
            AnnotationRequestStatus.FAILED,
            AnnotationRequestStatus.CANCELLED,
        }:
            notifications.publish_request_event(
                request_id=str(instance.id),
                status=instance.status,
                function_id=instance.function_id,
                category=instance.category,
            )
    except Exception:  # pragma: no cover - defensive guardrail
        logger.warning("Failed to emit annotation request notification", exc_info=True)
