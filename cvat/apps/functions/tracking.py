from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Iterable

from django.db import transaction
from django.utils.translation import gettext_lazy as _
from rest_framework.exceptions import PermissionDenied, ValidationError

from cvat.apps.engine.models import Job, LabeledTrack, SourceType, TrackedShape
from cvat.apps.events.handlers import handle_annotations_change
from cvat.apps.lambda_manager.models import FunctionKind

from .models import (
    AnnotationRequest,
    AnnotationRequestCategory,
    AnnotationRequestStatus,
    Function,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _TrackingTarget:
    track: LabeledTrack
    shape: TrackedShape


def _collect_targets(*, job: Job, track_ids: Iterable[int], frame: int) -> list[_TrackingTarget]:
    track_id_list = list(track_ids)
    tracks = (
        LabeledTrack.objects.filter(job=job, pk__in=track_id_list)
        .prefetch_related("shapes")
        .order_by("id")
    )

    if tracks.count() != len(set(track_id_list)):
        raise ValidationError(detail={"track_ids": _("One or more tracks were not found in the job.")})

    targets: list[_TrackingTarget] = []
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
        targets.append(_TrackingTarget(track=track, shape=shape))
    return targets


def start_tracking_action(
    *,
    job: Job,
    function: Function,
    user,
    frame: int,
    target_frame: int,
    track_ids: Iterable[int],
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

    supported_shapes = set(function.supported_shape_types or [])
    targets = _collect_targets(job=job, track_ids=track_ids, frame=frame)

    if not supported_shapes:
        raise ValidationError("Tracker function must declare supported shape types.")

    shape_payloads: list[dict] = []
    tracking_targets: list[dict] = []
    for item in targets:
        if item.shape.type not in supported_shapes:
            raise ValidationError(
                detail={
                    "track_ids": _(
                        "Track %(track_id)s uses shape type %(shape_type)s which is not "
                        "supported by function %(function_name)s."
                    )
                    % {
                        "track_id": item.track.id,
                        "shape_type": item.shape.type,
                        "function_name": function.name,
                    }
                }
            )

        shape_payloads.append(
            {
                "type": item.shape.type,
                "points": list(item.shape.points),
            }
        )
        tracking_targets.append(
            {
                "track_id": item.track.id,
                "label_id": item.track.label_id,
                "shape_type": item.shape.type,
            }
        )

    run_id = str(uuid.uuid4())
    pending_frames = list(range(frame + 1, target_frame + 1))

    if not pending_frames:
        raise ValidationError(detail={"target_frame": _("Tracking range must include at least one frame.")})

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
    if not run_id or not tracking_targets:
        logger.debug("Tracking request %s missing run metadata", annotation_request.id)
        return

    start_frame = params.get("start_frame")
    target_frame = params.get("target_frame")
    if start_frame is None or target_frame is None:
        raise ValidationError("Tracking request parameters are incomplete.")

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

    job = annotation_request.job
    change_payload = {"version": 0, "tags": [], "shapes": [], "tracks": []}

    with transaction.atomic():
        for target_index, target in enumerate(tracking_targets):
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

            TrackedShape.objects.filter(
                track_id=track_id, frame__gt=start_frame, frame__lte=target_frame
            ).delete()

            new_shapes: list[TrackedShape] = []
            track_shapes_payload: list[dict] = []
            last_points = base_points
            for frame in range(start_frame + 1, target_frame + 1):
                frame_payload = frames_to_shapes.get(frame) or []
                shape_payload = (
                    frame_payload[target_index] if target_index < len(frame_payload or []) else None
                )
                if shape_payload:
                    points = shape_payload.get("points") or last_points
                    outside = False
                    last_points = points
                else:
                    points = last_points
                    outside = True

                if points is None:
                    points = []

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

            if new_shapes:
                TrackedShape.objects.bulk_create(new_shapes)

            change_payload["tracks"].append(
                {
                    "id": track_id,
                    "label_id": target["label_id"],
                    "frame": start_frame,
                    "group": None,
                    "source": SourceType.AUTO.value,
                    "attributes": [],
                    "shapes": track_shapes_payload,
                }
            )

        job.touch()

    handle_annotations_change(job, change_payload, "update")
