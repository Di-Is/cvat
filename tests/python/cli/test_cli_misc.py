# Copyright (C) CVAT.ai Corporation
#
# SPDX-License-Identifier: MIT

import json
import logging
import os
from datetime import timedelta
from io import BytesIO
from pathlib import Path
from unittest import mock

import packaging.version as pv
import PIL.Image
import pytest
import cvat_sdk.auto_annotation as cvataa
from cvat_cli._internal.agent import (
    _Event,
    _InteractorFunctionContextImpl,
    _NewReconnectionDelay,
    _parse_event_stream,
    _serialize_mask_prediction,
    _worker_job_interact,
)
from cvat_sdk import Client
from cvat_sdk.api_client import ApiClient
from cvat_sdk.api_client import models
from cvat_sdk.exceptions import ApiException
from cvat_sdk.core.proxies.tasks import ResourceType

from .util import TestCliBase, generate_images, https_reverse_proxy, run_cli


class TestCliMisc(TestCliBase):
    def test_can_warn_on_mismatching_server_version(self, monkeypatch, caplog):
        def mocked_version(_):
            return pv.Version("0")

        # We don't actually run a separate process in the tests here, so it works
        monkeypatch.setattr(Client, "get_server_version", mocked_version)

        self.run_cli("task", "ls")

        assert "Server version '0' is not compatible with SDK version" in caplog.text

    @pytest.mark.parametrize("verify", [True, False])
    def test_can_control_ssl_verification_with_arg(self, verify: bool):
        with https_reverse_proxy() as proxy_url:
            if verify:
                insecure_args = []
            else:
                insecure_args = ["--insecure"]

            run_cli(
                self,
                f"--auth={self.user}:{self.password}",
                f"--server-host={proxy_url}",
                *insecure_args,
                "task",
                "ls",
                expected_code=1 if verify else 0,
            )
            stdout = self.stdout.getvalue()

        if not verify:
            for line in stdout.splitlines():
                int(line)

    def test_can_control_organization_context(self):
        org = "cli-test-org"
        self.client.organizations.create(models.OrganizationWriteRequest(org))

        files = generate_images(self.tmp_path, 1)

        stdout = self.run_cli(
            "task",
            "create",
            "personal_task",
            ResourceType.LOCAL.name,
            *map(os.fspath, files),
            "--labels=" + json.dumps([{"name": "person"}]),
            "--completion_verification_period=0.01",
            organization="",
        )

        personal_task_id = int(stdout.split()[-1])

        stdout = self.run_cli(
            "task",
            "create",
            "org_task",
            ResourceType.LOCAL.name,
            *map(os.fspath, files),
            "--labels=" + json.dumps([{"name": "person"}]),
            "--completion_verification_period=0.01",
            organization=org,
        )

        org_task_id = int(stdout.split()[-1])

        personal_task_ids = list(map(int, self.run_cli("task", "ls", organization="").split()))
        assert personal_task_id in personal_task_ids
        assert org_task_id not in personal_task_ids

        org_task_ids = list(map(int, self.run_cli("task", "ls", organization=org).split()))
        assert personal_task_id not in org_task_ids
        assert org_task_id in org_task_ids

        all_task_ids = list(map(int, self.run_cli("task", "ls").split()))
        assert personal_task_id in all_task_ids
        assert org_task_id in all_task_ids

    def test_can_use_access_token_env_variable(
        self, monkeypatch: pytest.MonkeyPatch, access_tokens
    ):
        token = next(t for t in access_tokens)["private_key"]

        from cvat_sdk.api_client.rest import RESTClientObject

        original_request = RESTClientObject.request

        calls = 0

        def patched_request(self, *args, **kwargs):
            nonlocal calls
            calls += 1

            assert kwargs["headers"].get("Authorization") == f"Bearer {token}"
            return original_request(self, *args, **kwargs)

        monkeypatch.setenv("CVAT_ACCESS_TOKEN", token)
        monkeypatch.setattr(RESTClientObject, "request", patched_request)
        self.run_cli("task", "ls", authenticate=False)

        assert calls

    def test_can_use_current_user_env_variable(self, monkeypatch: pytest.MonkeyPatch):
        # set all user env vars supported by getuser()
        for env_var in ("LOGNAME", "USER", "LNAME", "USERNAME"):
            monkeypatch.setenv(env_var, self.user)

        from getpass import getuser as original_getuser

        from cvat_cli._internal.common import default_auth_factory

        with (
            mock.patch(
                "cvat_cli._internal.common.default_auth_factory", wraps=default_auth_factory
            ) as mock_auth_factory,
            mock.patch("getpass.getuser", wraps=original_getuser) as mock_getuser,
            mock.patch("getpass.getpass", return_value=self.password) as mock_getpass,
        ):
            self.run_cli("task", "ls", authenticate=False)

        mock_auth_factory.assert_called_once()
        mock_getuser.assert_called()
        mock_getpass.assert_called_once()

    def test_can_use_pass_env_variable(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("PASS", self.password)

        from getpass import getuser as original_getpass

        from cvat_cli._internal.common import default_auth_factory

        with (
            mock.patch(
                "cvat_cli._internal.common.default_auth_factory", wraps=default_auth_factory
            ) as mock_auth_factory,
            mock.patch("getpass.getpass", wraps=original_getpass) as mock_getpass,
        ):
            self.run_cli(f"--auth={self.user}", "task", "ls", authenticate=False)

        mock_auth_factory.assert_called_once()
        mock_getpass.assert_not_called()

    def test_create_native_reports_missing_functions_api(self, monkeypatch, caplog):
        function_file = Path(__file__).with_name("example_function.py")
        original_call_api = ApiClient.call_api

        def fake_call_api(self, resource_path, method, *args, **kwargs):
            if resource_path == "/api/functions" and method == "POST":
                raise ApiException(status=404)
            return original_call_api(self, resource_path, method, *args, **kwargs)

        monkeypatch.setattr(ApiClient, "call_api", fake_call_api)
        caplog.set_level(logging.CRITICAL, logger="cvat_cli.__main__")

        self.run_cli(
            "function",
            "create-native",
            "sam2",
            "--function-file",
            str(function_file),
            expected_code=1,
        )

        assert "Creating native functions requires the native functions API" in caplog.text

    def test_run_agent_reports_missing_functions_api(self, monkeypatch, caplog):
        function_file = Path(__file__).with_name("example_function.py")
        original_call_api = ApiClient.call_api

        def fake_call_api(self, resource_path, method, *args, **kwargs):
            if resource_path.startswith("/api/functions/") and method == "GET":
                raise ApiException(status=404)
            return original_call_api(self, resource_path, method, *args, **kwargs)

        monkeypatch.setattr(ApiClient, "call_api", fake_call_api)
        caplog.set_level(logging.CRITICAL, logger="cvat_cli.__main__")

        self.run_cli(
            "function",
            "run-agent",
            "1",
            "--function-file",
            str(function_file),
            expected_code=1,
        )

        assert "Running native function agents requires the native functions API" in caplog.text

    def test_run_agent_accepts_cache_limits(self, monkeypatch):
        import cvat_cli._internal.agent as agent_module

        captured: dict[str, int | bool] = {}

        def fake_run_agent(*args, **kwargs):
            captured["burst"] = kwargs["burst"]
            captured["max_with"] = kwargs["max_tasks_with_chunks"]
            captured["max_without"] = kwargs["max_tasks_without_chunks"]

        monkeypatch.setattr(agent_module, "run_agent", fake_run_agent)
        function_file = Path(__file__).with_name("example_function.py")

        self.run_cli(
            "function",
            "run-agent",
            "2",
            "--function-file",
            str(function_file),
            "--max-cache-tasks-with-chunks",
            "3",
            "--max-cache-tasks-without-chunks",
            "7",
        )

        assert captured["burst"] is False
        assert captured["max_with"] == 3
        assert captured["max_without"] == 7

    def test_interactor_dataset_repository_caches(self, monkeypatch):
        import logging
        import types
        import cvat_cli._internal.agent as agent_module

        created: list[int] = []

        class DummyDataset:
            def __init__(
                self,
                client,
                task_id,
                load_annotations=False,
                media_download_policy=None,
            ):
                created.append(task_id)
                self.samples = []
                self.labels = []

        monkeypatch.setattr(agent_module.cvtads, "TaskDataset", DummyDataset)

        client = types.SimpleNamespace(logger=logging.getLogger("cvat_cli.test"))
        repo = agent_module._InteractorDatasetRepository(client)

        first = repo.get(10)
        second = repo.get(10)

        assert first is second
        assert created == [10]

        repo.discard(10)
        third = repo.get(10)

        assert third is not first
        assert created == [10, 10]

    def test_create_native_interactor_function(self, monkeypatch):
        function_file = Path(__file__).with_name("interactor_function.py")
        created_payloads: list[dict[str, object]] = []
        original_call_api = ApiClient.call_api

        def fake_call_api(self, resource_path, method, *args, **kwargs):
            if resource_path == "/api/functions" and method == "POST":
                created_payloads.append(kwargs.get("body", {}))
            return original_call_api(self, resource_path, method, *args, **kwargs)

        monkeypatch.setattr(ApiClient, "call_api", fake_call_api)

        stdout = self.run_cli(
            "function",
            "create-native",
            "sam2-interactor",
            "--function-file",
            str(function_file),
        )

        assert created_payloads
        payload = created_payloads[-1]
        assert payload["kind"] == "interactor"
        assert payload["min_pos_points"] == 2
        assert payload["min_neg_points"] == 0
        assert payload["startswith_box"] is True
        assert payload["startswith_box_optional"] is False
        assert payload["help_message"] == "Sample interactor"
        assert payload["animated_gif"].endswith("demo.gif")
        assert payload["version"] == 2

        function_id = int(stdout.strip().splitlines()[-1])
        try:
            self.client.api_client.call_api(
                "/api/functions/{function_id}",
                "DELETE",
                path_params={"function_id": function_id},
            )
        except ApiException:
            pass

    def test_worker_job_interact_serializes_prediction(self):
        import cvat_cli._internal.agent as agent_module

        class _DummyInteractor:
            spec = cvataa.InteractorFunctionSpec()

            def interact(self, context, image, prompt):
                assert context.task_id == 7
                assert prompt.positive_points
                mask = [[1, 0], [0, 1]]
                return cvataa.MaskPrediction(
                    mask=mask,
                    bounds=[0, 0, 2, 2],
                    points=[(0.0, 0.0)],
                )

        original_function = getattr(agent_module, "_current_function", None)
        agent_module._current_function = _DummyInteractor()
        try:
            context = _InteractorFunctionContextImpl(
                task_id=7,
                job_id=3,
                frame_index=5,
                job_frame_index=1,
                frame_name="frame_000001.jpg",
            )
            prompt = cvataa.InteractionPrompt(positive_points=[(0.0, 0.0)])
            image = PIL.Image.new("RGB", (2, 2), color="white")
            prediction = _worker_job_interact(context, image, prompt)
            payload = _serialize_mask_prediction(prediction)
        finally:
            agent_module._current_function = original_function

        assert payload["mask"] == [[1, 0], [0, 1]]
        assert payload["bounds"] == [0, 0, 2, 2]
        assert payload["points"] == [[0.0, 0.0]]

    def test_worker_tracking_supports_polygon_and_mask_inputs(self):
        import cvat_cli._internal.agent as agent_module

        class _DummyTracker:
            spec = cvataa.TrackingFunctionSpec(supported_shape_types={"polygon", "mask"})

            def preprocess_image(self, context, image):
                return image

            def init_tracking_state(self, context, pp_image, shape):
                assert context.original_shape_type == shape.type
                return {"type": shape.type, "points": list(shape.points)}

            def track(self, context, pp_image, state):
                assert context.original_shape_type == state["type"]
                return cvataa.TrackableShape(
                    type=state["type"],
                    points=[value + 1 for value in state["points"]],
                )

        original_function = getattr(agent_module, "_current_function", None)
        original_states = getattr(agent_module, "_tracking_states", None)
        original_generator = getattr(agent_module, "_tracking_state_id_generator", None)
        agent_module._current_function = _DummyTracker()
        agent_module._tracking_states = agent_module._TrackingStateContainer()
        id_iter = iter(["polygon-state", "mask-state"])
        agent_module._tracking_state_id_generator = lambda: next(id_iter)

        state_ids: list[str] = []
        predictions: list[cvataa.TrackableShape | None] = []
        image = PIL.Image.new("RGB", (6, 6), color="white")
        shapes = [
            cvataa.TrackableShape(type="polygon", points=[0.0, 0.0, 1.0, 1.0, 2.0, 2.0]),
            cvataa.TrackableShape(type="mask", points=[1.0, 0.0, 1.0, 0.0]),
        ]

        try:
            state_ids = agent_module._worker_job_init_tracking(11, image, shapes)
            predictions = agent_module._worker_job_track(11, image, state_ids)
        finally:
            agent_module._current_function = original_function
            agent_module._tracking_states = original_states
            agent_module._tracking_state_id_generator = original_generator

        assert state_ids == ["polygon-state", "mask-state"]
        assert [shape.type for shape in predictions] == ["polygon", "mask"]
        assert predictions[0] is not None and predictions[0].points[0] == shapes[0].points[0] + 1
        assert predictions[1] is not None and predictions[1].points[0] == shapes[1].points[0] + 1


@pytest.mark.parametrize(
    ["lines", "messages"],
    [
        # empty
        ([], []),
        ([""], [_Event("", "")]),
        # event only
        (["event: test", ""], [_Event("test", "")]),
        (["event: foo", "event: bar", ""], [_Event("bar", "")]),
        # data only
        (["data: test", ""], [_Event("", "test")]),
        (["data: foo", "data: bar", ""], [_Event("", "foo\nbar")]),
        # event and data
        (["event: test", "data: foo", "data: bar", ""], [_Event("test", "foo\nbar")]),
        (["data: foo", "event: test", "data: bar", ""], [_Event("test", "foo\nbar")]),
        (["data: foo", "data: bar", "event: test", ""], [_Event("test", "foo\nbar")]),
        # fields without values
        (["event: test", "event", ""], [_Event("", "")]),
        (["data: test", "data", ""], [_Event("", "test\n")]),
        # incomplete event
        (["event: test", "data: foo"], []),
        # multiple events
        (
            ["event: test1", "data: foo", "", "event: test2", "data: bar", ""],
            [_Event("test1", "foo"), _Event("test2", "bar")],
        ),
        # comments
        ([":"], []),
        ([":1", "event: test", ":2", "data: foo", ":3", ""], [_Event("test", "foo")]),
        # retry
        (["retry: 1234"], [_NewReconnectionDelay(timedelta(milliseconds=1234))]),
        (["retry", "retry:", "retry: a"], []),
        # no space
        (["event:test", "data:foo", ""], [_Event("test", "foo")]),
        # two spaces
        (["event:  test", "data:  foo", ""], [_Event(" test", " foo")]),
        # carriage return
        (["event: test\r", "data: foo\r", "\r"], [_Event("test", "foo")]),
    ],
)
def test_parse_event_stream(lines, messages):
    stream = BytesIO(b"".join(line.encode() + b"\n" for line in lines))
    assert list(_parse_event_stream(stream)) == messages
