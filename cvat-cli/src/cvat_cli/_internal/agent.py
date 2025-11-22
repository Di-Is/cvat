# Copyright (C) CVAT.ai Corporation
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import concurrent.futures
import contextlib
import json
import multiprocessing
import os
import random
import secrets
import shutil
import tempfile
import time
import threading
from collections import OrderedDict
from collections.abc import Generator, Iterator, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional, Union
import enum

import attrs
import cvat_sdk.auto_annotation as cvataa
import cvat_sdk.datasets as cvatds
import PIL.Image
import urllib3.exceptions
from cvat_sdk import Client, models
from cvat_sdk.auto_annotation.driver import (
    _AnnotationMapper,
    _DetectionFunctionContextImpl,
    _SpecNameMapping,
)
from cvat_sdk.datasets.caching import make_cache_manager
from cvat_sdk.datasets.common import UnsupportedDatasetError
from cvat_sdk.exceptions import ApiException
from typing_extensions import TypeAlias

from .common import CriticalError, FunctionLoader, raise_if_functions_api_missing

if TYPE_CHECKING:
    from _typeshed import SupportsReadline

FUNCTION_PROVIDER_NATIVE = "native"
FUNCTION_KIND_DETECTOR = "detector"
FUNCTION_KIND_TRACKER = "tracker"
FUNCTION_KIND_INTERACTOR = "interactor"
REQUEST_CATEGORY_BATCH = "batch"
REQUEST_CATEGORY_INTERACTIVE = "interactive"

REQUEST_CATEGORIES_WITH_DECREASING_PRIORITY = (REQUEST_CATEGORY_INTERACTIVE, REQUEST_CATEGORY_BATCH)

# Poll aggressively (fallback path) when the SSE watcher is disconnected so that
# interactive requests do not sit in the queue and hit the 60s REST timeout.
_POLLING_INTERVAL_MEAN_FREQUENT = timedelta(seconds=0.3)
_POLLING_INTERVAL_MEAN_RARE = timedelta(seconds=1)
_JITTER_AMOUNT = 0.15

_UPDATE_INTERVAL = timedelta(seconds=30)

_MAX_AGE_OF_TRACKING_STATE = timedelta(hours=8)


def _env_flag(name: str) -> bool:
    value = os.getenv(name)
    if value is None:
        return False

    normalized = value.strip().lower()
    return normalized not in {"", "0", "false", "off", "no"}


_SAM2_TRACKER_VERBOSE = _env_flag("SAM2_TRACKER_VERBOSE")
_TRACKER_PREFETCH_FRAMES = bool(int(os.getenv("SAM2_TRACKER_PREFETCH_FRAMES", "1")))
_TRACKER_PREFETCH_PARALLEL = int(os.getenv("SAM2_TRACKER_PREFETCH_PARALLEL", "0"))
_TRACKER_DOUBLE_BUFFER = _env_flag("SAM2_TRACKER_DOUBLE_BUFFER")
_TRACKER_GPU_DOUBLE_BUFFER = _env_flag("SAM2_TRACKER_GPU_DOUBLE_BUFFER")


class _RecoverableExecutor:
    # A wrapper around ProcessPoolExecutor that recreates the underlying
    # executor when a worker crashes.
    def __init__(self, initializer, initargs):
        self._mp_context = multiprocessing.get_context("spawn")
        self._initializer = initializer
        self._initargs = initargs
        self._executor = self._new_executor()

    def _new_executor(self):
        return concurrent.futures.ProcessPoolExecutor(
            max_workers=1,
            mp_context=self._mp_context,
            initializer=self._initializer,
            initargs=self._initargs,
        )

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self._executor.shutdown()

    def submit(self, func, /, *args, **kwargs):
        return self._executor.submit(func, *args, **kwargs)

    def result(self, future: concurrent.futures.Future):
        try:
            return future.result()
        except concurrent.futures.BrokenExecutor:
            self._executor.shutdown()
            self._executor = self._new_executor()
            raise


_TrackingStateIdGenerator: TypeAlias = Callable[[], str]


def _default_tracking_state_id_generator() -> str:
    # This is defined as a separate function so that tests can monkeypatch it
    # in order to get deterministic state IDs.
    return secrets.token_urlsafe(32)


_current_function: cvataa.AutoAnnotationFunction
_tracking_states: _TrackingStateContainer
_tracking_state_id_generator: _TrackingStateIdGenerator


@attrs.define
class _ExtendedTrackingState:
    inner_state: Any  # the state produced by the AA function
    original_shape_type: str
    original_task_id: int
    original_image_dims: tuple[int, int]
    last_accessed_at: datetime = attrs.field(factory=lambda: datetime.now(tz=timezone.utc))


class _TrackingStateContainer:
    def __init__(self):
        self._id_to_ext_state: OrderedDict[str, _ExtendedTrackingState] = OrderedDict()

    def store(self, state: Any, shape_type: str, task_id: int, image_dims: tuple[int, int]) -> str:
        state_id = _tracking_state_id_generator()
        self._id_to_ext_state[state_id] = _ExtendedTrackingState(
            inner_state=state,
            original_shape_type=shape_type,
            original_task_id=task_id,
            original_image_dims=image_dims,
        )
        return state_id

    def retrieve(self, state_id: str, task_id: int, image_dims: tuple[int, int]) -> Any:
        ext_state = self._id_to_ext_state.get(state_id)

        if not ext_state:
            raise _BadArError(f"Tracking state {state_id!r} not found - possibly expired")

        if ext_state.original_task_id != task_id:
            # This is a defense-in-depth measure. State IDs are supposed to be unguessable,
            # but even if an attacker manages to obtain one, they will not be able to use it
            # to get any information about a task they don't have access to.
            raise _BadArError(f"Tracking state {state_id!r} is not for task #{task_id}")

        if image_dims != ext_state.original_image_dims:
            raise _BadArError(f"Image sizes of the start frame and the current frame are different")

        ext_state.last_accessed_at = datetime.now(tz=timezone.utc)
        self._id_to_ext_state.move_to_end(state_id)

        return ext_state.inner_state, ext_state.original_shape_type

    def prune(self) -> None:
        cutoff = datetime.now(tz=timezone.utc) - _MAX_AGE_OF_TRACKING_STATE

        while (
            self._id_to_ext_state
            and next(iter(self._id_to_ext_state.values())).last_accessed_at < cutoff
        ):
            self._id_to_ext_state.popitem(last=False)


def _worker_init(function_loader: FunctionLoader, state_id_generator):
    global _current_function
    _current_function = function_loader.load()

    if isinstance(_current_function.spec, cvataa.TrackingFunctionSpec):
        global _tracking_states
        _tracking_states = _TrackingStateContainer()

        global _tracking_state_id_generator
        _tracking_state_id_generator = state_id_generator


def _worker_job_get_function_spec():
    return _current_function.spec


def _worker_job_detect(
    context: _DetectionFunctionContextImpl, image: PIL.Image.Image
) -> list[cvataa.DetectionAnnotation]:
    return _current_function.detect(context, image)


def _worker_job_init_tracking(
    task_id: int,
    image: PIL.Image.Image,
    shapes: list[cvataa.TrackableShape],
) -> list[str]:
    _tracking_states.prune()

    if hasattr(_current_function, "preprocess_image"):
        pp_image = _current_function.preprocess_image(_TrackingFunctionContextImpl(), image)
    else:
        pp_image = image

    return [
        _tracking_states.store(
            state=_current_function.init_tracking_state(
                _TrackingFunctionShapeContextImpl(original_shape_type=shape.type), pp_image, shape
            ),
            shape_type=shape.type,
            task_id=task_id,
            image_dims=image.size,
        )
        for shape in shapes
    ]


