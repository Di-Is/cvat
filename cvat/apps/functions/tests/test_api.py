from __future__ import annotations

import contextlib
import itertools
import json
import uuid
from datetime import timedelta
from unittest import mock
from urllib.parse import quote

from django.core.management import call_command
from rest_framework import status

import cvat.apps.dataset_manager as dm
from cvat.apps.engine.models import (
    Data,
    Job,
    Label,
    LabeledShape,
    LabeledTrack,
    Segment,
    SourceType,
    Task,
    TrackedShape,
    User,
)
from cvat.apps.engine.tests.utils import ApiTestBase
from cvat.apps.functions import interactors
from cvat.apps.functions.models import (
    AnnotationRequest,
    AnnotationRequestCategory,
    AnnotationRequestStatus,
    Function,
    FunctionProvider,
    FunctionRunStatus,
)
from cvat.apps.lambda_manager.models import FunctionKind


class FunctionsApiTests(ApiTestBase):
    @classmethod
    def setUpTestData(cls) -> None:
        super().setUpTestData()
        cls.owner = User.objects.create_user(username="function-owner", password="pass")
        cls.other_user = User.objects.create_user(username="function-other", password="pass")

        cls.data = Data.objects.create(chunk_size=1, size=0, deleted_frames=[])
        cls.task = Task.objects.create(
            name="demo-task",
            mode="interpolation",
            segment_size=0,
            owner=cls.owner,
            data=cls.data,
        )
        cls.label = Label.objects.create(name="object", task=cls.task)
        cls.segment = Segment.objects.create(task=cls.task, start_frame=0, stop_frame=9)
        cls.job = Job.objects.create(segment=cls.segment, assignee=cls.owner)

    def _create_function(
        self,
        owner: User,
        *,
        supported_shape_types: list[str] | None = None,
        kind: str = FunctionKind.TRACKER,
        supports_batched_tracker: bool = False,
    ) -> Function:
        return Function.objects.create(
            owner=owner,
            name="SAM2",
            description="",
            provider=FunctionProvider.NATIVE,
            kind=kind,
            supported_shape_types=supported_shape_types or ["rectangle"],
            supports_batched_tracker=supports_batched_tracker,
        )

    def _create_annotation_request(
        self,
        *,
        function: Function,
        category: str = AnnotationRequestCategory.BATCH,
        req_type: str = "annotate_task",
        status: str = AnnotationRequestStatus.PENDING,
        progress: float = 0.0,
        parameters: dict | None = None,
        run_status: FunctionRunStatus | None = None,
    ) -> AnnotationRequest:
        return AnnotationRequest.objects.create(
            function=function,
            owner=function.owner,
            task=self.task,
            job=self.job,
            category=category,
            type=req_type,
            status=status,
            progress=progress,
            run_status=run_status,
            parameters=parameters
            or {
                "task": self.task.id,
                "frame": 0,
                "type": "init_tracking",
                "shapes": [],
            },
        )

    def _create_run_status(
        self,
        *,
        function: Function,
        run_id: uuid.UUID,
        status: str = AnnotationRequestStatus.PENDING,
        total_requests: int = 1,
        completed_requests: int = 0,
        failed_requests: int = 0,
        cancelled_requests: int = 0,
        expected_frames: int | None = None,
        completed_frames: int = 0,
        progress: float = 0.0,
        active_request_id: uuid.UUID | None = None,
        active_request_type: str = "",
        active_request_progress: float = 0.0,
        active_request_frame_span: int = 0,
        failed_request_id: uuid.UUID | None = None,
    ) -> FunctionRunStatus:
        return FunctionRunStatus.objects.create(
            run_id=run_id,
            owner=function.owner,
            function=function,
            task=self.task,
            job=self.job,
            status=status,
            total_requests=total_requests,
            completed_requests=completed_requests,
            failed_requests=failed_requests,
            cancelled_requests=cancelled_requests,
            expected_frames=expected_frames,
            completed_frames=completed_frames,
            progress=progress,
            active_request_id=active_request_id,
            active_request_type=active_request_type,
            active_request_progress=active_request_progress,
            active_request_frame_span=active_request_frame_span,
            failed_request_id=failed_request_id,
        )

    def test_list_functions_filters_by_owner(self):
        owned_function = self._create_function(self.owner)
        self._create_function(self.other_user)

        response = self._get_request("/api/functions", self.owner)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        payload = response.json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["results"][0]["id"], owned_function.id)

    def test_list_functions_can_filter_by_kind(self):
        tracker = self._create_function(self.owner, kind=FunctionKind.TRACKER)
        self._create_function(self.owner, kind=FunctionKind.DETECTOR)

        filter_payload = json.dumps({
            "and": [{"==": [{"var": "kind"}, FunctionKind.TRACKER]}],
        })
        response = self._get_request(f"/api/functions?filter={quote(filter_payload)}", self.owner)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        payload = response.json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["results"][0]["id"], tracker.id)

    def test_create_detection_function_with_labels(self):
        payload = {
            "provider": FunctionProvider.NATIVE,
            "name": "Detector",
            "kind": FunctionKind.DETECTOR,
            "labels_v2": [
                {
                    "name": "object",
                    "type": "any",
                    "attributes": [
                        {"name": "confidence", "input_type": "number", "values": ["0", "1"]}
                    ],
                    "sublabels": [],
                }
            ],
        }

        response = self._post_request("/api/functions", self.owner, data=payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.json()["labels_v2"][0]["name"], "object")

    def test_queue_acquire_complete_flow(self):
        function = self._create_function(self.owner)
        annotation_request = self._create_annotation_request(function=function)

        acquire_response = self._post_request(
            f"/api/functions/queues/function:{function.id}/requests/acquire",
            self.owner,
            data={
                "agent_id": "agent-1",
                "request_category": AnnotationRequestCategory.BATCH,
            },
        )
        self.assertEqual(acquire_response.status_code, status.HTTP_200_OK)
        assignment = acquire_response.json()["ar_assignment"]
        self.assertIsNotNone(assignment)
        self.assertEqual(assignment["ar_id"], str(annotation_request.id))

        annotation_request.refresh_from_db()
        self.assertEqual(annotation_request.status, AnnotationRequestStatus.RUNNING)
        self.assertEqual(annotation_request.agent_id, "agent-1")

        acquire_again = self._post_request(
            f"/api/functions/queues/function:{function.id}/requests/acquire",
            self.owner,
            data={
                "agent_id": "agent-1",
                "request_category": AnnotationRequestCategory.BATCH,
            },
        )
        self.assertIsNone(acquire_again.json()["ar_assignment"])

        complete_response = self._post_request(
            f"/api/functions/queues/function:{function.id}/requests/{annotation_request.id}/complete",
            self.owner,
            data={
                "agent_id": "agent-1",
                "annotations": {"tags": []},
            },
        )
        self.assertEqual(complete_response.status_code, status.HTTP_200_OK)
        annotation_request.refresh_from_db()
        self.assertEqual(annotation_request.status, AnnotationRequestStatus.DONE)
        self.assertEqual(annotation_request.result["annotations"], {"tags": []})
        self.assertEqual(annotation_request.progress, 1.0)

    def test_queue_update_and_fail(self):
        function = self._create_function(self.owner)
        annotation_request = self._create_annotation_request(function=function)

        self._post_request(
            f"/api/functions/queues/function:{function.id}/requests/acquire",
            self.owner,
            data={
                "agent_id": "agent-2",
                "request_category": AnnotationRequestCategory.BATCH,
            },
        )

        update_response = self._post_request(
            f"/api/functions/queues/function:{function.id}/requests/{annotation_request.id}/update",
            self.owner,
            data={
                "agent_id": "agent-2",
                "progress": 0.5,
            },
        )
        self.assertEqual(update_response.status_code, status.HTTP_200_OK)

        fail_response = self._post_request(
            f"/api/functions/queues/function:{function.id}/requests/{annotation_request.id}/fail",
            self.owner,
            data={
                "agent_id": "agent-2",
                "exc_info": "network error",
            },
        )
        self.assertEqual(fail_response.status_code, status.HTTP_200_OK)

        annotation_request.refresh_from_db()
        self.assertEqual(annotation_request.status, AnnotationRequestStatus.FAILED)
        self.assertEqual(annotation_request.result["exc_info"], "network error")

    def test_request_detail_visible_to_owner(self):
        function = self._create_function(self.owner)
        annotation_request = self._create_annotation_request(function=function)

        response = self._get_request(
            f"/api/functions/requests/{annotation_request.id}",
            self.owner,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        payload = response.json()
        self.assertEqual(payload["id"], str(annotation_request.id))
        self.assertEqual(payload["status"], AnnotationRequestStatus.PENDING)

    def test_request_detail_denied_for_other_owner(self):
        function = self._create_function(self.owner)
        annotation_request = self._create_annotation_request(function=function)

        response = self._get_request(
            f"/api/functions/requests/{annotation_request.id}",
            self.other_user,
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_run_status_endpoint_reports_progress(self):
        function = self._create_function(self.owner)
        run_id = uuid.uuid4()
        summary = self._create_run_status(
            function=function,
            run_id=run_id,
            status=AnnotationRequestStatus.RUNNING,
            total_requests=2,
            completed_requests=0,
            progress=0.25,
        )

        response = self._get_request(f"/api/functions/runs/{run_id}", self.owner)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        payload = response.json()
        self.assertEqual(payload["status"], AnnotationRequestStatus.RUNNING)

        summary.status = AnnotationRequestStatus.DONE
        summary.completed_requests = 2
        summary.progress = 1.0
        summary.save(update_fields=["status", "completed_requests", "progress"])

        response = self._get_request(f"/api/functions/runs/{run_id}", self.owner)
        payload = response.json()
        self.assertEqual(payload["status"], AnnotationRequestStatus.DONE)
        self.assertEqual(payload["progress"], 1.0)

        summary.status = AnnotationRequestStatus.FAILED
        summary.failed_requests = 1
        summary.failed_request_id = uuid.uuid4()
        summary.progress = 0.5
        summary.save(
            update_fields=["status", "failed_requests", "failed_request_id", "progress"]
        )

        response = self._get_request(f"/api/functions/runs/{run_id}", self.owner)
        payload = response.json()
        self.assertEqual(payload["status"], AnnotationRequestStatus.FAILED)
        self.assertAlmostEqual(payload["progress"], 0.5)

    def test_run_status_endpoint_accounts_for_batched_tracking(self):
        function = self._create_function(self.owner)
        run_id = uuid.uuid4()
        active_request_id = uuid.uuid4()
        summary = self._create_run_status(
            function=function,
            run_id=run_id,
            status=AnnotationRequestStatus.RUNNING,
            expected_frames=6,
            completed_frames=5,
            total_requests=3,
            completed_requests=2,
            progress=5 / 6,
            active_request_id=active_request_id,
            active_request_type="track",
            active_request_progress=0.5,
            active_request_frame_span=2,
        )

        response = self._get_request(f"/api/functions/runs/{run_id}", self.owner)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        payload = response.json()
        self.assertEqual(payload["status"], AnnotationRequestStatus.RUNNING)
        self.assertAlmostEqual(payload["progress"], summary.progress)

    def test_run_status_endpoint_reports_cancelled(self):
        function = self._create_function(self.owner)
        run_id = uuid.uuid4()
        summary = self._create_run_status(
            function=function,
            run_id=run_id,
            status=AnnotationRequestStatus.CANCELLED,
            progress=0.4,
            total_requests=4,
            completed_requests=2,
            cancelled_requests=2,
        )

        response = self._get_request(f"/api/functions/runs/{run_id}", self.owner)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        payload = response.json()
        self.assertEqual(payload["status"], AnnotationRequestStatus.CANCELLED)
        self.assertEqual(payload["progress"], summary.progress)

    def test_run_cancel_endpoint_cancels_requests_and_rolls_back(self):
        function = self._create_function(self.owner)
        run_id = uuid.uuid4()
        summary = self._create_run_status(
            function=function,
            run_id=run_id,
            status=AnnotationRequestStatus.RUNNING,
            total_requests=2,
            completed_requests=0,
            progress=0.1,
        )
        base_parameters = {
            "task": self.task.id,
            "frame": 0,
            "type": "init_tracking",
            "shapes": [],
            "function_run_id": str(run_id),
        }

        pending_request = self._create_annotation_request(
            function=function,
            status=AnnotationRequestStatus.PENDING,
            parameters=base_parameters,
            run_status=summary,
        )
        running_request = self._create_annotation_request(
            function=function,
            status=AnnotationRequestStatus.RUNNING,
            parameters={**base_parameters, "frame": 1},
            run_status=summary,
        )

        track = LabeledTrack.objects.create(
            job=self.job,
            label=self.label,
            frame=0,
            group=None,
            source=SourceType.MANUAL.value,
        )
        tracked_shape = TrackedShape.objects.create(
            track=track,
            frame=1,
            type="rectangle",
            points=[0, 0, 1, 1],
            outside=False,
            occluded=False,
            z_order=0,
            rotation=0,
            function_run_id=run_id,
        )

        response = self._post_request(
            f"/api/functions/runs/{run_id}/cancel",
            self.owner,
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(response.json()["cancelled_requests"], 2)

        pending_request.refresh_from_db()
        running_request.refresh_from_db()
        self.assertEqual(pending_request.status, AnnotationRequestStatus.CANCELLED)
        self.assertEqual(running_request.status, AnnotationRequestStatus.CANCELLED)
        self.assertFalse(TrackedShape.objects.filter(pk=tracked_shape.id).exists())

    def test_run_cancel_endpoint_requires_owner(self):
        function = self._create_function(self.owner)
        run_id = uuid.uuid4()
        summary = self._create_run_status(
            function=function,
            run_id=run_id,
            status=AnnotationRequestStatus.RUNNING,
        )

        self._create_annotation_request(
            function=function,
            status=AnnotationRequestStatus.PENDING,
            parameters={
                "task": self.task.id,
                "frame": 0,
                "type": "init_tracking",
                "shapes": [],
                "function_run_id": str(run_id),
            },
            run_status=summary,
        )

        response = self._post_request(
            f"/api/functions/runs/{run_id}/cancel",
            self.other_user,
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_run_cancel_endpoint_conflict_for_completed_run(self):
        function = self._create_function(self.owner)
        run_id = uuid.uuid4()
        summary = self._create_run_status(
            function=function,
            run_id=run_id,
            status=AnnotationRequestStatus.DONE,
            total_requests=1,
            completed_requests=1,
            progress=1.0,
        )

        self._create_annotation_request(
            function=function,
            status=AnnotationRequestStatus.DONE,
            parameters={
                "task": self.task.id,
                "frame": 0,
                "type": "init_tracking",
                "shapes": [],
                "function_run_id": str(run_id),
            },
            run_status=summary,
        )

        response = self._post_request(
            f"/api/functions/runs/{run_id}/cancel",
            self.owner,
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_queue_watch_stream_emits_new_request_event(self):
        function = self._create_function(self.owner)
        self._create_annotation_request(function=function)

        response = self._get_request(
            f"/api/functions/queues/function:{function.id}/watch",
            self.owner,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        stream = iter(response.streaming_content)
        chunks = list(itertools.islice(stream, 3))
        close = getattr(stream, "close", None)
        if callable(close):
            close()
        self.assertTrue(any(b"event: newrequest" in chunk for chunk in chunks))

    def test_queue_watch_stream_closes_after_timeout(self):
        function = self._create_function(self.owner)
        self._create_annotation_request(function=function)

        ttl = timedelta(milliseconds=20)
        keepalive = timedelta(milliseconds=5)

        with mock.patch(
            "cvat.apps.functions.views._QUEUE_WATCH_STREAM_TTL", ttl
        ), mock.patch(
            "cvat.apps.functions.views._QUEUE_WATCH_KEEPALIVE_INTERVAL", keepalive
        ), mock.patch(
            "cvat.apps.functions.views.QUEUE_WATCH_POLL_INTERVAL", 0.01
        ), mock.patch(
            "cvat.apps.functions.views.close_old_connections"
        ) as mock_close_connections:
            response = self._get_request(
                f"/api/functions/queues/function:{function.id}/watch",
                self.owner,
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            stream = iter(response.streaming_content)
            list(stream)

        mock_close_connections.assert_called_once()

    @mock.patch("cvat.apps.functions.result_handlers.dm.task.patch_task_data")
    def test_queue_complete_applies_annotations(self, mock_patch_task_data):
        function = self._create_function(self.owner)
        annotation_request = self._create_annotation_request(function=function)

        self._post_request(
            f"/api/functions/queues/function:{function.id}/requests/acquire",
            self.owner,
            data={
                "agent_id": "agent-apply",
                "request_category": AnnotationRequestCategory.BATCH,
            },
        )

        response = self._post_request(
            f"/api/functions/queues/function:{function.id}/requests/{annotation_request.id}/complete",
            self.owner,
            data={
                "agent_id": "agent-apply",
                "annotations": {"tags": [], "shapes": [], "tracks": []},
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_patch_task_data.assert_called_once()
        args, kwargs = mock_patch_task_data.call_args
        self.assertEqual(args[0], annotation_request.task_id)
        self.assertEqual(args[2], dm.task.PatchAction.CREATE)

    def test_reset_annotation_request_command(self):
        function = self._create_function(self.owner)
        annotation_request = self._create_annotation_request(
            function=function,
            status=AnnotationRequestStatus.RUNNING,
            progress=0.5,
        )
        annotation_request.agent_id = "agent-reset"
        annotation_request.save(update_fields=["agent_id"])

        call_command("reset_annotation_request", str(annotation_request.id))

        annotation_request.refresh_from_db()
        self.assertEqual(annotation_request.status, AnnotationRequestStatus.PENDING)
        self.assertEqual(annotation_request.progress, 0.0)
        self.assertEqual(annotation_request.agent_id, "")

    def test_tracker_action_flow_updates_tracked_shapes(self):
        function = self._create_function(self.owner, supported_shape_types=["polygon"])
        track = self._create_track(frame=0)

        response = self._post_request(
            f"/api/jobs/{self.job.id}/functions/{function.id}/tracker-actions",
            self.owner,
            data={"frame": 0, "target_frame": 2, "track_ids": [track.id]},
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

        init_request = AnnotationRequest.objects.get(type="init_tracking")
        acquire_response = self._post_request(
            f"/api/functions/queues/function:{function.id}/requests/acquire",
            self.owner,
            data={
                "agent_id": "tracker-agent",
                "request_category": AnnotationRequestCategory.BATCH,
            },
        )
        self.assertEqual(acquire_response.status_code, status.HTTP_200_OK)
        assignment = acquire_response.json()["ar_assignment"]
        self.assertEqual(assignment["ar_id"], str(init_request.id))

        complete_init = self._post_request(
            f"/api/functions/queues/function:{function.id}/requests/{init_request.id}/complete",
            self.owner,
            data={"agent_id": "tracker-agent", "states": ["state-1"]},
        )
        self.assertEqual(complete_init.status_code, status.HTTP_200_OK)

        first_track_request = AnnotationRequest.objects.get(
            type="track", parameters__frame=1
        )
        acquire_track_one = self._post_request(
            f"/api/functions/queues/function:{function.id}/requests/acquire",
            self.owner,
            data={
                "agent_id": "tracker-agent",
                "request_category": AnnotationRequestCategory.BATCH,
            },
        )
        self.assertEqual(acquire_track_one.status_code, status.HTTP_200_OK)
        assignment = acquire_track_one.json()["ar_assignment"]
        self.assertEqual(assignment["ar_id"], str(first_track_request.id))
        complete_track_one = self._post_request(
            f"/api/functions/queues/function:{function.id}/requests/{first_track_request.id}/complete",
            self.owner,
            data={
                "agent_id": "tracker-agent",
                "states": ["state-1"],
                "shapes": [
                    {"type": "polygon", "points": [0, 0, 20, 0, 20, 20, 0, 20]},
                ],
            },
        )
        self.assertEqual(complete_track_one.status_code, status.HTTP_200_OK)

        second_track_request = AnnotationRequest.objects.get(
            type="track", parameters__frame=2
        )
        acquire_track_two = self._post_request(
            f"/api/functions/queues/function:{function.id}/requests/acquire",
            self.owner,
            data={
                "agent_id": "tracker-agent",
                "request_category": AnnotationRequestCategory.BATCH,
            },
        )
        self.assertEqual(acquire_track_two.status_code, status.HTTP_200_OK)
        assignment = acquire_track_two.json()["ar_assignment"]
        self.assertEqual(assignment["ar_id"], str(second_track_request.id))
        complete_track_two = self._post_request(
            f"/api/functions/queues/function:{function.id}/requests/{second_track_request.id}/complete",
            self.owner,
            data={
                "agent_id": "tracker-agent",
                "states": ["state-1"],
                "shapes": [None],
            },
        )
        self.assertEqual(complete_track_two.status_code, status.HTTP_200_OK)

        tracked_shapes = TrackedShape.objects.filter(track=track).order_by("frame")
        frames = {shape.frame: shape for shape in tracked_shapes}
        self.assertIn(1, frames)
        self.assertIn(2, frames)
        self.assertFalse(frames[1].outside)
        self.assertEqual(frames[1].points, [0, 0, 20, 0, 20, 20, 0, 20])
        self.assertTrue(frames[2].outside)
        self.assertEqual(frames[2].points, [0, 0, 20, 0, 20, 20, 0, 20])

    def test_tracker_action_appends_outside_keyframe_after_target(self):
        function = self._create_function(self.owner, supported_shape_types=["polygon"])
        track = self._create_track(frame=0)

        response = self._post_request(
            f"/api/jobs/{self.job.id}/functions/{function.id}/tracker-actions",
            self.owner,
            data={"frame": 0, "target_frame": 2, "track_ids": [track.id]},
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

        init_request = AnnotationRequest.objects.get(type="init_tracking")
        acquire_response = self._post_request(
            f"/api/functions/queues/function:{function.id}/requests/acquire",
            self.owner,
            data={
                "agent_id": "tracker-agent",
                "request_category": AnnotationRequestCategory.BATCH,
            },
        )
        self.assertEqual(acquire_response.status_code, status.HTTP_200_OK)
        assignment = acquire_response.json()["ar_assignment"]
        self.assertEqual(assignment["ar_id"], str(init_request.id))
        complete_init = self._post_request(
            f"/api/functions/queues/function:{function.id}/requests/{init_request.id}/complete",
            self.owner,
            data={"agent_id": "tracker-agent", "states": ["state-1"]},
        )
        self.assertEqual(complete_init.status_code, status.HTTP_200_OK)

        first_track_request = AnnotationRequest.objects.get(
            type="track", parameters__frame=1
        )
        acquire_track_one = self._post_request(
            f"/api/functions/queues/function:{function.id}/requests/acquire",
            self.owner,
            data={
                "agent_id": "tracker-agent",
                "request_category": AnnotationRequestCategory.BATCH,
            },
        )
        self.assertEqual(acquire_track_one.status_code, status.HTTP_200_OK)
        assignment = acquire_track_one.json()["ar_assignment"]
        self.assertEqual(assignment["ar_id"], str(first_track_request.id))
        complete_track_one = self._post_request(
            f"/api/functions/queues/function:{function.id}/requests/{first_track_request.id}/complete",
            self.owner,
            data={
                "agent_id": "tracker-agent",
                "states": ["state-1"],
                "shapes": [
                    {"type": "polygon", "points": [0, 0, 10, 0, 10, 10, 0, 10]},
                ],
            },
        )
        self.assertEqual(complete_track_one.status_code, status.HTTP_200_OK)

        second_track_request = AnnotationRequest.objects.get(
            type="track", parameters__frame=2
        )
        acquire_track_two = self._post_request(
            f"/api/functions/queues/function:{function.id}/requests/acquire",
            self.owner,
            data={
                "agent_id": "tracker-agent",
                "request_category": AnnotationRequestCategory.BATCH,
            },
        )
        self.assertEqual(acquire_track_two.status_code, status.HTTP_200_OK)
        assignment = acquire_track_two.json()["ar_assignment"]
        self.assertEqual(assignment["ar_id"], str(second_track_request.id))
        complete_track_two = self._post_request(
            f"/api/functions/queues/function:{function.id}/requests/{second_track_request.id}/complete",
            self.owner,
            data={
                "agent_id": "tracker-agent",
                "states": ["state-1"],
                "shapes": [
                    {"type": "polygon", "points": [0, 0, 10, 0, 10, 10, 0, 10]},
                ],
            },
        )
        self.assertEqual(complete_track_two.status_code, status.HTTP_200_OK)

        tracked_shapes = TrackedShape.objects.filter(track=track).order_by("frame")
        self.assertEqual([shape.frame for shape in tracked_shapes], [0, 1, 2, 2])
        self.assertFalse(tracked_shapes[1].outside)
        self.assertFalse(tracked_shapes[2].outside)
        self.assertTrue(tracked_shapes[3].outside)
        self.assertEqual(tracked_shapes[3].frame, 2)
        self.assertEqual(tracked_shapes[2].points, tracked_shapes[3].points)

    def test_tracker_action_removes_existing_shapes_beyond_target(self):
        function = self._create_function(self.owner, supported_shape_types=["polygon"])
        track = self._create_track(frame=0)
        TrackedShape.objects.create(
            track=track,
            frame=8,
            type="polygon",
            points=[0, 0, 5, 0, 5, 5, 0, 5],
            outside=False,
            occluded=False,
            z_order=0,
            rotation=0,
        )

        response = self._post_request(
            f"/api/jobs/{self.job.id}/functions/{function.id}/tracker-actions",
            self.owner,
            data={"frame": 0, "target_frame": 2, "track_ids": [track.id]},
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

        init_request = AnnotationRequest.objects.get(type="init_tracking")
        acquire_response = self._post_request(
            f"/api/functions/queues/function:{function.id}/requests/acquire",
            self.owner,
            data={
                "agent_id": "tracker-agent",
                "request_category": AnnotationRequestCategory.BATCH,
            },
        )
        self.assertEqual(acquire_response.status_code, status.HTTP_200_OK)
        assignment = acquire_response.json()["ar_assignment"]
        self.assertEqual(assignment["ar_id"], str(init_request.id))
        complete_init = self._post_request(
            f"/api/functions/queues/function:{function.id}/requests/{init_request.id}/complete",
            self.owner,
            data={"agent_id": "tracker-agent", "states": ["state-1"]},
        )
        self.assertEqual(complete_init.status_code, status.HTTP_200_OK)

        for frame, payload in (
            (1, {"type": "polygon", "points": [0, 0, 10, 0, 10, 10, 0, 10]}),
            (2, {"type": "polygon", "points": [0, 0, 12, 0, 12, 12, 0, 12]}),
        ):
            track_request = AnnotationRequest.objects.get(type="track", parameters__frame=frame)
            acquire_track = self._post_request(
                f"/api/functions/queues/function:{function.id}/requests/acquire",
                self.owner,
                data={
                    "agent_id": "tracker-agent",
                    "request_category": AnnotationRequestCategory.BATCH,
                },
            )
            self.assertEqual(acquire_track.status_code, status.HTTP_200_OK)
            assignment = acquire_track.json()["ar_assignment"]
            self.assertEqual(assignment["ar_id"], str(track_request.id))
            complete_track = self._post_request(
                f"/api/functions/queues/function:{function.id}/requests/{track_request.id}/complete",
                self.owner,
                data={
                    "agent_id": "tracker-agent",
                    "states": ["state-1"],
                    "shapes": [payload],
                },
            )
            self.assertEqual(complete_track.status_code, status.HTTP_200_OK)

        tracked_frames = list(
            TrackedShape.objects.filter(track=track).order_by("frame").values_list("frame", flat=True)
        )
        self.assertEqual(tracked_frames, [0, 1, 2, 2])

    def test_tracker_action_batches_frames_when_requested(self):
        function = self._create_function(
            self.owner,
            supported_shape_types=["polygon"],
            supports_batched_tracker=True,
        )
        track = self._create_track(frame=0)

        response = self._post_request(
            f"/api/jobs/{self.job.id}/functions/{function.id}/tracker-actions",
            self.owner,
            data={"frame": 0, "target_frame": 3, "track_ids": [track.id], "batch_size": 2},
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

        init_request = AnnotationRequest.objects.get(type="init_tracking")
        acquire_response = self._post_request(
            f"/api/functions/queues/function:{function.id}/requests/acquire",
            self.owner,
            data={
                "agent_id": "tracker-agent",
                "request_category": AnnotationRequestCategory.BATCH,
            },
        )
        self.assertEqual(acquire_response.status_code, status.HTTP_200_OK)

        complete_init = self._post_request(
            f"/api/functions/queues/function:{function.id}/requests/{init_request.id}/complete",
            self.owner,
            data={"agent_id": "tracker-agent", "states": ["state-1"]},
        )
        self.assertEqual(complete_init.status_code, status.HTTP_200_OK)

        first_track_request = AnnotationRequest.objects.get(type="track", parameters__frame=1)
        self.assertEqual(first_track_request.parameters["frames"], [1, 2])
        self.assertEqual(first_track_request.parameters["pending_frames"], [3])

        acquire_track = self._post_request(
            f"/api/functions/queues/function:{function.id}/requests/acquire",
            self.owner,
            data={
                "agent_id": "tracker-agent",
                "request_category": AnnotationRequestCategory.BATCH,
            },
        )
        self.assertEqual(acquire_track.status_code, status.HTTP_200_OK)

        complete_first_chunk = self._post_request(
            f"/api/functions/queues/function:{function.id}/requests/{first_track_request.id}/complete",
            self.owner,
            data={
                "agent_id": "tracker-agent",
                "states": ["state-1"],
                "frames": [
                    {
                        "frame": 1,
                        "shapes": [{"type": "polygon", "points": [0, 0, 11, 0, 11, 11, 0, 11]}],
                    },
                    {
                        "frame": 2,
                        "shapes": [{"type": "polygon", "points": [0, 0, 12, 0, 12, 12, 0, 12]}],
                    },
                ],
            },
        )
        self.assertEqual(complete_first_chunk.status_code, status.HTTP_200_OK)

        second_track_request = AnnotationRequest.objects.get(type="track", parameters__frame=3)
        self.assertEqual(second_track_request.parameters["frames"], [3])

        acquire_second = self._post_request(
            f"/api/functions/queues/function:{function.id}/requests/acquire",
            self.owner,
            data={
                "agent_id": "tracker-agent",
                "request_category": AnnotationRequestCategory.BATCH,
            },
        )
        self.assertEqual(acquire_second.status_code, status.HTTP_200_OK)

        complete_second_chunk = self._post_request(
            f"/api/functions/queues/function:{function.id}/requests/{second_track_request.id}/complete",
            self.owner,
            data={
                "agent_id": "tracker-agent",
                "states": ["state-1"],
                "frames": [
                    {
                        "frame": 3,
                        "shapes": [{"type": "polygon", "points": [0, 0, 13, 0, 13, 13, 0, 13]}],
                    },
                ],
            },
        )
        self.assertEqual(complete_second_chunk.status_code, status.HTTP_200_OK)

        tracked_frames = list(
            TrackedShape.objects.filter(track=track).order_by("frame").values_list("frame", flat=True)
        )
        self.assertEqual(tracked_frames, [0, 1, 2, 3, 3])

    def test_tracker_action_accepts_explicit_frame_list(self):
        function = self._create_function(
            self.owner,
            supported_shape_types=["polygon"],
            supports_batched_tracker=True,
        )
        track = self._create_track(frame=0)

        response = self._post_request(
            f"/api/jobs/{self.job.id}/functions/{function.id}/tracker-actions",
            self.owner,
            data={
                "frame": 0,
                "target_frame": 5,
                "track_ids": [track.id],
                "frames": [0, 2, 5],
            },
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

        init_request = AnnotationRequest.objects.get(type="init_tracking")
        acquire_response = self._post_request(
            f"/api/functions/queues/function:{function.id}/requests/acquire",
            self.owner,
            data={
                "agent_id": "tracker-agent",
                "request_category": AnnotationRequestCategory.BATCH,
            },
        )
        self.assertEqual(acquire_response.status_code, status.HTTP_200_OK)

        complete_init = self._post_request(
            f"/api/functions/queues/function:{function.id}/requests/{init_request.id}/complete",
            self.owner,
            data={"agent_id": "tracker-agent", "states": ["state-1"]},
        )
        self.assertEqual(complete_init.status_code, status.HTTP_200_OK)

        first_track_request = AnnotationRequest.objects.get(type="track", parameters__frame=2)
        self.assertEqual(first_track_request.parameters["frames"], [2, 5])
        self.assertEqual(first_track_request.parameters["pending_frames"], [])

        self.assertEqual(first_track_request.parameters["frames"], [2, 5])
        self.assertEqual(first_track_request.parameters["pending_frames"], [])

        acquire_first = self._post_request(
            f"/api/functions/queues/function:{function.id}/requests/acquire",
            self.owner,
            data={
                "agent_id": "tracker-agent",
                "request_category": AnnotationRequestCategory.BATCH,
            },
        )
        self.assertEqual(acquire_first.status_code, status.HTTP_200_OK)
        complete_first = self._post_request(
            f"/api/functions/queues/function:{function.id}/requests/{first_track_request.id}/complete",
            self.owner,
            data={
                "agent_id": "tracker-agent",
                "states": ["state-1"],
                "frames": [
                    {
                        "frame": 2,
                        "shapes": [{"type": "polygon", "points": [0, 0, 11, 0, 11, 11, 0, 11]}],
                    },
                    {
                        "frame": 5,
                        "shapes": [{"type": "polygon", "points": [0, 0, 12, 0, 12, 12, 0, 12]}],
                    },
                ],
            },
        )
        self.assertEqual(complete_first.status_code, status.HTTP_200_OK)

        tracked_frames = list(
            TrackedShape.objects.filter(track=track).order_by("frame").values_list("frame", flat=True)
        )
        self.assertIn(2, tracked_frames)
        self.assertGreaterEqual(tracked_frames.count(5), 2)

    def test_tracker_action_rejects_invalid_frame_list(self):
        function = self._create_function(
            self.owner,
            supported_shape_types=["polygon"],
            supports_batched_tracker=True,
        )
        track = self._create_track(frame=0)

        response = self._post_request(
            f"/api/jobs/{self.job.id}/functions/{function.id}/tracker-actions",
            self.owner,
            data={
                "frame": 0,
                "target_frame": 5,
                "track_ids": [track.id],
                "frames": [0, 2, 4],
            },
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("frames", response.json())

    def test_tracker_action_accepts_polygon_shapes(self):
        function = self._create_function(self.owner, supported_shape_types=["polygon"])
        shape = self._create_shape(frame=0)

        response = self._post_request(
            f"/api/jobs/{self.job.id}/functions/{function.id}/tracker-actions",
            self.owner,
            data={
                "frame": 0,
                "target_frame": 1,
                "track_ids": [],
                "shapes": [
                    {
                        "id": shape.id,
                        "client_id": 1001,
                        "label_id": self.label.id,
                        "frame": 0,
                        "shape_type": "polygon",
                        "points": [0, 0, 10, 0, 10, 10, 0, 10],
                        "z_order": 0,
                        "rotation": 0,
                        "group": None,
                        "occluded": False,
                        "outside": False,
                        "source": SourceType.MANUAL.value,
                        "attributes": [],
                    }
                ],
            },
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

        init_request = AnnotationRequest.objects.get(type="init_tracking")
        tracking_targets = init_request.parameters["tracking_targets"]
        self.assertEqual(len(tracking_targets), 1)
        target = tracking_targets[0]
        self.assertEqual(target["kind"], "shape")
        self.assertEqual(target["original_shape_id"], shape.id)
        self.assertEqual(init_request.parameters["conversion_mode"], "inline")

    def test_tracker_propagates_updated_states_between_frames(self):
        function = self._create_function(self.owner, supported_shape_types=["polygon"])
        track = self._create_track(frame=0)

        response = self._post_request(
            f"/api/jobs/{self.job.id}/functions/{function.id}/tracker-actions",
            self.owner,
            data={"frame": 0, "target_frame": 2, "track_ids": [track.id]},
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

        init_request = AnnotationRequest.objects.get(type="init_tracking")
        acquire_response = self._post_request(
            f"/api/functions/queues/function:{function.id}/requests/acquire",
            self.owner,
            data={
                "agent_id": "tracker-agent",
                "request_category": AnnotationRequestCategory.BATCH,
            },
        )
        self.assertEqual(acquire_response.status_code, status.HTTP_200_OK)

        complete_init = self._post_request(
            f"/api/functions/queues/function:{function.id}/requests/{init_request.id}/complete",
            self.owner,
            data={"agent_id": "tracker-agent", "states": ["state-initial"]},
        )
        self.assertEqual(complete_init.status_code, status.HTTP_200_OK)

        first_track_request = AnnotationRequest.objects.get(type="track", parameters__frame=1)
        complete_track_one = self._post_request(
            f"/api/functions/queues/function:{function.id}/requests/{first_track_request.id}/complete",
            self.owner,
            data={
                "agent_id": "tracker-agent",
                "states": ["state-updated"],
                "shapes": [
                    {"type": "polygon", "points": [0, 0, 10, 0, 10, 10, 0, 10]},
                ],
            },
        )
        self.assertEqual(complete_track_one.status_code, status.HTTP_200_OK)

        second_track_request = AnnotationRequest.objects.get(type="track", parameters__frame=2)
        self.assertEqual(second_track_request.parameters["states"], ["state-updated"])

    @mock.patch("cvat.apps.functions.interactors.wait_for_interactor_request")
    @mock.patch("cvat.apps.functions.interactors.interactor_wait_slot")
    def test_run_native_interactor_returns_result(
        self,
        mock_wait_slot,
        mock_wait,
    ):
        mock_wait_slot.side_effect = lambda: contextlib.nullcontext()
        mock_wait.return_value = {"mask_rle": [1, 2, 3, 4], "bounds": [0, 0, 1, 1]}
        function = self._create_function(self.owner, kind=FunctionKind.INTERACTOR)

        response = self._post_request(
            f"/api/jobs/{self.job.id}/functions/{function.id}/interactions",
            self.owner,
            data={
                "frame": 0,
                "pos_points": [[10.0, 15.0]],
                "neg_points": [],
                "obj_bbox": [[0.0, 0.0], [1.0, 1.0]],
                "start_with_box": True,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        payload = response.json()
        self.assertEqual(payload["mask_rle"], [1, 2, 3, 4])
        self.assertEqual(payload["bounds"], [0, 0, 1, 1])
        requests = AnnotationRequest.objects.filter(
            function=function,
            category=AnnotationRequestCategory.INTERACTIVE,
        )
        self.assertEqual(requests.count(), 1)
        mock_wait.assert_called_once()

    @mock.patch("cvat.apps.functions.interactors.wait_for_interactor_request")
    @mock.patch("cvat.apps.functions.interactors.interactor_wait_slot")
    def test_run_native_interactor_timeout_returns_504(
        self,
        mock_wait_slot,
        mock_wait,
    ):
        mock_wait_slot.side_effect = lambda: contextlib.nullcontext()
        mock_wait.side_effect = interactors.InteractorRequestTimeoutError("timeout")
        function = self._create_function(self.owner, kind=FunctionKind.INTERACTOR)

        response = self._post_request(
            f"/api/jobs/{self.job.id}/functions/{function.id}/interactions",
            self.owner,
            data={
                "frame": 0,
                "pos_points": [[10.0, 15.0]],
                "neg_points": [],
                "obj_bbox": [[0.0, 0.0], [1.0, 1.0]],
            },
        )

        self.assertEqual(response.status_code, status.HTTP_504_GATEWAY_TIMEOUT)

    def _create_shape(self, *, frame: int = 0) -> LabeledShape:
        return LabeledShape.objects.create(
            job=self.job,
            label=self.label,
            frame=frame,
            group=0,
            source=SourceType.MANUAL.value,
            type="polygon",
            points=[0, 0, 10, 0, 10, 10, 0, 10],
            rotation=0,
            z_order=0,
            occluded=False,
            outside=False,
        )

    def _create_track(self, *, frame: int = 0) -> LabeledTrack:
        track = LabeledTrack.objects.create(
            job=self.job,
            label=self.label,
            frame=frame,
            group=0,
            source=SourceType.MANUAL.value,
        )
        TrackedShape.objects.create(
            track=track,
            frame=frame,
            type="polygon",
            points=[0, 0, 10, 0, 10, 10, 0, 10],
            outside=False,
            occluded=False,
            z_order=0,
            rotation=0,
        )
        return track
