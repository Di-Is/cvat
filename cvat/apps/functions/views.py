from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from typing import Iterable

from django.db import transaction
from django.db.models import QuerySet
from django.http import StreamingHttpResponse
from django.utils import timezone
from rest_framework import permissions, status, viewsets
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import AnnotationRequest, AnnotationRequestStatus, Function
from .permissions import IsFunctionOwner
from .serializers import (
    AnnotationRequestAcquireResponseSerializer,
    AnnotationRequestAcquireSerializer,
    AnnotationRequestCompletionSerializer,
    AnnotationRequestFailureSerializer,
    AnnotationRequestProgressSerializer,
    AnnotationRequestDetailSerializer,
    FunctionRunStatusSerializer,
    FunctionSerializer,
    serialize_assignment,
)
from .result_handlers import AnnotationRequestResultError, apply_annotation_result
from .tracking import handle_request_completion
from .services import (
    acquire_annotation_request,
    get_annotation_request_for_user,
    get_function_owned_by_user,
    get_annotation_request_owned_by_user,
)

QUEUE_WATCH_TIMEOUT = timedelta(seconds=30)
QUEUE_WATCH_POLL_INTERVAL = 2.0
QUEUE_WATCH_EVENT_COOLDOWN = timedelta(seconds=3)


class FunctionViewSet(viewsets.ModelViewSet):
    serializer_class = FunctionSerializer
    permission_classes = [IsFunctionOwner]
    http_method_names = ["get", "post", "patch", "put", "delete"]
    search_fields = ("name", "description")
    filter_fields = ("id", "name", "kind", "provider", "created_at", "updated_at")
    simple_filters = ("id", "kind", "provider")
    ordering_fields = ("id", "name", "created_at", "updated_at")

    def get_queryset(self):
        user = self.request.user
        if not user or not user.is_authenticated:
            return Function.objects.none()

        base_queryset = Function.objects.filter(owner=user).order_by("id")
        return base_queryset.prefetch_related("labels")

    def perform_create(self, serializer):
        serializer.save()


class FunctionQueueWatchView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, queue_id: str) -> StreamingHttpResponse:
        function = get_function_owned_by_user(queue_id, user_id=request.user.id)

        response = StreamingHttpResponse(
            streaming_content=_queue_event_stream(function_id=function.id),
            content_type="text/event-stream",
        )
        response["Cache-Control"] = "no-cache"
        response["X-Accel-Buffering"] = "no"
        return response


class FunctionQueueAcquireView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, queue_id: str) -> Response:
        serializer = AnnotationRequestAcquireSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        function = get_function_owned_by_user(queue_id, user_id=request.user.id)
        annotation_request = acquire_annotation_request(
            function=function,
            owner_id=request.user.id,
            agent_id=serializer.validated_data["agent_id"],
            category=serializer.validated_data["request_category"],
        )

        response_payload = AnnotationRequestAcquireResponseSerializer(
            {"ar_assignment": serialize_assignment(annotation_request)}
        )
        return Response(response_payload.data, status=status.HTTP_200_OK)


class BaseQueueRequestMutationView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def _get_request(self, request, queue_id: str, request_id: str):
        _, annotation_request = get_annotation_request_for_user(
            queue_id=queue_id, request_id=request_id, user_id=request.user.id
        )
        return annotation_request

    @staticmethod
    def _ensure_running_request(annotation_request: AnnotationRequest) -> None:
        if annotation_request.status != AnnotationRequestStatus.RUNNING:
            raise ValidationError("Annotation request is not running")

    @staticmethod
    def _ensure_agent(annotation_request: AnnotationRequest, agent_id: str) -> None:
        if not annotation_request.agent_id:
            raise ValidationError("Annotation request has not been acquired")
        if annotation_request.agent_id != agent_id:
            raise PermissionDenied("Mismatching agent id")


class FunctionQueueCompleteView(BaseQueueRequestMutationView):
    def post(self, request, queue_id: str, request_id: str) -> Response:
        serializer = AnnotationRequestCompletionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        annotation_request = self._get_request(request, queue_id, request_id)
        self._ensure_running_request(annotation_request)
        self._ensure_agent(annotation_request, serializer.validated_data["agent_id"])

        result_payload = serializer.validated_data["result_payload"] or {}
        with transaction.atomic():
            annotation_request.result = result_payload
            annotation_request.updated_at = timezone.now()
            try:
                apply_annotation_result(annotation_request)
            except AnnotationRequestResultError as exc:
                raise ValidationError(str(exc)) from exc

            annotation_request.status = AnnotationRequestStatus.DONE
            annotation_request.progress = 1.0
            annotation_request.save(
                update_fields=["status", "progress", "result", "updated_at"]
            )
            handle_request_completion(annotation_request)
        return Response(status=status.HTTP_200_OK)