def _worker_job_track(
    task_id: int, image: PIL.Image.Image, states: list[str]
) -> list[Optional[cvataa.TrackableShape]]:
    _tracking_states.prune()

    pp_image = _current_function.preprocess_image(_TrackingFunctionContextImpl(), image)

    batch_entries: list[tuple[_TrackingFunctionShapeContextImpl, Any, str]] = []
    for state_id in states:
        inner_state, original_shape_type = _tracking_states.retrieve(
            state_id=state_id, task_id=task_id, image_dims=image.size
        )
        batch_entries.append(
            (
                _TrackingFunctionShapeContextImpl(original_shape_type=original_shape_type),
                inner_state,
                original_shape_type,
            )
        )

    context_state_pairs = [(context, inner_state) for context, inner_state, _ in batch_entries]

    if hasattr(_current_function, "track_batch"):
        predictions = _current_function.track_batch(context_state_pairs, pp_image)  # type: ignore[attr-defined]
    else:
        predictions = [
            _current_function.track(context, pp_image, inner_state)
            for context, inner_state in context_state_pairs
        ]

    if len(predictions) != len(batch_entries):
        raise cvataa.BadFunctionError(
            "Tracker returned an unexpected number of predictions in track_batch()"
        )

    normalized_predictions: list[Optional[cvataa.TrackableShape]] = []
    for prediction, (_, _, original_shape_type) in zip(predictions, batch_entries):
        if prediction and prediction.type != original_shape_type:
            raise cvataa.BadFunctionError(
                f"function output shape of type {prediction.type!r}, "
                f"but original shape was of type {original_shape_type!r}"
            )
        normalized_predictions.append(prediction)
    return normalized_predictions


def _worker_job_track_double_buffer(
    task_id: int, frames_and_images: list[tuple[int, PIL.Image.Image]], states: list[str]
) -> tuple[list[dict[str, Any]], list[float]]:
    """
    Track a chunk with double-buffered preprocess:
    - preprocess next frame on a background thread while tracking the current frame
    - CPU decode/resize/normalize happens in the background thread (fallback)
    - If SAM2_TRACKER_GPU_DOUBLE_BUFFER is set, use preprocess_image (fast-preprocess) in the
      background thread instead of CPU transforms, and rely on ready_event for stream sync.
    """
    global _tracking_states, _tracking_state_id_generator
    if "_tracking_states" not in globals() or not isinstance(_tracking_states, _TrackingStateContainer):
        _tracking_states = _TrackingStateContainer()
        _tracking_state_id_generator = _default_tracking_state_id_generator

    supports_gpu_pp = _TRACKER_GPU_DOUBLE_BUFFER and hasattr(_current_function, "preprocess_image")
    supports_cpu_pp = hasattr(_current_function, "cpu_preprocess_image") and hasattr(
        _current_function, "forward_preprocessed_tensor"
    )
    if not (supports_gpu_pp or supports_cpu_pp):
        raise cvataa.BadFunctionError(
            "Double-buffer path requires preprocess_image or cpu_preprocess_image support"
        )

    _tracking_states.prune()
    if not frames_and_images:
        return [], []

    first_image = frames_and_images[0][1]
    batch_entries: list[tuple[_TrackingFunctionShapeContextImpl, Any, str]] = []
    for state_id in states:
        inner_state, original_shape_type = _tracking_states.retrieve(
            state_id=state_id, task_id=task_id, image_dims=first_image.size
        )
        ctx = _TrackingFunctionShapeContextImpl(original_shape_type=original_shape_type)
        batch_entries.append((ctx, inner_state, original_shape_type))

    context_state_pairs = [(context, inner_state) for context, inner_state, _ in batch_entries]

    def _preprocess_cpu(img: PIL.Image.Image) -> Any:
        return _current_function.cpu_preprocess_image(_TrackingFunctionContextImpl(), img)

    def _preprocess_gpu(img: PIL.Image.Image):
        return _current_function.preprocess_image(_TrackingFunctionContextImpl(), img)

    frame_payloads: list[dict[str, Any]] = []
    frame_latencies_ms: list[float] = []

    # First frame preprocess happens synchronously to prime the buffers.
    if supports_gpu_pp:
        pp_current = _preprocess_gpu(frames_and_images[0][1])
        size_current = None
    else:
        pp_current, size_current = _preprocess_cpu(frames_and_images[0][1])

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        for idx, (frame_index, image) in enumerate(frames_and_images):
            pp_future: Optional[concurrent.futures.Future] = None
            if idx + 1 < len(frames_and_images):
                pp_future = pool.submit(
                    _preprocess_gpu if supports_gpu_pp else _preprocess_cpu,
                    frames_and_images[idx + 1][1],
                )

            frame_start = time.perf_counter()
            if supports_gpu_pp:
                pp_image = pp_current
            else:
                pp_image = _current_function.forward_preprocessed_tensor(
                    pp_current, original_size=size_current
                )
            predictions = (
                _current_function.track_batch(context_state_pairs, pp_image)
                if hasattr(_current_function, "track_batch")
                else [
                    _current_function.track(context, pp_image, inner_state)
                    for context, inner_state in context_state_pairs
                ]
            )

            if len(predictions) != len(batch_entries):
                raise cvataa.BadFunctionError(
                    "Tracker returned an unexpected number of predictions in track_batch()"
                )

            normalized_predictions: list[Optional[cvataa.TrackableShape]] = []
            for prediction, (_, _, original_shape_type) in zip(predictions, batch_entries):
                if prediction and prediction.type != original_shape_type:
                    raise cvataa.BadFunctionError(
                        f"function output shape of type {prediction.type!r}, "
                        f"but original shape was of type {original_shape_type!r}"
                    )
                normalized_predictions.append(prediction)

            frame_payloads.append(
                {
                    "frame": frame_index,
                    "shapes": [attrs.asdict(shape) if shape else None for shape in normalized_predictions],
                }
            )
            frame_latencies_ms.append(round((time.perf_counter() - frame_start) * 1000, 3))

            if pp_future is not None:
                if supports_gpu_pp:
                    pp_current = pp_future.result()
                else:
                    pp_current, size_current = pp_future.result()

    return frame_payloads, frame_latencies_ms


def _worker_job_interact(
    context: cvataa.InteractorFunctionContext,
    image: PIL.Image.Image,
    prompt: cvataa.InteractionPrompt,
) -> cvataa.MaskPrediction:
    return _current_function.interact(context, image, prompt)


def _serialize_mask_prediction(prediction: cvataa.MaskPrediction) -> dict[str, Any]:
    payload: dict[str, Any] = {}

    if prediction.mask is not None:
        payload["mask"] = [list(row) for row in prediction.mask]

    if prediction.mask_rle is not None:
        payload["mask_rle"] = list(prediction.mask_rle)

    if prediction.bounds is not None:
        payload["bounds"] = list(prediction.bounds)

    if prediction.points is not None:
        payload["points"] = [list(point) for point in prediction.points]

    return payload


def _build_interaction_prompt(ar_params: dict) -> cvataa.InteractionPrompt:
    return cvataa.InteractionPrompt(
        positive_points=ar_params["pos_points"],
        negative_points=ar_params.get("neg_points", []),
        bounding_box=ar_params.get("obj_bbox"),
        start_with_box=bool(ar_params.get("start_with_box")),
        label_id=ar_params.get("label_id"),
    )


@attrs.frozen
class _Event:
    type: str
    data: str


@attrs.frozen
class _NewReconnectionDelay:
    delay: timedelta


