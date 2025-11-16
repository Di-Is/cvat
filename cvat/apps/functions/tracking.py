from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Iterable, Literal, Any, Sequence

from django.db import transaction
from django.utils.translation import gettext_lazy as _
from rest_framework.exceptions import PermissionDenied, ValidationError

from cvat.apps.engine.models import Job, LabeledShape, LabeledTrack, SourceType, TrackedShape
from cvat.apps.events.handlers import handle_annotations_change
from cvat.apps.lambda_manager.models import FunctionKind

from . import telemetry

from .models import (
    AnnotationRequest,
    AnnotationRequestCategory,
    AnnotationRequestStatus,
    Function,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TrackingSubject:
    kind: Literal["track", "shape"]
    label_id: int
    shape_type: str
    initializer: dict[str, Any]
    track_id: int | None = None
    original_shape_id: int | None = None


def _append_outside_shape(
    *,
    track_shapes_payload: list[dict],
    new_shapes: list[TrackedShape],
    track_id: int,
    frame: int,
    shape_type: str,
    points: Sequence[float],
    z_order: int,
    function_run_uuid: uuid.UUID | None = None,
) -> None:
    """Append an outside keyframe to clamp track propagation at or before the target frame."""

    payload = {
        "frame": frame,
        "type": shape_type,
        "points": [float(value) for value in points],
        "outside": True,
        "occluded": False,
        "z_order": z_order,
        "rotation": 0,
        "attributes": [],
    }
    track_shapes_payload.append(payload)
    new_shapes.append(
        TrackedShape(
            track_id=track_id,
            frame=frame,
            type=shape_type,
            points=list(payload["points"]),
            outside=True,
            occluded=False,
            z_order=z_order,
            rotation=0,
            function_run_id=function_run_uuid,
        )
    )


def _build_track_subjects(
    *,
    job: Job,
    track_ids: Iterable[int],
    frame: int,
    supported_shapes: set[str],
) -> list[TrackingSubject]:
    track_id_list = list(track_ids)
    tracks = (
        LabeledTrack.objects.filter(job=job, pk__in=track_id_list)
        .prefetch_related("shapes")
        .order_by("id")
    )

    if tracks.count() != len(set(track_id_list)):
        raise ValidationError(detail={"track_ids": _("One or more tracks were not found in the job.")})

    subjects: list[TrackingSubject] = []
    for track in tracks:
        shape = (
            track.shapes.filter(frame=frame)
            .order_by("-id")
            .first()
        )
        if not shape or shape.outside:
            raise ValidationError(
                detail={
                    "frame": _(
                        "Track %(track_id)s does not have a visible keyframe at frame %(frame)d."
                    )
                    % {"track_id": track.id, "frame": frame}
                }
            )
        shape_type = shape.type.lower()
        if shape_type not in supported_shapes:
            raise ValidationError(
                detail={
                    "track_ids": _(
                        "Track %(track_id)s uses shape type %(shape_type)s which is not supported."
                    )
                    % {"track_id": track.id, "shape_type": shape.type}
                }
            )

        subjects.append(
            TrackingSubject(
                kind="track",
                track_id=track.id,
                label_id=track.label_id,
                shape_type=shape_type,
                initializer={
                    "frame": frame,
                    "points": list(shape.points),
                    "z_order": shape.z_order,
                    "rotation": shape.rotation,
                    "group": track.group,
                    "occluded": shape.occluded,
                    "outside": shape.outside,
                    "source": track.source,
                },
            )
        )
    return subjects


def _build_shape_subjects(
    *,
    job: Job,
    shapes: Iterable[dict[str, Any]],
    frame: int,
    supported_shapes: set[str],
) -> list[TrackingSubject]:
    task = job.segment.task
    label_ids = set(task.get_labels().values_list("id", flat=True))
    subjects: list[TrackingSubject] = []

    for index, shape in enumerate(shapes):
        try:
            label_id = int(shape["label_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError({"shapes": _(f"Shape #{index} is missing a valid label_id.")}) from exc

        if label_id not in label_ids:
            raise ValidationError({"shapes": _(f"Shape #{index} references an unknown label.")})

        shape_type = str(shape.get("shape_type") or "").lower()
        if shape_type not in supported_shapes:
            raise ValidationError(
                {"shapes": _(f"Shape #{index} uses unsupported shape type '{shape_type}'.")}
            )

        points = shape.get("points") or []
        if len(points) % 2:
            raise ValidationError({"shapes": _(f"Shape #{index} points must contain an even number of values.")})
        if shape_type == "polygon" and len(points) < 6:
            raise ValidationError({"shapes": _(f"Shape #{index} must contain at least 3 polygon points.")})

        try:
            z_order = int(shape.get("z_order", 0))
        except (TypeError, ValueError) as exc:
            raise ValidationError({"shapes": _(f"Shape #{index} has an invalid z_order.")}) from exc

        try:
            rotation = float(shape.get("rotation", 0.0))
        except (TypeError, ValueError) as exc:
            raise ValidationError({"shapes": _(f"Shape #{index} has an invalid rotation.")}) from exc

        group = shape.get("group")
        if group is not None:
            try:
                group = int(group)
            except (TypeError, ValueError) as exc:
                raise ValidationError({"shapes": _(f"Shape #{index} has an invalid group value.")}) from exc

        initializer = {
            "frame": frame,
            "points": [float(value) for value in points],
            "z_order": z_order,
            "rotation": rotation,
            "group": group,
            "occluded": bool(shape.get("occluded")),
            "outside": bool(shape.get("outside")),
            "source": shape.get("source") or SourceType.MANUAL.value,
            "attributes": shape.get("attributes") or [],
        }

        subjects.append(
            TrackingSubject(
                kind="shape",
                label_id=label_id,
                shape_type=shape_type,
                initializer=initializer,
                original_shape_id=shape.get("id"),
            ),
        )

    return subjects


def start_tracking_action(
    *,
    job: Job,
    function: Function,
    user,
    frame: int,
    target_frame: int,
    track_ids: Iterable[int] | None = None,
    shapes: Iterable[dict[str, Any]] | None = None,
    conversion_mode: str = "inline",
) -> dict[str, str]:
    if function.owner_id != user.id:
        raise PermissionDenied("You do not own this function.")

    if function.kind != FunctionKind.TRACKER:
        raise ValidationError(detail="Only tracker functions can be used for SAM2 actions.")

    segment = job.segment
    if frame < segment.start_frame or frame > segment.stop_frame:
        raise ValidationError(detail={"frame": _("Frame is outside the job range.")})
    if target_frame > segment.stop_frame:
        raise ValidationError(
            detail={"target_frame": _("Target frame must be within the job range.")}
        )

    supported_shapes = {shape.lower() for shape in (function.supported_shape_types or [])}

    if not supported_shapes:
        raise ValidationError("Tracker function must declare supported shape types.")

    normalized_track_ids = list(track_ids or [])
    shape_payloads_input = list(shapes or [])
    conversion_mode = (conversion_mode or "inline").lower()
    if conversion_mode not in {"inline", "preconvert"}:
        raise ValidationError({"conversion_mode": "Invalid conversion mode."})

    if not normalized_track_ids and not shape_payloads_input:
        raise ValidationError({"track_ids": "At least one track or shape must be provided."})

    tracker_supported_shapes = ",".join(sorted(supported_shapes)) if supported_shapes else None

    with telemetry.traced(
        "functions.tracking.start",
        function_id=function.id,
        job_id=job.id,
        frame=frame,
        target_frame=target_frame,
        requested_objects=len(normalized_track_ids) + len(shape_payloads_input),
        tracker_supported_shapes=tracker_supported_shapes,
    ) as span:
        track_subjects = _build_track_subjects(
            job=job,
            track_ids=normalized_track_ids,
            frame=frame,
            supported_shapes=supported_shapes,
        )
        shape_subjects = _build_shape_subjects(
            job=job,
            shapes=shape_payloads_input,
            frame=frame,
            supported_shapes=supported_shapes,
        )
        subjects = [*track_subjects, *shape_subjects]

        if not subjects:
            raise ValidationError({"track_ids": _("At least one subject must be provided.")})

        shape_payloads: list[dict] = []
        tracking_targets: list[dict] = []
        requested_shape_types: set[str] = set()
        for subject in subjects:
            requested_shape_types.add(subject.shape_type)
            shape_payloads.append(
                {
                    "type": subject.shape_type,
                    "points": list(subject.initializer.get("points") or []),
                }
            )
            tracking_targets.append(
                {
                    "kind": subject.kind,
                    "track_id": subject.track_id,
                    "label_id": subject.label_id,
                    "shape_type": subject.shape_type,
                    "initializer": subject.initializer,
                    "original_shape_id": subject.original_shape_id,
                }
            )

        run_id = str(uuid.uuid4())
        pending_frames = list(range(frame + 1, target_frame + 1))

        if not pending_frames:
            raise ValidationError(
                detail={"target_frame": _("Tracking range must include at least one frame.")}
            )

        if span:
            span.set_attribute(
                "cvat.tracker.pending_frames", len(pending_frames)
            )
            if requested_shape_types:
                span.set_attribute(
                    "cvat.tracker.input_shape_types",
                    ",".join(sorted(requested_shape_types)),
                )
            span.set_attribute("cvat.tracker.conversion_mode", conversion_mode)
            span.set_attribute("cvat.tracker.subject_counts.track", len(track_subjects))
            span.set_attribute("cvat.tracker.subject_counts.shape", len(shape_subjects))

        with transaction.atomic():
            annotation_request = AnnotationRequest.objects.create(
                function=function,
                owner=function.owner,
                task=job.segment.task,
                job=job,
                category=AnnotationRequestCategory.BATCH,
                type="init_tracking",
                parameters={
                    "type": "init_tracking",
                    "task": job.segment.task_id,
                    "job": job.id,
                    "frame": frame,
                    "start_frame": frame,
                    "target_frame": target_frame,
                    "pending_frames": pending_frames,
                    "shapes": shape_payloads,
                    "tracking_targets": tracking_targets,
                    "function_run_id": run_id,
                    "conversion_mode": conversion_mode,
                },
            )

    return {"run_id": run_id, "initial_request_id": str(annotation_request.id)}


def handle_request_completion(annotation_request: AnnotationRequest) -> None:
    if annotation_request.type == "init_tracking":
        _handle_tracking_init_completion(annotation_request)
    elif annotation_request.type == "track":
        _handle_tracking_track_completion(annotation_request)


def _handle_tracking_init_completion(annotation_request: AnnotationRequest) -> None:
    params = annotation_request.parameters or {}
    result_payload = annotation_request.result or {}
    states = result_payload.get("states")
    targets = params.get("tracking_targets") or []
    if not isinstance(states, list) or not states:
        raise ValidationError("Tracker init results must include states for all targets.")
    if len(states) != len(targets):
        raise ValidationError("Number of states does not match the number of tracked targets.")

    pending_frames = list(params.get("pending_frames", []))
    if not pending_frames:
        logger.debug(
            "Init tracking request %s completed with no pending frames", annotation_request.id
        )
        return

    _enqueue_track_request(
        template_request=annotation_request,
        next_frame=pending_frames[0],
        remaining_frames=pending_frames[1:],
        states=states,
        params=params,
    )


def _handle_tracking_track_completion(annotation_request: AnnotationRequest) -> None:
    params = annotation_request.parameters or {}
    pending_frames = list(params.get("pending_frames", []))
    result_payload = annotation_request.result or {}
    result_states = result_payload.get("states")
    if isinstance(result_states, list) and result_states:
        states = result_states
    else:
        states = params.get("states")
    if not isinstance(states, list) or not states:
        raise ValidationError("Tracker requests must carry persisted state identifiers.")
    targets = params.get("tracking_targets") or []
    if len(states) != len(targets):
        raise ValidationError("State metadata mismatch for tracking run.")

    if pending_frames:
        _enqueue_track_request(
            template_request=annotation_request,
            next_frame=pending_frames[0],
            remaining_frames=pending_frames[1:],
            states=states,
            params=params,
        )
    else:
        _apply_tracking_results(annotation_request)


def _enqueue_track_request(
    *,
    template_request: AnnotationRequest,
    next_frame: int,
    remaining_frames: list[int],
    states: list[str],
    params: dict,
) -> None:
    AnnotationRequest.objects.create(
        function=template_request.function,
        owner=template_request.owner,
        task=template_request.task,
        job=template_request.job,
        category=template_request.category,
        type="track",
        parameters={
            "type": "track",
            "task": params["task"],
            "job": params["job"],
            "frame": next_frame,
            "start_frame": params.get("start_frame", params["frame"]),
            "target_frame": params["target_frame"],
            "pending_frames": remaining_frames,
            "tracking_targets": params["tracking_targets"],
            "function_run_id": params["function_run_id"],
            "states": list(states),
        },
    )


def _apply_tracking_results(annotation_request: AnnotationRequest) -> None:
    params = annotation_request.parameters or {}
    run_id = params.get("function_run_id")
    tracking_targets = params.get("tracking_targets") or []
    conversion_mode = params.get("conversion_mode", "inline")
    if not run_id or not tracking_targets:
        logger.debug("Tracking request %s missing run metadata", annotation_request.id)
        return

    try:
        run_uuid = uuid.UUID(str(run_id))
    except (TypeError, ValueError):
        run_uuid = None

    start_frame = params.get("start_frame")
    target_frame = params.get("target_frame")
    if start_frame is None or target_frame is None:
        raise ValidationError("Tracking request parameters are incomplete.")
    tracked_objects = len(tracking_targets)
    tracker_shape_types = ",".join(
        sorted({str(target.get("shape_type")) for target in tracking_targets if target.get("shape_type")})
    )

    with telemetry.traced(
        "functions.tracking.apply",
        function_id=annotation_request.function_id,
        job_id=annotation_request.job_id,
        run_id=run_id,
        start_frame=start_frame,
        target_frame=target_frame,
        tracked_objects=tracked_objects,
    ) as span:
        if span and tracker_shape_types:
            span.set_attribute("cvat.tracker.target_shape_types", tracker_shape_types)

        if AnnotationRequest.objects.filter(
            parameters__function_run_id=str(run_id),
            status=AnnotationRequestStatus.CANCELLED,
        ).exists():
            logger.info("Skipping tracker apply for cancelled run %s", run_id)
            return

        track_requests = list(
            AnnotationRequest.objects.filter(
                function=annotation_request.function,
                type="track",
                status=AnnotationRequestStatus.DONE,
                parameters__function_run_id=run_id,
            ).order_by("parameters__frame")
        )

        if not track_requests:
            logger.warning("No completed track requests found for run %s", run_id)
            return

        frames_to_shapes: dict[int, list | None] = {}
        for req in track_requests:
            frame_number = req.parameters.get("frame")
            frames_to_shapes[frame_number] = (req.result or {}).get("shapes")

        if span:
            span.set_attribute("cvat.tracker.completed_track_requests", len(track_requests))

        job = annotation_request.job
        segment = job.segment
        created_tracks: list[dict] = []
        updated_tracks: list[dict] = []
        deleted_shapes: list[dict] = []

        with transaction.atomic():
            for target_index, target in enumerate(tracking_targets):
                subject_kind = target.get("kind", "track")
                initializer = target.get("initializer") or {}

                if subject_kind == "track":
                    track_id = target["track_id"]
                    base_shape = (
                        TrackedShape.objects.filter(track_id=track_id, frame=start_frame)
                        .order_by("-id")
                        .select_for_update()
                        .first()
                    )
                    if not base_shape:
                        raise ValidationError(
                            f"Track {track_id} does not contain a keyframe at frame {start_frame}."
                        )
                    base_points = list(base_shape.points)
                    base_z_order = base_shape.z_order
                    track_source = SourceType.AUTO.value
                    track_group = None
                else:
                    track = LabeledTrack.objects.create(
                        job=job,
                        label_id=target["label_id"],
                        frame=start_frame,
                        group=initializer.get("group"),
                        source=SourceType.AUTO.value if conversion_mode == "inline" else SourceType.MANUAL.value,
                    )
                    track_id = track.id
                    base_points = list(initializer.get("points") or [])
                    base_z_order = int(initializer.get("z_order", 0))
                    base_shape = TrackedShape.objects.create(
                        track=track,
                        frame=start_frame,
                        type=target["shape_type"],
                        points=base_points,
                        outside=bool(initializer.get("outside")),
                        occluded=bool(initializer.get("occluded")),
                        z_order=base_z_order,
                        rotation=float(initializer.get("rotation", 0)),
                        function_run_id=run_uuid,
                    )
                    track_source = track.source
                    track_group = initializer.get("group")
                    original_shape_id = target.get("original_shape_id")
                    if original_shape_id:
                        deleted = LabeledShape.objects.filter(pk=original_shape_id, job=job).delete()
                        if deleted[0]:
                            deleted_shapes.append(
                                {"id": original_shape_id, "type": target["shape_type"]}
                            )

                TrackedShape.objects.filter(track_id=track_id, frame__gte=start_frame).exclude(
                    pk=base_shape.id
                ).delete()

                new_shapes: list[TrackedShape] = []
                track_shapes_payload: list[dict] = []
                last_points = list(base_points)
                last_frame = start_frame
                last_outside = bool(initializer.get("outside"))
                for frame in range(start_frame + 1, target_frame + 1):
                    frame_payload = frames_to_shapes.get(frame) or []
                    shape_payload = (
                        frame_payload[target_index] if target_index < len(frame_payload or []) else None
                    )
                    if shape_payload:
                        points_source = shape_payload.get("points")
                        points = list(points_source) if points_source is not None else list(last_points)
                        outside = bool(shape_payload.get("outside"))
                    else:
                        points = list(last_points)
                        outside = True

                    if points is None:
                        points = []

                    points = list(points)

                    new_shapes.append(
                        TrackedShape(
                            track_id=track_id,
                            frame=frame,
                            type=target["shape_type"],
                            points=list(points),
                            outside=outside,
                            occluded=False,
                            z_order=base_z_order,
                            rotation=0,
                            function_run_id=run_uuid,
                        )
                    )
                    track_shapes_payload.append(
                        {
                            "frame": frame,
                            "type": target["shape_type"],
                            "points": list(points),
                            "outside": outside,
                            "occluded": False,
                            "z_order": base_z_order,
                            "rotation": 0,
                            "attributes": [],
                        }
                    )

                    last_points = list(points)
                    last_frame = frame
                    last_outside = outside

                should_terminate = segment.stop_frame > last_frame and not last_outside
                if should_terminate:
                    _append_outside_shape(
                        track_shapes_payload=track_shapes_payload,
                        new_shapes=new_shapes,
                        track_id=track_id,
                        frame=last_frame,
                        shape_type=target["shape_type"],
                        points=last_points,
                        z_order=base_z_order,
                        function_run_uuid=run_uuid,
                    )

                if new_shapes:
                    created_shapes = TrackedShape.objects.bulk_create(new_shapes)
                    for created_shape, payload in zip(created_shapes, track_shapes_payload[-len(created_shapes):]):
                        payload["id"] = created_shape.id

                if subject_kind == "shape":
                    track_shapes_payload.insert(
                        0,
                        {
                            "frame": start_frame,
                            "type": target["shape_type"],
                            "points": list(base_points),
                            "outside": bool(initializer.get("outside")),
                            "occluded": bool(initializer.get("occluded")),
                            "z_order": base_z_order,
                            "rotation": float(initializer.get("rotation", 0)),
                            "attributes": [],
                        },
                    )
                    track_shapes_payload[0]["id"] = base_shape.id

                track_payload = {
                    "id": track_id,
                    "label_id": target["label_id"],
                    "frame": start_frame,
                    "group": track_group,
                    "source": track_source,
                    "attributes": [],
                    "shapes": track_shapes_payload,
                }

                if subject_kind == "shape":
                    created_tracks.append(track_payload)
                else:
                    updated_tracks.append(track_payload)

            job.touch()

        if span:
            span.set_attribute("cvat.tracker.created_tracks", len(created_tracks))
            span.set_attribute("cvat.tracker.deleted_shapes", len(deleted_shapes))

        if updated_tracks:
            handle_annotations_change(job, {"tracks": updated_tracks}, "update")
        if created_tracks:
            handle_annotations_change(job, {"tracks": created_tracks}, "create")
        if deleted_shapes:
            handle_annotations_change(job, {"shapes": deleted_shapes}, "delete")


def rollback_tracking_run(run_id: str) -> None:
    if not run_id:
        return

    try:
        run_uuid = uuid.UUID(str(run_id))
    except (TypeError, ValueError):
        logger.debug("Rollback skipped for invalid run id %s", run_id)
        return

    deleted_shapes, _ = TrackedShape.objects.filter(function_run_id=run_uuid).delete()
    if deleted_shapes:
        logger.info("Rolled back %d tracked shapes for run %s", deleted_shapes, run_id)