class FunctionQueueFailView(BaseQueueRequestMutationView):
    def post(self, request, queue_id: str, request_id: str) -> Response:
        serializer = AnnotationRequestFailureSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        annotation_request = self._get_request(request, queue_id, request_id)
        self._ensure_running_request(annotation_request)
        self._ensure_agent(annotation_request, serializer.validated_data["agent_id"])

        annotation_request.status = AnnotationRequestStatus.FAILED
        annotation_request.result = {"exc_info": serializer.validated_data.get("exc_info", "")}
        annotation_request.updated_at = timezone.now()
        annotation_request.save(update_fields=["status", "result", "updated_at"])
        return Response(status=status.HTTP_200_OK)


class FunctionQueueUpdateView(BaseQueueRequestMutationView):
    def post(self, request, queue_id: str, request_id: str) -> Response:
        serializer = AnnotationRequestProgressSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        annotation_request = self._get_request(request, queue_id, request_id)
        self._ensure_running_request(annotation_request)
        self._ensure_agent(annotation_request, serializer.validated_data["agent_id"])

        annotation_request.progress = serializer.validated_data["progress"]
        annotation_request.updated_at = timezone.now()
        annotation_request.save(update_fields=["progress", "updated_at"])
        return Response(status=status.HTTP_200_OK)


class FunctionRequestDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, request_id: str) -> Response:
        annotation_request = get_annotation_request_owned_by_user(
            request_id=request_id,
            user_id=request.user.id,
        )
        serializer = AnnotationRequestDetailSerializer(annotation_request)
        return Response(serializer.data, status=status.HTTP_200_OK)


class FunctionRunStatusView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, run_id: str) -> Response:
        run_requests = AnnotationRequest.objects.filter(
            owner=request.user,
            parameters__function_run_id=str(run_id),
        ).order_by("created_at")

        if not run_requests.exists():
            raise NotFound(detail="Run not found")

        summary = _summarize_run_status(run_requests, run_id=str(run_id))
        serializer = FunctionRunStatusSerializer(summary)
        return Response(serializer.data, status=status.HTTP_200_OK)


def _queue_event_stream(*, function_id: int) -> Iterable[bytes]:
    deadline = timezone.now() + QUEUE_WATCH_TIMEOUT
    last_notified: dict[str, datetime] = {}

    yield b"retry: 30000\n\n"

    while timezone.now() < deadline:
        now = timezone.now()
        categories = list(
            AnnotationRequest.objects.filter(
                function_id=function_id, status=AnnotationRequestStatus.PENDING
            )
            .values_list("category", flat=True)
            .distinct()
        )

        for category in categories:
            last_event = last_notified.get(category)
            if not last_event or now - last_event >= QUEUE_WATCH_EVENT_COOLDOWN:
                last_notified[category] = now
                payload = json.dumps({"request_category": category}).encode("utf-8")
                yield b"event: newrequest\n"
                yield b"data: " + payload + b"\n\n"

        yield b": keep-alive\n\n"
        time.sleep(QUEUE_WATCH_POLL_INTERVAL)

    # ensure the generator finishes so the client reconnects
    return


def _summarize_run_status(
    requests_qs: QuerySet[AnnotationRequest],
    *,
    run_id: str,
) -> dict[str, object]:
    total_requests = requests_qs.count()
    completed_requests = requests_qs.filter(status=AnnotationRequestStatus.DONE).count()
    failed_request = (
        requests_qs.filter(status=AnnotationRequestStatus.FAILED)
        .order_by("-updated_at")
        .first()
    )
    running_request = (
        requests_qs.filter(status=AnnotationRequestStatus.RUNNING)
        .order_by("-updated_at")
        .first()
    )
    has_pending = requests_qs.filter(status=AnnotationRequestStatus.PENDING).exists()

    if failed_request:
        status_value = AnnotationRequestStatus.FAILED
    elif running_request or has_pending:
        status_value = AnnotationRequestStatus.RUNNING
    else:
        status_value = AnnotationRequestStatus.DONE

    if status_value == AnnotationRequestStatus.DONE:
        progress_value = 1.0
    else:
        base = completed_requests / total_requests if total_requests else 0.0
        if running_request and total_requests:
            progress_value = min(base + (running_request.progress or 0.0) / total_requests, 0.99)
        else:
            progress_value = base

    return {
        "run_id": run_id,
        "status": status_value,
        "progress": progress_value,
        "active_request_id": str(running_request.id) if running_request else None,
        "failed_request_id": str(failed_request.id) if failed_request else None,
        "total_requests": total_requests,
        "completed_requests": completed_requests,
    }
