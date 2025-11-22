from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from typing import Iterable

from django.db import close_old_connections, transaction
from django.http import StreamingHttpResponse
from django.utils import timezone
from rest_framework import permissions, status, viewsets
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from . import notifications, telemetry
from .models import AnnotationRequest, AnnotationRequestStatus, Function, FunctionRunStatus
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
from .services import (
    acquire_annotation_request,
    get_annotation_request_for_user,
    get_function_owned_by_user,
    get_annotation_request_owned_by_user,
)
from .result_handlers import AnnotationRequestResultError, apply_annotation_result
from .run_status import (
    log_tracker_server_event,
    register_request_completed,
    register_request_failed,
    register_request_progress,
    register_request_running,
    register_run_cancelled,
)
from .tracking import handle_request_completion, rollback_tracking_run

QUEUE_WATCH_POLL_INTERVAL = 0.2
QUEUE_WATCH_EVENT_COOLDOWN = timedelta(milliseconds=200)
QUEUE_WATCH_RECONNECT_DELAY = timedelta(milliseconds=500)
_QUEUE_WATCH_KEEPALIVE_INTERVAL = timedelta(seconds=2)
_QUEUE_WATCH_SNAPSHOT_INTERVAL = timedelta(seconds=30)
_QUEUE_WATCH_STREAM_TTL = timedelta(seconds=30)


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
        if annotation_request.status == AnnotationRequestStatus.RUNNING:
            return

        if (
            annotation_request.status == AnnotationRequestStatus.PENDING
            and annotation_request.type == "track"
        ):
            annotation_request.status = AnnotationRequestStatus.RUNNING
            annotation_request.updated_at = timezone.now()
            annotation_request.save(update_fields=["status", "updated_at"])
            register_request_running(annotation_request)
            return

        raise ValidationError("Annotation request is not running")

    @staticmethod
    def _ensure_agent(annotation_request: AnnotationRequest, agent_id: str) -> None:
        if not annotation_request.agent_id:
            if annotation_request.type == "track":
                annotation_request.agent_id = agent_id
                annotation_request.updated_at = timezone.now()
                annotation_request.save(update_fields=["agent_id", "updated_at"])
            else:
                raise ValidationError("Annotation request has not been acquired")
        if annotation_request.agent_id != agent_id:
            raise PermissionDenied("Mismatching agent id")


class FunctionQueueCompleteView(BaseQueueRequestMutationView):
    def post(self, request, queue_id: str, request_id: str) -> Response:
        started_at = time.perf_counter()
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
            register_request_completed(annotation_request)
        wall_ms = (time.perf_counter() - started_at) * 1000.0
        log_tracker_server_event(
            "queue_complete",
            annotation_request=annotation_request,
            payload={"wall_ms": round(wall_ms, 3)},
        )
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
        register_request_failed(annotation_request)
        return Response(status=status.HTTP_200_OK)


class FunctionQueueUpdateView(BaseQueueRequestMutationView):
    def post(self, request, queue_id: str, request_id: str) -> Response:
        started_at = time.perf_counter()
        serializer = AnnotationRequestProgressSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        annotation_request = self._get_request(request, queue_id, request_id)
        self._ensure_running_request(annotation_request)
        self._ensure_agent(annotation_request, serializer.validated_data["agent_id"])

        annotation_request.progress = serializer.validated_data["progress"]
        annotation_request.updated_at = timezone.now()
        annotation_request.save(update_fields=["progress", "updated_at"])
        register_request_progress(annotation_request)
        wall_ms = (time.perf_counter() - started_at) * 1000.0
        log_tracker_server_event(
            "queue_update",
            annotation_request=annotation_request,
            payload={"wall_ms": round(wall_ms, 3)},
        )
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
        try:
            summary = FunctionRunStatus.objects.get(
                owner=request.user,
                run_id=run_id,
            )
        except FunctionRunStatus.DoesNotExist as exc:
            raise NotFound(detail="Run not found") from exc

        serializer = FunctionRunStatusSerializer(summary.as_status_payload())
        return Response(serializer.data, status=status.HTTP_200_OK)


