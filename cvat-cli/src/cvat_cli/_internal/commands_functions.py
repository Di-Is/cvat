# Copyright (C) CVAT.ai Corporation
#
# SPDX-License-Identifier: MIT

import argparse
import json
import textwrap
from collections.abc import Sequence
from typing import Any, Optional, Union

import cvat_sdk.auto_annotation as cvataa
from cvat_sdk import Client, models
from cvat_sdk.exceptions import ApiException

from .agent import (
    FUNCTION_KIND_DETECTOR,
    FUNCTION_KIND_INTERACTOR,
    FUNCTION_KIND_TRACKER,
    FUNCTION_PROVIDER_NATIVE,
    run_agent,
)
from .command_base import CommandGroup
from .common import (
    FunctionLoader,
    configure_function_implementation_arguments,
    raise_if_functions_api_missing,
)

COMMANDS = CommandGroup(description="Perform operations on CVAT lambda functions.")


@COMMANDS.command_class("create-native")
class FunctionCreateNative:
    description = textwrap.dedent(
        """\
        Create a CVAT function that can be powered by an agent running the given local function.
        """
    )

    def configure_parser(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "name",
            help="a human-readable name for the function",
        )
        parser.add_argument(
            "--supports-batched-tracker",
            dest="supports_batched_tracker",
            action="store_true",
            default=None,
            help=(
                "Explicitly enable tracker batching support when creating tracker functions. "
                "Enabled by default for SAM2 tracker specs."
            ),
        )
        parser.add_argument(
            "--no-supports-batched-tracker",
            dest="supports_batched_tracker",
            action="store_false",
            help="Disable tracker batching capability when registering tracker functions.",
        )

        configure_function_implementation_arguments(parser)

    @staticmethod
    def _dump_sublabel_spec(
        sl_spec: Union[models.SublabelRequest, models.PatchedLabelRequest],
    ) -> dict:
        result = {
            "name": sl_spec.name,
            "attributes": [
                {
                    "name": attribute_spec.name,
                    "input_type": attribute_spec.input_type,
                    "values": attribute_spec.values,
                }
                for attribute_spec in getattr(sl_spec, "attributes", [])
            ],
        }

        if getattr(sl_spec, "type", "any") != "any":
            # Add the type conditionally, to stay compatible with older
            # CVAT versions when the function doesn't define label types.
            result["type"] = sl_spec.type

        return result

    def execute(
        self,
        client: Client,
        *,
        name: str,
        function_loader: FunctionLoader,
        supports_batched_tracker: Optional[bool] = None,
    ) -> None:
        function = function_loader.load()

        remote_function: dict[str, Any] = {
            "provider": FUNCTION_PROVIDER_NATIVE,
            "name": name,
        }

        spec = function.spec

        if isinstance(spec, cvataa.DetectionFunctionSpec):
            remote_function["kind"] = FUNCTION_KIND_DETECTOR
            remote_function["labels_v2"] = []

            for label_spec in spec.labels:
                remote_function["labels_v2"].append(self._dump_sublabel_spec(label_spec))

                if sublabels := getattr(label_spec, "sublabels", None):
                    remote_function["labels_v2"][-1]["sublabels"] = [
                        self._dump_sublabel_spec(sublabel) for sublabel in sublabels
                    ]
        elif isinstance(spec, cvataa.TrackingFunctionSpec):
            remote_function["kind"] = FUNCTION_KIND_TRACKER
            remote_function["supported_shape_types"] = sorted(spec.supported_shape_types)
            tracker_support_flag = supports_batched_tracker
            if tracker_support_flag is None:
                tracker_support_flag = True
            remote_function["supports_batched_tracker"] = bool(tracker_support_flag)
        elif isinstance(spec, cvataa.InteractorFunctionSpec):
            remote_function["kind"] = FUNCTION_KIND_INTERACTOR
            remote_function.update(
                min_pos_points=spec.min_pos_points,
                min_neg_points=spec.min_neg_points,
                startswith_box=spec.startswith_box,
                startswith_box_optional=spec.startswith_box_optional,
                help_message=spec.help_message,
                animated_gif=spec.animated_gif,
                version=spec.version,
            )
        else:
            raise cvataa.BadFunctionError(f"Unsupported function spec type: {type(spec).__name__}")

        try:
            _, response = client.api_client.call_api(
                "/api/functions",
                "POST",
                body=remote_function,
            )
        except ApiException as exc:
            raise_if_functions_api_missing(exc, action="Creating native functions")

        remote_function = json.loads(response.data)

        client.logger.info(
            "Created function #%d: %s", remote_function["id"], remote_function["name"]
        )
        print(remote_function["id"])


@COMMANDS.command_class("delete")
class FunctionDelete:
    description = "Delete a list of functions, ignoring those which don't exist."

    def configure_parser(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("function_ids", type=int, help="IDs of functions to delete", nargs="+")

    def execute(self, client: Client, *, function_ids: Sequence[int]) -> None:
        for function_id in function_ids:
            _, response = client.api_client.call_api(
                "/api/functions/{function_id}",
                "DELETE",
                path_params={"function_id": function_id},
                _check_status=False,
            )

            if 200 <= response.status <= 299:
                client.logger.info(f"Function #{function_id} deleted")
            elif response.status == 404:
                client.logger.warning(f"Function #{function_id} not found")
            else:
                client.logger.error(
                    f"Failed to delete function #{function_id}: "
                    f"{response.msg} (status {response.status})"
                )


@COMMANDS.command_class("run-agent")
class FunctionRunAgent:
    description = "Process requests for a given native function, indefinitely."

    def configure_parser(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "function_id",
            type=int,
            help="ID of the function to process requests for",
        )

        configure_function_implementation_arguments(parser)

        parser.add_argument(
            "--burst",
            action="store_true",
            help="process all pending requests and then exit",
        )
        parser.add_argument(
            "--max-cache-tasks-with-chunks",
            type=int,
            default=1,
            help="maximum number of tasks whose media chunks may be cached locally",
        )
        parser.add_argument(
            "--max-cache-tasks-without-chunks",
            type=int,
            default=10,
            help="maximum number of tasks cached without downloaded chunks",
        )
        parser.add_argument(
            "--tracker-preload-chunks",
            action="store_true",
            help="download tracker task media chunks when possible to speed up frame access",
        )
        parser.add_argument(
            "--include-fetch-metrics",
            action="store_true",
            help=(
                "enable verbose tracker dataset fetch logs so benchmarking tools can "
                "collect chunk cache metrics"
            ),
        )

    def execute(
        self,
        client: Client,
        *,
        function_id: int,
        function_loader: FunctionLoader,
        burst: bool,
        max_cache_tasks_with_chunks: int,
        max_cache_tasks_without_chunks: int,
        tracker_preload_chunks: bool,
        include_fetch_metrics: bool,
    ) -> None:
        run_agent(
            client,
            function_loader,
            function_id,
            burst=burst,
            max_tasks_with_chunks=max_cache_tasks_with_chunks,
            max_tasks_without_chunks=max_cache_tasks_without_chunks,
            tracker_allow_chunk_preload=tracker_preload_chunks,
            tracker_verbose_logs=True if include_fetch_metrics else None,
        )
