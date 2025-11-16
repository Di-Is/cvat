from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any

from django.db import transaction
from rest_framework import serializers

from cvat.apps.lambda_manager.models import FunctionKind

from .models import (
    AnnotationRequest,
    AnnotationRequestCategory,
    Function,
    FunctionLabel,
    FunctionProvider,
)


class FunctionLabelV2Serializer(serializers.Serializer):
    name = serializers.CharField(max_length=256)
    type = serializers.CharField(required=False, default="any", allow_blank=True)
    attributes = serializers.ListField(child=serializers.DictField(), required=False, default=list)
    sublabels = serializers.ListField(child=serializers.DictField(), required=False, default=list)


class FunctionSerializer(serializers.ModelSerializer):
    labels_v2 = FunctionLabelV2Serializer(many=True, required=False)
    supported_shape_types = serializers.ListField(
        child=serializers.CharField(), required=False, allow_empty=True, default=list
    )
    min_pos_points = serializers.IntegerField(min_value=0, required=False)
    min_neg_points = serializers.IntegerField(required=False)
    startswith_box = serializers.BooleanField(required=False)
    startswith_box_optional = serializers.BooleanField(required=False)
    help_message = serializers.CharField(required=False, allow_blank=True)
    animated_gif = serializers.CharField(required=False, allow_blank=True)
    version = serializers.IntegerField(min_value=1, required=False)

    class Meta:
        model = Function
        fields = (
            "id",
            "name",
            "description",
            "provider",
            "kind",
            "supported_shape_types",
            "min_pos_points",
            "min_neg_points",
            "startswith_box",
            "startswith_box_optional",
            "help_message",
            "animated_gif",
            "version",
            "labels_v2",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "created_at", "updated_at")

    def validate_provider(self, value: str) -> str:
        if value != FunctionProvider.NATIVE:
            raise serializers.ValidationError("Only native provider is supported in OSS mode")
        return value

    def validate_kind(self, value: str) -> str:
        valid_kinds = {choice for choice, _ in FunctionKind.choices}
        if value not in valid_kinds:
            raise serializers.ValidationError("Unsupported function kind")
        return value

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        kind = attrs.get("kind") or getattr(self.instance, "kind", None)
        labels_value = attrs.get("labels_v2", serializers.empty)
        if labels_value is serializers.empty:
            has_labels = bool(self.instance and self.instance.labels.exists())
        else:
            has_labels = bool(labels_value)

        if kind == FunctionKind.DETECTOR and not has_labels:
            raise serializers.ValidationError({"labels_v2": "Detection functions require labels."})

        if kind == FunctionKind.INTERACTOR:
            min_pos = attrs.get("min_pos_points", getattr(self.instance, "min_pos_points", 0))
            min_neg = attrs.get("min_neg_points", getattr(self.instance, "min_neg_points", -1))
            if min_neg is not None and min_neg < -1:
                raise serializers.ValidationError({"min_neg_points": "Value must be >= -1."})
            if min_pos is not None and min_pos < 0:
                raise serializers.ValidationError({"min_pos_points": "Value must be >= 0."})
            version = attrs.get("version", getattr(self.instance, "version", 1))
            if version is not None and version < 1:
                raise serializers.ValidationError({"version": "Version must be positive."})
        return super().validate(attrs)

    def validate_labels_v2(self, value: list[dict[str, Any]]) -> list[dict[str, Any]]:
        names: set[str] = set()
        for payload in value:
            name = payload.get("name")
            if name in names:
                raise serializers.ValidationError("Label names must be unique.")
            names.add(name)
        return value

    def create(self, validated_data: dict[str, Any]) -> Function:
        labels_data = validated_data.pop("labels_v2", [])
        request = self.context["request"]
        function = Function.objects.create(owner=request.user, **validated_data)
        self._replace_labels(function, labels_data)
        return function

    def update(self, instance: Function, validated_data: dict[str, Any]) -> Function:
        labels_data = validated_data.pop("labels_v2", None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        if validated_data:
            instance.save(update_fields=list(validated_data.keys()))
        else:
            instance.save()
        if labels_data is not None:
            self._replace_labels(instance, labels_data)
        return instance

    def to_representation(self, instance: Function) -> dict[str, Any]:
        data = super().to_representation(instance)
        labels = instance.labels.order_by("position", "id")
        data["labels_v2"] = FunctionLabelV2Serializer(labels, many=True).data
        return data

    @staticmethod
    def _replace_labels(function: Function, labels_data: Iterable[dict[str, Any]]) -> None:
        with transaction.atomic():
            function.labels.all().delete()
            label_objects = [
                FunctionLabel(
                    function=function,
                    name=payload["name"],
                    label_type=payload.get("type", "any") or "any",
                    attributes=payload.get("attributes", []),
                    sublabels=payload.get("sublabels", []),
                    position=index,
                )
                for index, payload in enumerate(labels_data)
            ]
            if label_objects:
                FunctionLabel.objects.bulk_create(label_objects)


class AnnotationRequestAssignmentSerializer(serializers.Serializer):
    ar_id = serializers.CharField()
    ar_params = serializers.JSONField()


class AnnotationRequestAcquireSerializer(serializers.Serializer):
    agent_id = serializers.CharField(max_length=64)
    request_category = serializers.ChoiceField(choices=AnnotationRequestCategory.choices)


class AnnotationRequestAcquireResponseSerializer(serializers.Serializer):
    ar_assignment = AnnotationRequestAssignmentSerializer(allow_null=True)


class AnnotationRequestProgressSerializer(serializers.Serializer):
    agent_id = serializers.CharField(max_length=64)
    progress = serializers.FloatField(min_value=0.0, max_value=1.0)


class AnnotationRequestCompletionSerializer(serializers.Serializer):
    agent_id = serializers.CharField(max_length=64)

    def to_internal_value(self, data: dict[str, Any]) -> dict[str, Any]:
        if "agent_id" not in data:
            raise serializers.ValidationError({"agent_id": "This field is required."})
        result_payload = {key: value for key, value in data.items() if key != "agent_id"}
        return {"agent_id": data["agent_id"], "result_payload": result_payload}


class AnnotationRequestFailureSerializer(serializers.Serializer):
    agent_id = serializers.CharField(max_length=64)
    exc_info = serializers.CharField(allow_blank=True, required=False)


class TrackingActionRequestSerializer(serializers.Serializer):
    frame = serializers.IntegerField(min_value=0)
    target_frame = serializers.IntegerField(min_value=0)
    conversion_mode = serializers.ChoiceField(choices=["inline", "preconvert"], default="inline")
    track_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        allow_empty=True,
        required=False,
        default=list,
    )
    shapes = serializers.ListField(
        child=serializers.DictField(),
        required=False,
        allow_empty=True,
        default=list,
    )

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        frame = attrs["frame"]
        target_frame = attrs["target_frame"]
        if target_frame <= frame:
            raise serializers.ValidationError(
                {"target_frame": "Target frame must be greater than the start frame."}
            )

        track_ids = attrs["track_ids"]
        if len(track_ids) != len(set(track_ids)):
            raise serializers.ValidationError({"track_ids": "Track ids must be unique."})

        shapes = attrs.get("shapes", [])
        for index, shape in enumerate(shapes):
            if shape.get("frame") != frame:
                raise serializers.ValidationError(
                    {"shapes": f"Shape #{index} is not on frame {frame}."}
                )
            points = shape.get("points") or []
            if len(points) % 2:
                raise serializers.ValidationError(
                    {"shapes": f"Shape #{index} points must include an even number of coordinates."}
                )

        if not track_ids and not shapes:
            raise serializers.ValidationError(
                {"track_ids": "At least one track or shape must be provided for tracking."}
            )

        return attrs


