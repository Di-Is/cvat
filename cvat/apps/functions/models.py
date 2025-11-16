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
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["id"]
        indexes = [
            models.Index(fields=["owner"]),
            models.Index(fields=["kind"]),
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
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["function", "status"]),
            models.Index(fields=["task"]),
        ]

    def __str__(self) -> str:
        return f"AnnotationRequest(id={self.pk}, function_id={self.function_id})"