class _TaskCacheLimiter:
    """
    This class deletes least-recently used tasks from the dataset cache,
    so that at any time the cache contains at most _MAX_TASKS_WITH_CHUNKS
    tasks with downloaded chunks, and at most _MAX_TASKS_WITHOUT_CHUNKS without.

    This helps manage disk usage, since agents may run indefinitely, and
    we don't want the dataset cache to keep growing.
    """

    class _CacheOwner(enum.Enum):
        TRACKER = "tracker"
        INTERACTOR = "interactor"
        OTHER = "other"

    def __init__(
        self,
        client: Client,
        *,
        max_tasks_with_chunks: int = 1,
        max_tasks_without_chunks: int = 10,
        on_task_evicted: Callable[[int, bool, "._CacheOwner"], None] | None = None,
    ) -> None:
        self._client = client
        self._cache_manager = make_cache_manager(client, cvatds.UpdatePolicy.IF_MISSING_OR_STALE)

        self._max_tasks_with_chunks = max(1, max_tasks_with_chunks)
        self._max_tasks_without_chunks = max(1, max_tasks_without_chunks)
        self._on_task_evicted = on_task_evicted

        self._cached_with_chunks_task_ids = []
        self._cached_without_chunks_task_ids = []

        self._task_ids_in_use = set()

    @contextlib.contextmanager
    def using_cache_for_task(
        self,
        task_id: int,
        *,
        with_chunks: bool,
        owner: "_TaskCacheLimiter._CacheOwner" = _CacheOwner.OTHER,
    ) -> Generator[None, None, None]:
        if task_id in self._task_ids_in_use:
            yield
            return

        if task_id in self._cached_with_chunks_task_ids:
            # If the task already had cached chunks, we have to return it back to
            # _cached_with_chunks_task_ids in the end.
            with_chunks = True

            self._cached_with_chunks_task_ids.remove(task_id)
        elif task_id in self._cached_without_chunks_task_ids:
            self._cached_without_chunks_task_ids.remove(task_id)

        self._task_ids_in_use.add(task_id)

        if with_chunks:
            cached_task_ids = self._cached_with_chunks_task_ids
            max_cached_tasks = self._max_tasks_with_chunks
        else:
            cached_task_ids = self._cached_without_chunks_task_ids
            max_cached_tasks = self._max_tasks_without_chunks

        if len(cached_task_ids) + len(self._task_ids_in_use) > max_cached_tasks:
            evicted_task_id = cached_task_ids.pop(0)
            self._delete_task_cache(evicted_task_id, with_chunks=with_chunks, owner=owner)

        try:
            yield
        finally:
            self._task_ids_in_use.remove(task_id)
            cached_task_ids.append(task_id)

    def _delete_task_cache(
        self,
        task_id: int,
        *,
        with_chunks: bool,
        owner: "_TaskCacheLimiter._CacheOwner",
    ) -> None:
        self._client.logger.info("Deleting task %d from the cache to make room...", task_id)
        shutil.rmtree(self._cache_manager.task_dir(task_id), ignore_errors=True)
        if self._on_task_evicted:
            self._on_task_evicted(task_id, with_chunks, owner)


class _DatasetRepositoryBase:
    def __init__(self, client: Client) -> None:
        self._client = client
        self._datasets: dict[int, cvatds.TaskDataset] = {}
        self._lock = threading.Lock()

    def get(self, task_id: int, **kwargs) -> cvatds.TaskDataset:
        with self._lock:
            dataset = self._datasets.get(task_id)
            if dataset:
                return dataset

        dataset = self._build_dataset(task_id, **kwargs)

        with self._lock:
            self._datasets[task_id] = dataset

        return dataset

    def discard(self, task_id: int) -> None:
        with self._lock:
            self._datasets.pop(task_id, None)

    def _build_dataset(self, task_id: int, **_) -> cvatds.TaskDataset:
        raise NotImplementedError


class _InteractorDatasetRepository(_DatasetRepositoryBase):
    def _build_dataset(self, task_id: int, **_) -> cvatds.TaskDataset:
        start_ts = time.perf_counter()
        try:
            dataset = cvatds.TaskDataset(
                self._client,
                task_id,
                load_annotations=False,
                media_download_policy=cvatds.MediaDownloadPolicy.PRELOAD_ALL,
            )
            elapsed = time.perf_counter() - start_ts
            self._client.logger.info(
                "Preloaded dataset for task %d (%.2fs)", task_id, elapsed
            )
            return dataset
        except UnsupportedDatasetError:
            self._client.logger.warning(
                "Task %d does not support chunk preloading; falling back to on-demand frames",
                task_id,
            )
            return cvatds.TaskDataset(
                self._client,
                task_id,
                load_annotations=False,
                media_download_policy=cvatds.MediaDownloadPolicy.FETCH_FRAMES_ON_DEMAND,
            )


class _TrackerDatasetRepository(_DatasetRepositoryBase):
    def get(self, task_id: int, *, allow_chunks: bool = False) -> cvatds.TaskDataset:
        mode = (
            cvatds.ChunkCacheMode.PREFETCH_CHUNKS_ONCE
            if allow_chunks
            else cvatds.ChunkCacheMode.FETCH_ON_DEMAND
        )
        return super().get(task_id, chunk_cache_mode=mode)

    def _build_dataset(self, task_id: int, **kwargs) -> cvatds.TaskDataset:
        chunk_cache_mode: cvatds.ChunkCacheMode = kwargs.get(
            "chunk_cache_mode", cvatds.ChunkCacheMode.FETCH_ON_DEMAND
        )
        start_ts = time.perf_counter()
        try:
            dataset = cvatds.TaskDataset(
                self._client,
                task_id,
                load_annotations=False,
                chunk_cache_mode=chunk_cache_mode,
            )
        except UnsupportedDatasetError:
            if chunk_cache_mode != cvatds.ChunkCacheMode.PREFETCH_CHUNKS_ONCE:
                raise
            self._client.logger.warning(
                "Task %d does not support tracker chunk preloading; falling back to on-demand frames",
                task_id,
            )
            dataset = cvatds.TaskDataset(
                self._client,
                task_id,
                load_annotations=False,
                chunk_cache_mode=cvatds.ChunkCacheMode.FETCH_ON_DEMAND,
            )

        elapsed = time.perf_counter() - start_ts
        mode_label = (
            "lazy-chunk"
            if dataset.chunk_cache_mode == cvatds.ChunkCacheMode.PREFETCH_CHUNKS_ONCE
            else "on-demand"
        )
        self._client.logger.info(
            "Prepared %s tracker dataset for task %d (%.2fs)", mode_label, task_id, elapsed
        )
        return dataset


def _parse_event_stream(
    stream: SupportsReadline[bytes],
) -> Iterator[Union[_Event, _NewReconnectionDelay]]:
    # https://html.spec.whatwg.org/multipage/server-sent-events.html#event-stream-interpretation

    event_type = event_data = ""

    while True:
        line_bytes = stream.readline()
        if not line_bytes:
            return

        line = line_bytes.decode("UTF-8").removesuffix("\n").removesuffix("\r")

        # Technically, a standalone \r is supposed to be treated as a line terminator,
        # but it's annoying to implement, and there's no reason for CVAT to use that.
        if "\r" in line:
            raise ValueError("CR found in event stream")

        if not line:
            yield _Event(event_type, event_data.removesuffix("\n"))
            event_type = event_data = ""
            continue

        if line.startswith(":"):
            # it's a comment/keepalive
            continue

        if ":" in line:
            field_name, field_value = line.split(":", maxsplit=1)
            field_value = field_value.removeprefix(" ")
        else:
            field_name = line
            field_value = ""

        if field_name == "event":
            event_type = field_value
        elif field_name == "data":
            event_data += field_value + "\n"
        elif field_name == "retry":
            if field_value.isascii() and field_value.isdecimal():
                yield _NewReconnectionDelay(timedelta(milliseconds=int(field_value)))


class _BadArError(Exception):
    pass


class _IncompatibleFunctionError(Exception):
    # This should only be thrown from inside _validate_X_function_compatibility methods.
    pass


class _TrackingFunctionContextImpl(cvataa.TrackingFunctionContext):
    pass


@attrs.frozen(kw_only=True)
class _TrackingFunctionShapeContextImpl(cvataa.TrackingFunctionShapeContext):
    original_shape_type: str


class _InteractorFunctionContextImpl(cvataa.InteractorFunctionContext):
    def __init__(
        self,
        *,
        task_id: int,
        job_id: Optional[int],
        frame_index: int,
        job_frame_index: int,
        frame_name: str,
    ) -> None:
        self._task_id = task_id
        self._job_id = job_id
        self._frame_index = frame_index
        self._job_frame_index = job_frame_index
        self._frame_name = frame_name

    @property
    def task_id(self) -> int:
        return self._task_id

    @property
    def job_id(self) -> Optional[int]:
        return self._job_id

    @property
    def frame_index(self) -> int:
        return self._frame_index

    @property
    def job_frame_index(self) -> int:
        return self._job_frame_index

    @property
    def frame_name(self) -> str:
        return self._frame_name


