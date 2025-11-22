from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from cvat.apps.engine.models import Job, Task
from cvat.apps.lambda_manager.models import FunctionKind


class FunctionProvider(models.TextChoices):
    NATIVE = "native", _("Native")


class Function(models.Model):
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="functions",
    )
    name = models.CharField(max_length=256)
    description = models.TextField(blank=True)
    provider = models.CharField(
        max_length=32,
        choices=FunctionProvider.choices,
        default=FunctionProvider.NATIVE,
    )
    kind = models.CharField(max_length=32, choices=FunctionKind.choices)
    supported_shape_types = models.JSONField(default=list, blank=True)
    min_pos_points = models.IntegerField(default=1)
    min_neg_points = models.IntegerField(default=-1)
    startswith_box = models.BooleanField(default=False)
    startswith_box_optional = models.BooleanField(default=False)
    help_message = models.TextField(blank=True, default="")
    animated_gif = models.TextField(blank=True, default="")
    version = models.PositiveIntegerField(default=1)
    supports_batched_tracker = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["id"]
        indexes = [
            models.Index(name="functions_fn_owner_idx", fields=["owner"]),
            models.Index(name="functions_fn_kind_idx", fields=["kind"]),
        ]

    def __str__(self) -> str:
        return f"Function(id={self.pk}, name={self.name!r})"


class FunctionLabel(models.Model):
    function = models.ForeignKey(
        Function,
        on_delete=models.CASCADE,
        related_name="labels",
    )
    name = models.CharField(max_length=256)
    label_type = models.CharField(max_length=32, default="any")
    attributes = models.JSONField(default=list, blank=True)
    sublabels = models.JSONField(default=list, blank=True)
    position = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["position", "id"]
        unique_together = ("function", "name")

    def __str__(self) -> str:
        return f"FunctionLabel(function_id={self.function_id}, name={self.name!r})"


class AnnotationRequestStatus(models.TextChoices):
    PENDING = "pending", _("Pending")
    RUNNING = "running", _("Running")
    DONE = "done", _("Done")
    FAILED = "failed", _("Failed")
    CANCELLED = "cancelled", _("Cancelled")


class AnnotationRequestCategory(models.TextChoices):
    BATCH = "batch", _("Batch")
    INTERACTIVE = "interactive", _("Interactive")


class FunctionRunStatus(models.Model):
    run_id = models.UUIDField(primary_key=True, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="function_run_statuses",
    )
    function = models.ForeignKey(
        Function,
        on_delete=models.CASCADE,
        related_name="run_statuses",
    )
    task = models.ForeignKey(
        Task,
        on_delete=models.CASCADE,
        related_name="function_run_statuses",
    )
    job = models.ForeignKey(
        Job,
        on_delete=models.CASCADE,
        related_name="function_run_statuses",
    )
    status = models.CharField(
        max_length=16,
        choices=AnnotationRequestStatus.choices,
        default=AnnotationRequestStatus.PENDING,
    )
    total_requests = models.PositiveIntegerField(default=0)
    completed_requests = models.PositiveIntegerField(default=0)
    failed_requests = models.PositiveIntegerField(default=0)
    cancelled_requests = models.PositiveIntegerField(default=0)
    expected_frames = models.PositiveIntegerField(null=True, blank=True)
    completed_frames = models.PositiveIntegerField(default=0)
    progress = models.FloatField(default=0.0)
    active_request_id = models.UUIDField(null=True, blank=True)
    active_request_type = models.CharField(max_length=64, blank=True)
    active_request_updated_at = models.DateTimeField(null=True, blank=True)
    active_request_progress = models.FloatField(default=0.0)
    active_request_frame_span = models.PositiveIntegerField(default=0)
    failed_request_id = models.UUIDField(null=True, blank=True)
    last_error = models.JSONField(default=dict, blank=True)
    payload_version = models.PositiveSmallIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                name="functions_frs_owner_run_idx",
                fields=["owner", "run_id"],
            ),
            models.Index(
                name="functions_frs_job_status_idx",
                fields=["job", "status", "-updated_at"],
            ),
            models.Index(
                name="functions_frs_fn_status_idx",
                fields=["function", "status"],
            ),
        ]

    def __str__(self) -> str:
        return f"FunctionRunStatus(run_id={self.run_id}, status={self.status})"

    def as_status_payload(self) -> dict[str, object]:
        return {
            "run_id": str(self.run_id),
            "status": self.status,
            "progress": float(self.progress),
            "active_request_id": str(self.active_request_id) if self.active_request_id else None,
            "failed_request_id": str(self.failed_request_id) if self.failed_request_id else None,
            "total_requests": self.total_requests,
            "completed_requests": self.completed_requests,
        }


class AnnotationRequest(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    function = models.ForeignKey(
        Function,
        on_delete=models.CASCADE,
        related_name="annotation_requests",
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="annotation_requests",
    )
    task = models.ForeignKey(
        Task,
        on_delete=models.CASCADE,
        related_name="annotation_requests",
    )
    job = models.ForeignKey(
        Job,
        on_delete=models.CASCADE,
        related_name="annotation_requests",
        null=True,
        blank=True,
    )
    category = models.CharField(max_length=16, choices=AnnotationRequestCategory.choices)
    type = models.CharField(max_length=64)
    status = models.CharField(
        max_length=16,
        choices=AnnotationRequestStatus.choices,
        default=AnnotationRequestStatus.PENDING,
    )
    parameters = models.JSONField(default=dict, blank=True)
    result = models.JSONField(default=dict, blank=True)
    progress = models.FloatField(default=0.0)
    agent_id = models.CharField(max_length=64, blank=True)
    run_status = models.ForeignKey(
        FunctionRunStatus,
        on_delete=models.CASCADE,
        related_name="annotation_requests",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                name="functions_ar_fn_status_idx",
                fields=["function", "status"],
            ),
            models.Index(
                name="functions_ar_task_idx",
                fields=["task"],
            ),
            models.Index(
                name="functions_ar_run_status_idx",
                fields=["run_status"],
            ),
        ]

    def __str__(self) -> str:
        return f"AnnotationRequest(id={self.pk}, function_id={self.function_id})"
