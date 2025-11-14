from __future__ import annotations

import itertools
import json
import uuid
from urllib.parse import quote

from unittest import mock

from django.core.management import call_command
from rest_framework import status

import cvat.apps.dataset_manager as dm
from cvat.apps.engine.models import (
    Data,
    Job,
    Label,
    LabeledTrack,
    Segment,
    SourceType,
    Task,
    TrackedShape,
    User,
)
from cvat.apps.engine.tests.utils import ApiTestBase
from cvat.apps.functions.models import (
    AnnotationRequest,
    AnnotationRequestCategory,
    AnnotationRequestStatus,
    Function,
    FunctionProvider,
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
    ) -> Function:
        return Function.objects.create(
            owner=owner,
            name="SAM2",
            description="",
            provider=FunctionProvider.NATIVE,
            kind=kind,
            supported_shape_types=supported_shape_types or ["rectangle"],
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
    ) -> AnnotationRequest:
        return AnnotationRequest.objects.create(
            function=function,
            owner=function.owner,
            task=self.task,
            category=category,
            type=req_type,
            status=status,
            progress=progress,
            parameters=parameters
            or {
                "task": self.task.id,
                "frame": 0,
                "type": "init_tracking",
                "shapes": [],
            },
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

        base_parameters = {
            "task": self.task.id,
            "frame": 0,
            "type": "init_tracking",
            "shapes": [],
            "function_run_id": str(run_id),
        }

        request = self._create_annotation_request(
            function=function,
            status=AnnotationRequestStatus.PENDING,
            parameters=base_parameters,
        )

        response = self._get_request(f"/api/functions/runs/{run_id}", self.owner)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        payload = response.json()
        self.assertEqual(payload["status"], AnnotationRequestStatus.RUNNING)

        request.status = AnnotationRequestStatus.DONE
        request.progress = 1.0
        request.save()

        response = self._get_request(f"/api/functions/runs/{run_id}", self.owner)
        payload = response.json()
        self.assertEqual(payload["status"], AnnotationRequestStatus.DONE)
        self.assertEqual(payload["progress"], 1.0)

        self._create_annotation_request(
            function=function,
            status=AnnotationRequestStatus.FAILED,
            parameters={**base_parameters, "frame": 1},
        )

        response = self._get_request(f"/api/functions/runs/{run_id}", self.owner)
        payload = response.json()
        self.assertEqual(payload["status"], AnnotationRequestStatus.FAILED)
        self.assertAlmostEqual(payload["progress"], 0.5)

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