class _Agent:
    def __init__(
        self,
        client: Client,
        executor: _RecoverableExecutor,
        function_id: int,
        *,
        max_tasks_with_chunks: int = 1,
        max_tasks_without_chunks: int = 10,
        tracker_allow_chunk_preload: bool = False,
        tracker_verbose_logs: bool | None = None,
    ):
        self._rng = random.Random()  # nosec

        self._client = client
        self._executor = executor
        self._function_id = function_id
        self._function_spec = self._executor.result(
            self._executor.submit(_worker_job_get_function_spec)
        )

        _, response = self._client.api_client.call_api(
            "/api/functions/{function_id}",
            "GET",
            path_params={"function_id": self._function_id},
        )

        remote_function = json.loads(response.data)

        self._validate_function_compatibility(remote_function)

        self._agent_id = secrets.token_hex(16)
        self._client.logger.info("Agent starting with ID %r", self._agent_id)

        self._interactor_datasets = _InteractorDatasetRepository(self._client)
        self._tracker_datasets = _TrackerDatasetRepository(self._client)
        self._tracker_allow_chunk_preload = tracker_allow_chunk_preload
        if tracker_verbose_logs is None:
            tracker_verbose_logs = _SAM2_TRACKER_VERBOSE
        self._tracker_verbose_logs = tracker_verbose_logs
        self._task_cache_limiter = _TaskCacheLimiter(
            client,
            max_tasks_with_chunks=max_tasks_with_chunks,
            max_tasks_without_chunks=max_tasks_without_chunks,
            on_task_evicted=self._handle_task_cache_evicted,
        )

        self._queue_watch_response = None
        self._queue_watch_response_lock = threading.Lock()
        self._queue_watcher_should_stop = threading.Event()

        self._potential_work_condition = threading.Condition(threading.Lock())
        self._potential_work_per_category = {
            category: True for category in REQUEST_CATEGORIES_WITH_DECREASING_PRIORITY
        }

        self._polling_interval = _POLLING_INTERVAL_MEAN_FREQUENT

        # If we fail to connect to the queue event stream, it might be because
        # the server is too old and doesn't support the watch endpoint.
        # In this case, it doesn't make sense to continue trying to connect frequently,
        # although we should still be trying occasionally in case the error is transient.
        # Once we're successful, we'll rely on the server to set a new reconnection delay.
        self._queue_reconnection_delay = _POLLING_INTERVAL_MEAN_RARE

    def _handle_task_cache_evicted(
        self,
        task_id: int,
        with_chunks: bool,
        owner: _TaskCacheLimiter._CacheOwner,
    ) -> None:
        if owner == _TaskCacheLimiter._CacheOwner.INTERACTOR:
            self._client.logger.info(
                "Evicting cached preloaded dataset for task %d", task_id
            )
            self._interactor_datasets.discard(task_id)
        elif owner == _TaskCacheLimiter._CacheOwner.TRACKER:
            self._client.logger.info(
                "Evicting cached tracker dataset for task %d", task_id
            )
            self._tracker_datasets.discard(task_id)
        else:
            self._client.logger.info("Cache eviction completed for task %d", task_id)

    def _validate_function_compatibility(self, remote_function: dict) -> None:
        function_id = remote_function["id"]

        if remote_function["provider"] != FUNCTION_PROVIDER_NATIVE:
            raise CriticalError(
                f"Function #{function_id} has provider {remote_function['provider']!r}. "
                f"Agents can only be run for functions with provider {FUNCTION_PROVIDER_NATIVE!r}."
            )

        try:
            if isinstance(self._function_spec, cvataa.DetectionFunctionSpec):
                self._validate_detection_function_compatibility(remote_function)
                self._calculate_result_for_ar = self._calculate_result_for_detection_ar
            elif isinstance(self._function_spec, cvataa.TrackingFunctionSpec):
                self._validate_tracking_function_compatibility(remote_function)
                self._calculate_result_for_ar = self._calculate_result_for_tracking_ar
            elif isinstance(self._function_spec, cvataa.InteractorFunctionSpec):
                self._validate_interactor_function_compatibility(remote_function)
                self._calculate_result_for_ar = self._calculate_result_for_interactor_ar
            else:
                raise CriticalError(
                    f"Unsupported function spec type: {type(self._function_spec).__name__}"
                )
        except _IncompatibleFunctionError as ex:
            raise CriticalError(
                f"Function #{function_id} is incompatible with function object: {ex}"
            ) from ex

    def _validate_detection_function_compatibility(self, remote_function: dict) -> None:
        self._validate_remote_function_kind(remote_function, FUNCTION_KIND_DETECTOR)

        labels_by_name = {label.name: label for label in self._function_spec.labels}

        for remote_label in remote_function["labels_v2"]:
            label_desc = f"label {remote_label['name']!r}"
            label = labels_by_name.get(remote_label["name"])

            self._validate_sublabel_compatibility(remote_label, label, label_desc)

            sublabels_by_name = {sl.name: sl for sl in getattr(label, "sublabels", [])}

            for remote_sl in remote_label.get("sublabels", []):
                sl_desc = f"sublabel {remote_sl['name']!r} of {label_desc}"
                sl = sublabels_by_name.get(remote_sl["name"])

                self._validate_sublabel_compatibility(remote_sl, sl, sl_desc)

    def _validate_sublabel_compatibility(
        self, remote_sl: dict, sl: Optional[models.Sublabel], sl_desc: str
    ):
        if not sl:
            raise CriticalError(f"{sl_desc} is not supported.")

        if remote_sl["type"] not in {"any", "unknown"} and remote_sl["type"] != sl.type:
            raise _IncompatibleFunctionError(
                f"{sl_desc} has type {remote_sl['type']!r}, "
                f"but the function object declares type {sl.type!r}."
            )

        attrs_by_name = {attr.name: attr for attr in getattr(sl, "attributes", [])}

        for remote_attr in remote_sl["attributes"]:
            attr_desc = f"attribute {remote_attr['name']!r} of {sl_desc}"
            attr = attrs_by_name.get(remote_attr["name"])

            if not attr:
                raise _IncompatibleFunctionError(f"{attr_desc} is not supported.")

            if remote_attr["input_type"] != attr.input_type.value:
                raise _IncompatibleFunctionError(
                    f"{attr_desc} has input type {remote_attr['input_type']!r},"
                    f" but the function object declares input type {attr.input_type.value!r}."
                )

            if remote_attr["values"] != attr.values:
                raise _IncompatibleFunctionError(
                    f"{attr_desc} has values {remote_attr['values']!r},"
                    f" but the function object declares values {attr.values!r}."
                )

    def _validate_tracking_function_compatibility(self, remote_function: dict) -> None:
        self._validate_remote_function_kind(remote_function, FUNCTION_KIND_TRACKER)

        remote_supported_shape_types = frozenset(remote_function["supported_shape_types"])
        unsupported = remote_supported_shape_types - self._function_spec.supported_shape_types

        if unsupported:
            raise _IncompatibleFunctionError(
                "the function object does not support the following shape types: "
                + ", ".join(map(repr, unsupported))
            )

    def _validate_interactor_function_compatibility(self, remote_function: dict) -> None:
        self._validate_remote_function_kind(remote_function, FUNCTION_KIND_INTERACTOR)

        field_names = (
            "min_pos_points",
            "min_neg_points",
            "startswith_box",
            "startswith_box_optional",
            "help_message",
            "animated_gif",
            "version",
        )

        for field_name in field_names:
            remote_value = remote_function.get(field_name)
            spec_value = getattr(self._function_spec, field_name)
            if remote_value != spec_value:
                raise _IncompatibleFunctionError(
                    f"{field_name} is {remote_value!r}, but the function object declares {spec_value!r}."
                )

    def _validate_remote_function_kind(self, remote_function: dict, expected_kind: str) -> None:
        if remote_function["kind"] != expected_kind:
            raise _IncompatibleFunctionError(
                f"kind is {remote_function['kind']!r} (expected {expected_kind!r})."
            )

    def _wait_between_polls(self):
        # offset the interval randomly to avoid synchronization between workers
        timeout_multiplier = self._rng.uniform(1 - _JITTER_AMOUNT, 1 + _JITTER_AMOUNT)

        with self._potential_work_condition:
            wait_succeeded = self._potential_work_condition.wait_for(
                lambda: any(self._potential_work_per_category.values()),
                timeout=self._polling_interval.total_seconds() * timeout_multiplier,
            )

            if not wait_succeeded:
                # If we timed out, there is a possibility that the queue watcher is broken or
                # that it somehow missed an event. Either way, we'll force a poll to make sure
                # we don't miss anything.
                for category in self._potential_work_per_category:
                    self._potential_work_per_category[category] = True

    def _dispatch_queue_event(self, event: _Event) -> None:
        if event.type == "newrequest":
            event_data_object = json.loads(event.data)
            request_category = event_data_object["request_category"]

            with self._potential_work_condition:
                if request_category in self._potential_work_per_category:
                    self._client.logger.info(
                        "Received notification about a new request of category %r",
                        request_category,
                    )
                    self._potential_work_per_category[request_category] = True
                    self._potential_work_condition.notify()
                else:
                    self._client.logger.warning(
                        "Received notification about a new request of unknown category: %r",
                        request_category,
                    )
        else:
            self._client.logger.warning("Received event of unknown type: %r", event.type)

    def _wait_before_reconnecting_to_queue(self):
        delay_multiplier = self._rng.uniform(1, 1 + _JITTER_AMOUNT)
        self._queue_watcher_should_stop.wait(
            timeout=self._queue_reconnection_delay.total_seconds() * delay_multiplier
        )

        # Apply exponential backoff.
        self._queue_reconnection_delay = min(
            self._queue_reconnection_delay * 2, _POLLING_INTERVAL_MEAN_RARE
        )

    def _watch_queue(self) -> None:
        while not self._queue_watcher_should_stop.is_set():
            # Until we can (re)connect to the event stream, poll more frequently.
            self._polling_interval = _POLLING_INTERVAL_MEAN_FREQUENT

            with self._queue_watch_response_lock:
                self._client.logger.info("Attempting to watch the function's queue...")

                try:
                    _, self._queue_watch_response = self._client.api_client.call_api(
                        "/api/functions/queues/{queue_id}/watch",
                        "GET",
                        path_params={"queue_id": f"function:{self._function_id}"},
                        _parse_response=False,
                    )
                except Exception:
                    self._client.logger.error(
                        "Failed to connect to the queue event stream; will retry",
                        exc_info=True,
                    )
                    self._wait_before_reconnecting_to_queue()
                    continue
                else:
                    self._client.logger.info("Connected to the queue event stream")

                    # Now we can rely on notifications, so slow down polling.
                    self._polling_interval = _POLLING_INTERVAL_MEAN_RARE

            try:
                for message in _parse_event_stream(self._queue_watch_response):
                    if isinstance(message, _Event):
                        self._dispatch_queue_event(message)
                    elif isinstance(message, _NewReconnectionDelay):
                        self._queue_reconnection_delay = message.delay
                        self._client.logger.info(
                            "New queue event stream reconnection delay is %fs",
                            self._queue_reconnection_delay.total_seconds(),
                        )
                    else:
                        assert False, f"unexpected message type {type(message)}"

                self._queue_watch_response.release_conn()

                # We should normally not get here unless the function is deleted on the server.
                # However, we don't know that for sure, so instead of quitting immediately,
                # we'll ask the main thread to poll for an AR.
                # If the function did get deleted, the main thread will get a 404 and quit.
                # Otherwise, we'll just reconnect again.
                with self._potential_work_condition:
                    for category in self._potential_work_per_category:
                        self._potential_work_per_category[category] = True
                    self._potential_work_condition.notify()

                self._client.logger.warning("Event stream ended; will reconnect")
            except Exception:
                # This is an extra check to prevent useless messages.
                # If we crashed, but the main thread wants us to stop anyway,
                # we should just stop and not spam the log.
                if self._queue_watcher_should_stop.is_set():
                    break

                self._client.logger.error(
                    "Event stream interrupted or other error; will reconnect", exc_info=True
                )
            finally:
                self._queue_watch_response.close()

            self._wait_before_reconnecting_to_queue()

    def run(self, *, burst: bool) -> None:
        if burst:
            self._process_all_available_ars()
            self._client.logger.info("No annotation requests left in queue; exiting.")
        else:
            watcher = threading.Thread(name="Queue Watcher", target=self._watch_queue)
            watcher.start()

            try:
                while True:
                    self._process_all_available_ars()
                    self._wait_between_polls()
            finally:
                self._queue_watcher_should_stop.set()

                with self._queue_watch_response_lock:
                    if self._queue_watch_response:
                        with contextlib.suppress(Exception):
                            # shutdown() requires urllib3 2.3.0, whereas we only require 1.25
                            # (via the SDK). The reason we can't bump the requirement is that
                            # the testsuite depends on botocore, which is incompatible with urllib3
                            # 2.x on Python 3.9 and earlier.
                            # Since pip will, by default, install the latest dependency versions,
                            # most users should not be affected. For the ones that are, shutdown
                            # will be broken, but everything else should still work fine.
                            # This should be revisited once we drop Python 3.9 support.
                            self._queue_watch_response.shutdown()

                watcher.join()

    def _process_all_available_ars(self):
        for category in REQUEST_CATEGORIES_WITH_DECREASING_PRIORITY:
            self._process_available_ars(category)

    def _process_available_ars(self, category) -> None:
        with self._potential_work_condition:
            if not self._potential_work_per_category[category]:
                return

            self._potential_work_per_category[category] = False

        while ar_assignment := self._poll_for_ar(category):
            self._process_ar(ar_assignment)

    def _process_ar(self, ar_assignment: dict) -> None:
        ar_id = ar_assignment["ar_id"]
        ar_params = ar_assignment["ar_params"]

        self._client.logger.info(
            "Got assigned annotation request %r of type %r (%s)",
            ar_id,
            ar_params["type"],
            # Log only a few key parameters to avoid cluttering the info-level log.
            " ".join([f"{k}={ar_params[k]!r}" for k in ("task", "frame") if k in ar_params]),
        )
        self._client.logger.debug("AR %r parameters: %r", ar_id, ar_params)

        try:
            result = self._calculate_result_for_ar(ar_id, ar_params)

            self._client.logger.info("Submitting result for AR %r...", ar_id)
            self._client.api_client.call_api(
                "/api/functions/queues/{queue_id}/requests/{request_id}/complete",
                "POST",
                path_params={"queue_id": f"function:{self._function_id}", "request_id": ar_id},
                body={"agent_id": self._agent_id, **result},
            )
            self._client.logger.info("AR %r completed", ar_id)
        except Exception as ex:
            self._client.logger.error("Failed to process AR %r", ar_id, exc_info=True)

            # Arbitrary exceptions may contain details of the client's system or code, which
            # shouldn't be exposed to the server (and to users of the function).
            # Therefore, we only produce a limited amount of detail, and only in known failure cases.
            error_message = "Unknown error"

            if isinstance(ex, ApiException):
                if ex.status:
                    error_message = f"Received HTTP status {ex.status}"
                else:
                    error_message = "Failed an API call"
            elif isinstance(ex, urllib3.exceptions.RequestError):
                if isinstance(ex, urllib3.exceptions.MaxRetryError):
                    ex_type = type(ex.reason)
                else:
                    ex_type = type(ex)

                error_message = f"Failed to make an HTTP request to {ex.url} ({ex_type.__name__})"
            elif isinstance(ex, urllib3.exceptions.HTTPError):
                error_message = "Failed to make an HTTP request"
            elif isinstance(ex, cvataa.BadFunctionError):
                error_message = "Underlying function returned incorrect result: " + str(ex)
            elif isinstance(ex, _BadArError):
                error_message = "Invalid annotation request: " + str(ex)
            elif isinstance(ex, concurrent.futures.BrokenExecutor):
                error_message = "Worker process crashed"

            try:
                self._client.api_client.call_api(
                    "/api/functions/queues/{queue_id}/requests/{request_id}/fail",
                    "POST",
                    path_params={
                        "queue_id": f"function:{self._function_id}",
                        "request_id": ar_id,
                    },
                    body={"agent_id": self._agent_id, "exc_info": error_message},
                )
            except Exception:
                self._client.logger.error("Couldn't fail AR %r", ar_id, exc_info=True)
            else:
                self._client.logger.info("AR %r failed", ar_id)

    def _poll_for_ar(self, category: str) -> Optional[dict]:
        while True:
            self._client.logger.info(
                "Trying to acquire an annotation request of category %r...", category
            )
            try:
                _, response = self._client.api_client.call_api(
                    "/api/functions/queues/{queue_id}/requests/acquire",
                    "POST",
                    path_params={"queue_id": f"function:{self._function_id}"},
                    body={"agent_id": self._agent_id, "request_category": category},
                )
                break
            except (urllib3.exceptions.HTTPError, ApiException) as ex:
                if isinstance(ex, ApiException) and ex.status and 400 <= ex.status < 500:
                    # We did something wrong; no point in retrying.
                    raise

                self._client.logger.error("Acquire request failed; will retry", exc_info=True)
                self._wait_between_polls()

        response_data = json.loads(response.data)
        return response_data["ar_assignment"]

    def _calculate_result_for_detection_ar(self, ar_id: str, ar_params) -> dict[str, Any]:
        if ar_params["type"] == "annotate_task":
            with self._task_cache_limiter.using_cache_for_task(
                ar_params["task"],
                with_chunks=True,
                owner=_TaskCacheLimiter._CacheOwner.INTERACTOR,
            ):
                return self._calculate_result_for_annotate_task_ar(ar_id, ar_params)
        elif ar_params["type"] == "annotate_frame":
            with self._task_cache_limiter.using_cache_for_task(
                ar_params["task"], with_chunks=False
            ):
                return self._calculate_result_for_annotate_frame_ar(ar_id, ar_params)
        else:
            raise _BadArError(f"unsupported type: {ar_params['type']!r}")

    def _create_annotation_mapper_for_detection_ar(
        self, ar_params: dict, ds_labels: Sequence[models.ILabel]
    ) -> _AnnotationMapper:
        spec_nm = _SpecNameMapping.from_api(
            {
                k: models.LabelMappingEntryRequest._from_openapi_data(**v)
                for k, v in ar_params["mapping"].items()
            }
        )

        return _AnnotationMapper(
            self._client.logger,
            self._function_spec.labels,
            ds_labels,
            allow_unmatched_labels=False,
            spec_nm=spec_nm,
            conv_mask_to_poly=ar_params["conv_mask_to_poly"],
        )

    def _create_detection_function_context(
        self, ar_params: dict, frame_name: str
    ) -> cvataa.DetectionFunctionContext:
        return _DetectionFunctionContextImpl(
            frame_name=frame_name,
            conf_threshold=ar_params["threshold"],
            conv_mask_to_poly=ar_params["conv_mask_to_poly"],
        )

    def _calculate_result_for_annotate_task_ar(self, ar_id: str, ar_params) -> dict[str, Any]:
        ds = cvatds.TaskDataset(self._client, ar_params["task"], load_annotations=False)

        # Fetching the dataset might take a while, so do a progress update to let the server
        # know we're still alive.
        self._update_ar(ar_id, 0)
        last_update_timestamp = datetime.now(tz=timezone.utc)

        mapper = self._create_annotation_mapper_for_detection_ar(ar_params, ds.labels)

        all_annotations = models.PatchedLabeledDataRequest(tags=[], shapes=[])

        for sample_index, sample in enumerate(ds.samples):
            context = self._create_detection_function_context(ar_params, sample.frame_name)
            annotations = self._executor.result(
                self._executor.submit(_worker_job_detect, context, sample.media.load_image())
            )

            tags, shapes = mapper.validate_and_remap(annotations, sample.frame_index)
            all_annotations.tags.extend(tags)
            all_annotations.shapes.extend(shapes)

            current_timestamp = datetime.now(tz=timezone.utc)

            if current_timestamp >= last_update_timestamp + _UPDATE_INTERVAL:
                self._update_ar(ar_id, (sample_index + 1) / len(ds.samples))
                last_update_timestamp = current_timestamp

            # Interactive requests are time sensitive, so if there are any,
            # we have to put the current AR on hold and process them ASAP.
            self._process_available_ars(REQUEST_CATEGORY_INTERACTIVE)

        return {"annotations": all_annotations}

    def _calculate_result_for_annotate_frame_ar(self, ar_id: str, ar_params) -> dict[str, Any]:
        sample, ds_labels = self._get_sample_from_ar_params(ar_params)

        mapper = self._create_annotation_mapper_for_detection_ar(ar_params, ds_labels)

        context = self._create_detection_function_context(ar_params, sample.frame_name)

        annotations = self._executor.result(
            self._executor.submit(_worker_job_detect, context, sample.media.load_image())
        )

        tags, shapes = mapper.validate_and_remap(annotations, sample.frame_index)
        return {"annotations": models.PatchedLabeledDataRequest(tags=tags, shapes=shapes)}

    def _calculate_result_for_tracking_ar(self, ar_id: str, ar_params) -> dict[str, Any]:
        allow_chunks = self._tracker_allow_chunk_preload
        if ar_params["type"] == "init_tracking":
            with self._task_cache_limiter.using_cache_for_task(
                ar_params["task"],
                with_chunks=allow_chunks,
                owner=_TaskCacheLimiter._CacheOwner.TRACKER,
            ):
                return self._calculate_result_for_init_tracking_ar(ar_id, ar_params)
        elif ar_params["type"] == "track":
            with self._task_cache_limiter.using_cache_for_task(
                ar_params["task"],
                with_chunks=allow_chunks,
                owner=_TaskCacheLimiter._CacheOwner.TRACKER,
            ):
                return self._calculate_result_for_track_ar(ar_id, ar_params)
        else:
            raise _BadArError(f"unsupported type: {ar_params['type']!r}")

    def _tracker_log_context(
        self,
        *,
        ar_id: str | None = None,
        ar_params: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        context: dict[str, Any] = {}
        if ar_id:
            context["ar_id"] = ar_id
        if ar_params:
            context["ar_type"] = ar_params.get("type")
            context["task_id"] = ar_params.get("task")
            context["job_id"] = ar_params.get("job")
            run_id = ar_params.get("function_run_id") or ar_params.get("run_id")
            if run_id:
                context["function_run_id"] = run_id
            track_ids = ar_params.get("track_ids")
            if track_ids:
                context["track_ids"] = track_ids
            target_frame = ar_params.get("target_frame")
            if target_frame is not None:
                context["target_frame"] = target_frame
        return context

    def _log_tracker_event(
        self,
        phase: str,
        *,
        context: Optional[dict[str, Any]] = None,
        **payload: Any,
    ) -> None:
        if not self._tracker_verbose_logs:
            return

        record = {"component": "sam2_tracker", "phase": phase}
        if context:
            for key, value in context.items():
                if value is not None:
                    record[key] = value
        record.update(payload)
        self._client.logger.info(
            "SAM2_TRACKER_LOG %s", json.dumps(record, separators=(",", ":"))
        )

    def _tracker_frame_indexes(self, ar_params: dict[str, Any]) -> list[int]:
        frames = ar_params.get("frames")
        normalized: list[int] = []
        if isinstance(frames, list):
            for value in frames:
                try:
                    normalized.append(int(value))
                except (TypeError, ValueError):
                    continue

        if normalized:
            return normalized

        try:
            frame_index = int(ar_params["frame"])
        except (KeyError, TypeError, ValueError) as exc:
            raise _BadArError("AR is missing a valid frame index") from exc
        return [frame_index]

    def _load_tracker_image(
        self,
        sample,
        *,
        ar_type: str,
        task_id: int,
        dataset: Optional[cvatds.TaskDataset] = None,
        log_context: Optional[dict[str, Any]] = None,
    ) -> PIL.Image.Image:
        metadata: Optional[dict[str, Any]] = None
        metadata_internal: dict[str, Any] = {}
        if self._tracker_verbose_logs and dataset is not None:
            metadata, metadata_internal = self._build_dataset_fetch_metadata(
                dataset, sample.frame_index
            )

        if not self._tracker_verbose_logs:
            return sample.media.load_image()

        chunk_cached_before = metadata_internal.get("chunk_cached_before")
        chunk_id = metadata_internal.get("chunk_id")
        chunk_zip_path = metadata_internal.get("chunk_zip_path")

        start = time.perf_counter()
        image = sample.media.load_image()
        elapsed_ms = (time.perf_counter() - start) * 1000

        chunk_cached_after = chunk_cached_before
        if dataset is not None and chunk_id is not None:
            downloaded_chunks = getattr(dataset, "_downloaded_chunk_indexes", set())
            chunk_cached_after = chunk_id in downloaded_chunks

        self._log_tracker_event(
            "frame_fetch",
            context=log_context,
            task_id=task_id,
            frame_index=sample.frame_index,
            ar_type=ar_type,
            wall_ms=round(elapsed_ms, 3),
        )
        if metadata is not None:
            metadata = metadata.copy()
            cached_before_flag = bool(chunk_cached_before)
            cached_after_flag = bool(chunk_cached_after)
            # 2025-11-22: Treat dataset_fetch.download_ms as pure I/O (zip/HTTP)
            # and reserve decode_ms for explicit CPU decode stages. Under the
            # current SAM2 tracker pipeline, TaskDataset does not perform eager
            # JPEG decode, so decode_ms remains 0.0 while download_ms accounts
            # for chunk reads and lightweight header parsing.
            download_ms = elapsed_ms
            decode_ms = 0.0
            metadata.update(
                {
                    "frames": [sample.frame_index],
                    "cached": cached_before_flag,
                    "cached_after": cached_after_flag,
                    "download_ms": round(download_ms, 3),
                    "decode_ms": round(decode_ms, 3),
                    "wall_ms": round(elapsed_ms, 3),
                }
            )
            self._log_tracker_event("dataset_fetch", context=log_context, **metadata)

            if (
                not cached_before_flag
                and cached_after_flag
                and chunk_zip_path is not None
                and chunk_zip_path.exists()
            ):
                cost_bytes = chunk_zip_path.stat().st_size
                self._log_tracker_event(
                    "dataset_cache",
                    context=log_context,
                    event="put",
                    chunk_id=chunk_id,
                    cost_bytes=cost_bytes,
                )
        return image

    def _build_dataset_fetch_metadata(
        self,
        dataset: cvatds.TaskDataset,
        frame_index: int,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        task_obj = getattr(dataset, "_task", None)
        chunk_size = getattr(task_obj, "data_chunk_size", None)
        chunk_id = None
        cached = None
        chunk_zip_path = None
        if isinstance(chunk_size, int) and chunk_size > 0:
            chunk_id = frame_index // chunk_size
            downloaded = getattr(dataset, "_downloaded_chunk_indexes", set())
            cached = chunk_id in downloaded
            chunk_dir = getattr(dataset, "_chunk_dir", None)
            if chunk_dir is not None:
                chunk_zip_path = Path(chunk_dir) / f"{chunk_id}.zip"
        else:
            chunk_id = frame_index
            cached = False
        loader = getattr(getattr(dataset, "_load_frame_image", None), "__name__", None)
        chunk_cache_mode = getattr(dataset, "chunk_cache_mode", None)
        metadata = {
            "chunk_id": chunk_id,
            "cached": cached,
            "chunk_size": chunk_size,
            "chunk_type": getattr(task_obj, "data_original_chunk_type", None),
            "chunk_preload_enabled": chunk_cache_mode
            == cvatds.ChunkCacheMode.PREFETCH_CHUNKS_ONCE,
            "frame_loader": loader,
        }
        internals = {
            "chunk_cached_before": cached,
            "chunk_id": chunk_id,
            "chunk_zip_path": chunk_zip_path,
        }
        return metadata, internals

    def _calculate_result_for_init_tracking_ar(self, ar_id: str, ar_params) -> dict[str, Any]:
        dataset = self._tracker_datasets.get(
            ar_params["task"],
            allow_chunks=self._tracker_allow_chunk_preload,
        )
        sample, _ = self._get_sample_from_ar_params(ar_params, dataset=dataset)
        log_context = self._tracker_log_context(ar_id=ar_id, ar_params=ar_params)

        def convert_shape(shape: dict) -> cvataa.TrackableShape:
            if shape["type"] not in self._function_spec.supported_shape_types:
                raise _BadArError(f"Unsupported shape type {shape['type']!r}")
            return cvataa.TrackableShape(type=shape["type"], points=shape["points"])

        shapes = list(map(convert_shape, ar_params["shapes"]))

        states = self._executor.result(
            self._executor.submit(
                _worker_job_init_tracking,
                ar_params["task"],
                self._load_tracker_image(
                    sample,
                    ar_type=ar_params["type"],
                    task_id=ar_params["task"],
                    dataset=dataset,
                    log_context=log_context,
                ),
                shapes,
            )
        )

        return {"states": states}

    def _calculate_result_for_track_ar(self, ar_id: str, ar_params) -> dict[str, Any]:
        dataset = self._tracker_datasets.get(
            ar_params["task"],
            allow_chunks=self._tracker_allow_chunk_preload,
        )
        frame_indexes = self._tracker_frame_indexes(ar_params)
        log_context = self._tracker_log_context(ar_id=ar_id, ar_params=ar_params)

        self._prefetch_tracker_chunks(
            dataset=dataset,
            frame_indexes=frame_indexes,
            ar_params=ar_params,
            log_context=log_context,
        )

        states = ar_params["states"]
        frame_payloads: list[dict[str, Any]] = []
        frame_latencies_ms: list[float] = []
        chunk_start = time.perf_counter()
        use_double_buffer = _TRACKER_DOUBLE_BUFFER and len(frame_indexes) > 1
        # 2025-11-22: dataset_fetch を track とオーバーラップさせるため、prefetch を優先。
        # ダブルバッファを使いたい場合は `SAM2_TRACKER_PREFETCH_FRAMES=0` で明示的に切り替える。
        if _TRACKER_PREFETCH_FRAMES and len(frame_indexes) > 1:
            # 先行デコードをキューに積み、track 中に次フレームをデコードしてオーバーラップさせる。
            max_workers = max(2, min(len(frame_indexes), 4))

            def _prepare(frame_idx: int):
                sample, _ = self._get_sample_from_ar_params(
                    ar_params,
                    dataset=dataset,
                    frame_override=frame_idx,
                )
                image = self._load_tracker_image(
                    sample,
                    ar_type=ar_params["type"],
                    task_id=ar_params["task"],
                    dataset=dataset,
                    log_context=log_context,
                )
                return sample, image

            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as prefetch_pool:
                frame_iter = iter(frame_indexes)
                first_idx = next(frame_iter)
                future = prefetch_pool.submit(_prepare, first_idx)

                for next_idx in frame_iter:
                    frame_start = time.perf_counter()
                    sample, image = future.result()
                    future = prefetch_pool.submit(_prepare, next_idx)

                    shapes = self._executor.result(
                        self._executor.submit(
                            _worker_job_track,
                            ar_params["task"],
                            image,
                            states,
                        )
                    )
                    frame_payloads.append(
                        {
                            "frame": sample.frame_index,
                            "shapes": [attrs.asdict(shape) if shape else None for shape in shapes],
                        }
                    )
                    frame_latencies_ms.append(round((time.perf_counter() - frame_start) * 1000, 3))

                # last frame
                frame_start = time.perf_counter()
                sample, image = future.result()
                shapes = self._executor.result(
                    self._executor.submit(
                        _worker_job_track,
                        ar_params["task"],
                        image,
                        states,
                    )
                )
                frame_payloads.append(
                    {
                        "frame": sample.frame_index,
                        "shapes": [attrs.asdict(shape) if shape else None for shape in shapes],
                    }
                )
                frame_latencies_ms.append(round((time.perf_counter() - frame_start) * 1000, 3))
        elif use_double_buffer:
            frames_and_images: list[tuple[int, PIL.Image.Image]] = []
            for frame_index in frame_indexes:
                sample, _ = self._get_sample_from_ar_params(
                    ar_params,
                    dataset=dataset,
                    frame_override=frame_index,
                )
                image = self._load_tracker_image(
                    sample,
                    ar_type=ar_params["type"],
                    task_id=ar_params["task"],
                    dataset=dataset,
                    log_context=log_context,
                )
                frames_and_images.append((sample.frame_index, image))

            frame_payloads, frame_latencies_ms = self._executor.result(
                self._executor.submit(
                    _worker_job_track_double_buffer,
                    ar_params["task"],
                    frames_and_images,
                    states,
                )
            )
        else:
            for frame_index in frame_indexes:
                frame_start = time.perf_counter()
                sample, _ = self._get_sample_from_ar_params(
                    ar_params,
                    dataset=dataset,
                    frame_override=frame_index,
                )
                shapes = self._executor.result(
                    self._executor.submit(
                        _worker_job_track,
                        ar_params["task"],
                        self._load_tracker_image(
                            sample,
                            ar_type=ar_params["type"],
                            task_id=ar_params["task"],
                            dataset=dataset,
                            log_context=log_context,
                        ),
                        states,
                    )
                )
                frame_payloads.append(
                    {
                        "frame": sample.frame_index,
                        "shapes": [attrs.asdict(shape) if shape else None for shape in shapes],
                    }
                )
                frame_latencies_ms.append(round((time.perf_counter() - frame_start) * 1000, 3))

        if self._tracker_verbose_logs:
            frame_loader = getattr(getattr(dataset, "_load_frame_image", None), "__name__", None)
            self._log_tracker_event(
                "track_chunk",
                context=log_context,
                ar_id=ar_id,
                task_id=ar_params["task"],
                job_id=ar_params.get("job"),
                frame_indexes=frame_indexes,
                total_frames=len(frame_indexes),
                frame_latencies_ms=frame_latencies_ms,
                chunk_preload_enabled=
                getattr(dataset, "chunk_cache_mode", None)
                == cvatds.ChunkCacheMode.PREFETCH_CHUNKS_ONCE,
                double_buffer=use_double_buffer,
                frame_loader=frame_loader,
                wall_ms=round((time.perf_counter() - chunk_start) * 1000, 3),
            )

        result_payload: dict[str, Any] = {
            "states": states,
            "frames": frame_payloads,
        }
        if len(frame_payloads) == 1:
            result_payload["shapes"] = frame_payloads[0]["shapes"]
        return result_payload

    def _prefetch_tracker_chunks(
        self,
        *,
        dataset: cvatds.TaskDataset,
        frame_indexes: list[int],
        ar_params: dict[str, Any],
        log_context: dict[str, Any],
    ) -> None:
        if getattr(dataset, "chunk_cache_mode", None) != cvatds.ChunkCacheMode.PREFETCH_CHUNKS_ONCE:
            return

        task_obj = getattr(dataset, "_task", None)
        cache_manager = getattr(dataset, "_cache_manager", None)
        if task_obj is None or cache_manager is None:
            return

        chunk_size = getattr(task_obj, "data_chunk_size", None)
        if not chunk_size:
            return

        chunk_ids = {frame_index // chunk_size for frame_index in frame_indexes}
        downloaded: set[int] = getattr(dataset, "_downloaded_chunk_indexes", set())
        pending = [cid for cid in sorted(chunk_ids) if cid not in downloaded]
        if not pending:
            return

        batch_size = ar_params.get("batch_size") or len(frame_indexes)
        max_workers = (
            _TRACKER_PREFETCH_PARALLEL if _TRACKER_PREFETCH_PARALLEL > 0 else batch_size * 2
        )
        max_workers = max(1, max_workers)

        start = time.perf_counter()
        self._log_tracker_event(
            "prefetch",
            context=log_context,
            event="start",
            pending_chunks=len(pending),
            max_workers=max_workers,
        )

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [
                pool.submit(cache_manager.ensure_chunk, task_obj, chunk_id) for chunk_id in pending
            ]
            for future in concurrent.futures.as_completed(futures):
                future.result()

        downloaded.update(pending)
        dataset._downloaded_chunk_indexes = downloaded  # type: ignore[attr-defined]
        self._log_tracker_event(
            "prefetch",
            context=log_context,
            event="done",
            pending_chunks=len(pending),
            wall_ms=round((time.perf_counter() - start) * 1000, 3),
        )

    def _calculate_result_for_interactor_ar(self, ar_id: str, ar_params) -> dict[str, Any]:
        if ar_params["type"] != "interact":
            raise _BadArError(f"unsupported type: {ar_params['type']!r}")

        with self._task_cache_limiter.using_cache_for_task(
            ar_params["task"],
            with_chunks=True,
            owner=_TaskCacheLimiter._CacheOwner.INTERACTOR,
        ):
            dataset = self._interactor_datasets.get(ar_params["task"])
            sample, _ = self._get_sample_from_ar_params(ar_params, dataset=dataset)
            prompt = _build_interaction_prompt(ar_params)
            context = _InteractorFunctionContextImpl(
                task_id=ar_params["task"],
                job_id=ar_params.get("job"),
                frame_index=sample.frame_index,
                job_frame_index=ar_params["frame"],
                frame_name=sample.frame_name,
            )
            prediction = self._executor.result(
                self._executor.submit(
                    _worker_job_interact,
                    context,
                    sample.media.load_image(),
                    prompt,
                )
            )

        return _serialize_mask_prediction(prediction)

    def _get_sample_from_ar_params(
        self,
        ar_params,
        *,
        dataset: cvatds.TaskDataset | None = None,
        media_download_policy: cvatds.MediaDownloadPolicy = cvatds.MediaDownloadPolicy.FETCH_FRAMES_ON_DEMAND,
        frame_override: int | None = None,
    ):
        if dataset is None:
            dataset = cvatds.TaskDataset(
                self._client,
                ar_params["task"],
                load_annotations=False,
                media_download_policy=media_download_policy,
            )

        if frame_override is None:
            frame_value = ar_params["frame"]
        else:
            frame_value = frame_override

        try:
            frame_index = int(frame_value)
        except (TypeError, ValueError, KeyError) as exc:
            raise _BadArError("Tracking AR is missing frame metadata") from exc

        try:
            sample = dataset.get_sample_by_frame_index(frame_index)
        except KeyError as exc:
            raise _BadArError(f"Frame with index {frame_index} does not exist in the task") from exc

        return sample, dataset.labels

    def _update_ar(self, ar_id: str, progress: float) -> None:
        self._client.logger.info("Updating AR %r progress to %.2f%%", ar_id, progress * 100)
        self._client.api_client.call_api(
            "/api/functions/queues/{queue_id}/requests/{request_id}/update",
            "POST",
            path_params={"queue_id": f"function:{self._function_id}", "request_id": ar_id},
            body={"agent_id": self._agent_id, "progress": progress},
        )


def run_agent(
    client: Client,
    function_loader: FunctionLoader,
    function_id: int,
    *,
    burst: bool,
    max_tasks_with_chunks: int = 1,
    max_tasks_without_chunks: int = 10,
    tracker_allow_chunk_preload: bool = False,
    tracker_verbose_logs: bool | None = None,
) -> None:
    with (
        _RecoverableExecutor(
            initializer=_worker_init,
            initargs=[function_loader, _default_tracking_state_id_generator],
        ) as executor,
        tempfile.TemporaryDirectory() as cache_dir,
    ):
        client.config.cache_dir = Path(cache_dir, "cache")
        client.logger.info("Will store cache at %s", client.config.cache_dir)

        try:
            agent = _Agent(
                client,
                executor,
                function_id,
                max_tasks_with_chunks=max_tasks_with_chunks,
                max_tasks_without_chunks=max_tasks_without_chunks,
                tracker_allow_chunk_preload=tracker_allow_chunk_preload,
                tracker_verbose_logs=tracker_verbose_logs,
            )
        except ApiException as exc:
            raise_if_functions_api_missing(exc, action="Running native function agents")
        agent.run(burst=burst)