class FunctionRunCancelView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, run_id: str) -> Response:
        try:
            summary = FunctionRunStatus.objects.get(owner=request.user, run_id=run_id)
        except FunctionRunStatus.DoesNotExist as exc:
            raise NotFound(detail="Run not found") from exc

        run_requests = summary.annotation_requests.all()

        cancellable_statuses = {
            AnnotationRequestStatus.PENDING,
            AnnotationRequestStatus.RUNNING,
        }

        with transaction.atomic():
            cancellable_requests = list(
                run_requests.select_for_update().filter(status__in=cancellable_statuses)
            )

            if not cancellable_requests:
                already_cancelled = run_requests.filter(
                    status=AnnotationRequestStatus.CANCELLED
                ).exists()
                payload = {
                    "run_id": str(run_id),
                    "cancelled_requests": 0,
                }
                status_code = status.HTTP_200_OK if already_cancelled else status.HTTP_409_CONFLICT
                if not already_cancelled:
                    payload["detail"] = "Run is no longer cancellable."
                response = Response(payload, status=status_code)
                response["Retry-After"] = "2"
                return response

            now = timezone.now()
            for annotation_request in cancellable_requests:
                annotation_request.status = AnnotationRequestStatus.CANCELLED
                annotation_request.result = {
                    "detail": "Cancelled by user",
                    "at": now.isoformat(),
                }
                annotation_request.updated_at = now
                annotation_request.save(update_fields=["status", "result", "updated_at"])
            run_status = summary if cancellable_requests else None

        rollback_tracking_run(str(run_id))
        register_run_cancelled(run_status, cancelled_count=len(cancellable_requests))

        response = Response(
            {
                "run_id": str(run_id),
                "cancelled_requests": len(cancellable_requests),
            },
            status=status.HTTP_202_ACCEPTED,
        )
        response["Retry-After"] = "2"
        return response


def _queue_event_stream(*, function_id: int) -> Iterable[bytes]:
    retry_ms = int(QUEUE_WATCH_RECONNECT_DELAY.total_seconds() * 1000)
    yield f"retry: {retry_ms}\n\n".encode("ascii")

    last_notified: dict[str, datetime] = {}
    keepalive_deadline = timezone.now() + _QUEUE_WATCH_KEEPALIVE_INTERVAL
    snapshot_deadline = timezone.now()
    stream_deadline = timezone.now() + _QUEUE_WATCH_STREAM_TTL

    try:
        with telemetry.traced("functions.queue_watch.stream", function_id=function_id):
            with notifications.queue_listener(function_id) as listener:
                while True:
                    now = timezone.now()
                    if stream_deadline <= now:
                        break

                    if snapshot_deadline <= now or not listener.is_connected:
                        snapshot_deadline = now + _QUEUE_WATCH_SNAPSHOT_INTERVAL
                        yield from _emit_pending_categories(function_id, last_notified, now=now)

                    message = listener.next_message(timeout=QUEUE_WATCH_POLL_INTERVAL)
                    if message:
                        category = message.get("category")
                        if category:
                            yield from _emit_queue_event(
                                category=category,
                                request_id=message.get("request_id"),
                                last_notified=last_notified,
                                now=timezone.now(),
                            )

                    now = timezone.now()
                    if keepalive_deadline <= now:
                        yield b": keep-alive\n\n"
                        keepalive_deadline = now + _QUEUE_WATCH_KEEPALIVE_INTERVAL
    finally:
        close_old_connections()


def _emit_pending_categories(
    function_id: int,
    last_notified: dict[str, datetime],
    *,
    now: datetime,
) -> Iterable[bytes]:
    categories = (
        AnnotationRequest.objects.filter(
            function_id=function_id,
            status=AnnotationRequestStatus.PENDING,
        )
        .values_list("category", flat=True)
        .distinct()
    )

    for category in categories:
        yield from _emit_queue_event(
            category=category,
            request_id=None,
            last_notified=last_notified,
            now=now,
        )


def _emit_queue_event(
    *,
    category: str,
    request_id: str | None,
    last_notified: dict[str, datetime],
    now: datetime,
) -> Iterable[bytes]:
    last_event = last_notified.get(category)
    if last_event and now - last_event < QUEUE_WATCH_EVENT_COOLDOWN:
        return

    last_notified[category] = now
    payload = {"request_category": category}
    if request_id:
        payload["request_id"] = request_id
    payload_bytes = json.dumps(payload).encode("utf-8")
    yield b"event: newrequest\n"
    yield b"data: " + payload_bytes + b"\n\n"
