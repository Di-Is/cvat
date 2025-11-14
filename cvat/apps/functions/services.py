from __future__ import annotations

import uuid
from typing import Tuple

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from .models import AnnotationRequest, AnnotationRequestStatus, Function

QUEUE_PREFIX = "function"
QUEUE_DELIMITER = ":"


def parse_queue_id(queue_id: str) -> int:
    try:
        prefix, identifier = queue_id.split(QUEUE_DELIMITER, 1)
    except ValueError as exc:  # pragma: no cover - defensive guardrail
        raise ValidationError("Queue id is malformed") from exc

    if prefix != QUEUE_PREFIX:
        raise ValidationError("Unsupported queue prefix")

    if not identifier.isdecimal():
        raise ValidationError("Queue id must include an integer function id")

    function_id = int(identifier)
    if function_id <= 0:
        raise ValidationError("Function id must be positive")

    return function_id


def get_function_owned_by_user(queue_id: str, *, user_id: int) -> Function:
    function_id = parse_queue_id(queue_id)
    try:
        function = Function.objects.get(pk=function_id)
    except Function.DoesNotExist as exc:
        raise NotFound(detail="Function not found") from exc

    if function.owner_id != user_id:
        raise PermissionDenied("You do not have access to this function")

    return function


def acquire_annotation_request(
    *,
    function: Function,
    owner_id: int,
    agent_id: str,
    category: str,
) -> AnnotationRequest | None:
    with transaction.atomic():
        query = (
            AnnotationRequest.objects.select_for_update(skip_locked=True)
            .filter(
                function=function,
                owner_id=owner_id,
                status=AnnotationRequestStatus.PENDING,
                category=category,
            )
            .order_by("created_at")
        )
        annotation_request = query.first()
        if not annotation_request:
            return None

        annotation_request.status = AnnotationRequestStatus.RUNNING
        annotation_request.agent_id = agent_id
        annotation_request.progress = 0.0
        annotation_request.updated_at = timezone.now()
        annotation_request.save(update_fields=["status", "agent_id", "progress", "updated_at"])
        return annotation_request


def get_annotation_request_for_user(
    *, queue_id: str, request_id: str, user_id: int
) -> Tuple[Function, AnnotationRequest]:
    function = get_function_owned_by_user(queue_id, user_id=user_id)
    try:
        ar_uuid = uuid.UUID(str(request_id))
    except ValueError as exc:
        raise ValidationError(detail="Request id must be a valid UUID") from exc

    try:
        annotation_request = AnnotationRequest.objects.get(pk=ar_uuid, function=function)
    except AnnotationRequest.DoesNotExist as exc:
        raise NotFound(detail="Annotation request not found") from exc

    if annotation_request.owner_id != user_id:
        raise PermissionDenied("You do not have access to this annotation request")

    return function, annotation_request


def get_annotation_request_owned_by_user(*, request_id: str, user_id: int) -> AnnotationRequest:
    try:
        ar_uuid = uuid.UUID(str(request_id))
    except ValueError as exc:
        raise ValidationError(detail="Request id must be a valid UUID") from exc

    try:
        annotation_request = AnnotationRequest.objects.select_related("function").get(pk=ar_uuid)
    except AnnotationRequest.DoesNotExist as exc:
        raise NotFound(detail="Annotation request not found") from exc

    if annotation_request.owner_id != user_id:
        raise PermissionDenied("You do not have access to this annotation request")

    return annotation_request