class TrackingActionResponseSerializer(serializers.Serializer):
    run_id = serializers.CharField()
    initial_request_id = serializers.CharField()


class _PointField(serializers.ListField):
    def __init__(self, **kwargs):
        super().__init__(
            child=serializers.FloatField(),
            min_length=2,
            max_length=2,
            allow_empty=False,
            **kwargs,
        )


class InteractorActionRequestSerializer(serializers.Serializer):
    frame = serializers.IntegerField(min_value=0)
    pos_points = serializers.ListField(child=_PointField(), allow_empty=False)
    neg_points = serializers.ListField(child=_PointField(), allow_empty=True, required=False, default=list)
    obj_bbox = serializers.ListField(
        child=_PointField(),
        allow_empty=False,
        required=False,
        allow_null=True,
    )
    label_id = serializers.IntegerField(min_value=1, required=False, allow_null=True)
    start_with_box = serializers.BooleanField(required=False, default=False)

    def validate_obj_bbox(self, value: list[list[float]] | None) -> list[list[float]] | None:
        if value in (None, []):
            return None
        return value


class InteractorActionResponseSerializer(serializers.Serializer):
    mask = serializers.ListField(
        child=serializers.ListField(child=serializers.IntegerField()),
        required=False,
    )
    mask_rle = serializers.ListField(child=serializers.IntegerField(), required=False)
    bounds = serializers.ListField(child=serializers.IntegerField(), min_length=4, max_length=4, required=False)
    points = serializers.ListField(child=_PointField(), required=False)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if "mask" not in attrs and "mask_rle" not in attrs:
            raise serializers.ValidationError("Result payload must include a mask or mask_rle field.")
        return super().validate(attrs)


def serialize_assignment(ar: AnnotationRequest | None) -> dict[str, Any] | None:
    if ar is None:
        return None
    return {
        "ar_id": str(ar.id if isinstance(ar.id, uuid.UUID) else ar.id),
        "ar_params": ar.parameters,
    }


class AnnotationRequestDetailSerializer(serializers.ModelSerializer):
    function_id = serializers.IntegerField(source="function.id", read_only=True)

    class Meta:
        model = AnnotationRequest
        fields = (
            "id",
            "function_id",
            "status",
            "category",
            "type",
            "progress",
            "result",
            "parameters",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


class FunctionRunStatusSerializer(serializers.Serializer):
    run_id = serializers.CharField()
    status = serializers.CharField()
    progress = serializers.FloatField()
    active_request_id = serializers.CharField(allow_null=True)
    failed_request_id = serializers.CharField(allow_null=True)
    total_requests = serializers.IntegerField()
    completed_requests = serializers.IntegerField()
