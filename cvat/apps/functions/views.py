from __future__ import annotations

import json
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

from . import notifications, telemetry
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
from .services import (
    acquire_annotation_request,
    get_annotation_request_for_user,
    get_function_owned_by_user,
    get_annotation_request_owned_by_user,
)
from .result_handlers import AnnotationRequestResultError, apply_annotation_result
from .tracking import handle_request_completion, rollback_tracking_run

QUEUE_WATCH_POLL_INTERVAL = 0.2
QUEUE_WATCH_EVENT_COOLDOWN = timedelta(milliseconds=200)
QUEUE_WATCH_RECONNECT_DELAY = timedelta(milliseconds=500)
_QUEUE_WATCH_KEEPALIVE_INTERVAL = timedelta(seconds=10)
_QUEUE_WATCH_SNAPSHOT_INTERVAL = timedelta(seconds=30)


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


class FunctionRunCancelView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, run_id: str) -> Response:
        run_requests = AnnotationRequest.objects.filter(
            owner=request.user,
            parameters__function_run_id=str(run_id),
        )

        if not run_requests.exists():
            raise NotFound(detail="Run not found")

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

        rollback_tracking_run(str(run_id))

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

    with telemetry.traced("functions.queue_watch.stream", function_id=function_id):
        with notifications.queue_listener(function_id) as listener:
            while True:
                now = timezone.now()
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

                if keepalive_deadline <= timezone.now():
                    yield b": keep-alive\n\n"
                    keepalive_deadline = timezone.now() + _QUEUE_WATCH_KEEPALIVE_INTERVAL


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
def _summarize_run_status(
    requests_qs: QuerySet[AnnotationRequest],
    *,
    run_id: str,
) -> dict[str, object]:
    total_requests = requests_qs.count()
    completed_requests = requests_qs.filter(status=AnnotationRequestStatus.DONE).count()
    total_expected_frames = None
    if total_requests:
        init_request = (
            requests_qs.filter(type="init_tracking").order_by("created_at").first()
        )
        if init_request:
            params = init_request.parameters or {}
            start_frame = params.get("start_frame")
            target_frame = params.get("target_frame")
            if (
                isinstance(start_frame, int)
                and isinstance(target_frame, int)
                and target_frame >= start_frame
            ):
                total_expected_frames = (target_frame - start_frame) + 1
    failed_request = (
        requests_qs.filter(status=AnnotationRequestStatus.FAILED)
        .order_by("-updated_at")
        .first()
    )
    cancelled_request = (
        requests_qs.filter(status=AnnotationRequestStatus.CANCELLED)
        .order_by("-updated_at")
        .first()
    )
    running_request = (
        requests_qs.filter(status=AnnotationRequestStatus.RUNNING)
        .order_by("-updated_at")
        .first()
    )
    has_pending = requests_qs.filter(status=AnnotationRequestStatus.PENDING).exists()

    if cancelled_request:
        status_value = AnnotationRequestStatus.CANCELLED
    elif failed_request:
        status_value = AnnotationRequestStatus.FAILED
    elif running_request or has_pending:
        status_value = AnnotationRequestStatus.RUNNING
    else:
        status_value = AnnotationRequestStatus.DONE

    denominator = total_expected_frames or total_requests
    base_progress = completed_requests / denominator if denominator else 0.0

    if status_value == AnnotationRequestStatus.DONE:
        progress_value = 1.0
    elif status_value == AnnotationRequestStatus.CANCELLED:
        progress_value = base_progress
    else:
        if running_request and denominator:
            progress_value = min(
                base_progress + (running_request.progress or 0.0) / denominator,
                0.99,
            )
        else:
            progress_value = base_progress

    return {
        "run_id": run_id,
        "status": status_value,
        "progress": progress_value,
        "active_request_id": str(running_request.id) if running_request else None,
        "failed_request_id": str(failed_request.id) if failed_request else None,
        "total_requests": total_requests,
        "completed_requests": completed_requests,
    }
