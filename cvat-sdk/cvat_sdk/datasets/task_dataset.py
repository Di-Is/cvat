# Copyright (C) CVAT.ai Corporation
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import io
import zipfile
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor

import PIL.Image

import cvat_sdk.core
import cvat_sdk.core.exceptions
import cvat_sdk.models as models
from cvat_sdk.datasets.caching import CacheManager, UpdatePolicy, make_cache_manager
from cvat_sdk.datasets.common import (
    ChunkCacheMode,
    FrameAnnotations,
    MediaDownloadPolicy,
    MediaElement,
    Sample,
    UnsupportedDatasetError,
)

_NUM_DOWNLOAD_THREADS = 4


class TaskDataset:
    """
    Represents a task on a CVAT server as a collection of samples.

    Each sample corresponds to one frame in the task, and provides access to
    the corresponding annotations and media data. Deleted frames are omitted.

    This class caches all data and annotations for the task on the local file system
    during construction.

    Limitations:

    * Only tasks with image (not video) data are supported at the moment.
    * Track annotations are currently not accessible.
    """

    class _TaskMediaElement(MediaElement):
        def __init__(self, dataset: TaskDataset, frame_index: int) -> None:
            self._dataset = dataset
            self._frame_index = frame_index

        def load_image(self) -> PIL.Image.Image:
            return self._dataset._load_frame_image(self._frame_index)

        def load_encoded_bytes(self) -> bytes:
            return self._dataset._load_frame_bytes(self._frame_index)

    def __init__(
        self,
        client: cvat_sdk.core.Client,
        task_id: int,
        *,
        update_policy: UpdatePolicy = UpdatePolicy.IF_MISSING_OR_STALE,
        load_annotations: bool = True,
        media_download_policy: MediaDownloadPolicy = MediaDownloadPolicy.PRELOAD_ALL,
        chunk_cache_mode: ChunkCacheMode | None = None,
    ) -> None:
        """
        Creates a dataset corresponding to the task with ID `task_id` on the
        server that `client` is connected to.

        `update_policy` determines when and if the local cache will be updated.

        `load_annotations` determines whether annotations will be loaded from
        the server. If set to False, the `annotations` field in the samples will
        be set to None.

        `media_download_policy` determines when media data is downloaded.

        `chunk_cache_mode` selects whether chunks are prefetched and cached once or fetched on
        demand. When provided, it overrides `media_download_policy` so callers do not have to
        translate between the enums manually.

        `MediaDownloadPolicy.FETCH_FRAMES_ON_DEMAND` may not be used with with `UpdatePolicy.NEVER`,
        as it requires network access.
        """

        self._logger = client.logger

        cache_manager = make_cache_manager(client, update_policy)
        self._cache_manager = cache_manager
        self._task = cache_manager.retrieve_task(task_id)
        self._chunk_dir = cache_manager.chunk_dir(task_id)
        self._downloaded_chunk_indexes: set[int] = set()

        if not self._task.size or not self._task.data_chunk_size:
            raise UnsupportedDatasetError("The task has no data")

        self._logger.info("Fetching labels...")
        self._labels = tuple(self._task.get_labels())

        data_meta = cache_manager.ensure_task_model(
            self._task.id,
            "data_meta.json",
            models.DataMetaRead,
            self._task.get_meta,
            "data metadata",
        )

        active_frame_indexes = set(range(self._task.size)) - set(data_meta.deleted_frames)

        if chunk_cache_mode is not None:
            if chunk_cache_mode == ChunkCacheMode.PREFETCH_CHUNKS_ONCE:
                media_download_policy = MediaDownloadPolicy.PREFETCH_CHUNKS_ONCE
            elif chunk_cache_mode == ChunkCacheMode.FETCH_ON_DEMAND:
                media_download_policy = MediaDownloadPolicy.FETCH_FRAMES_ON_DEMAND
            else:
                raise AssertionError("Unknown chunk cache mode")
        else:
            if media_download_policy == MediaDownloadPolicy.PREFETCH_CHUNKS_ONCE:
                chunk_cache_mode = ChunkCacheMode.PREFETCH_CHUNKS_ONCE
            elif media_download_policy == MediaDownloadPolicy.FETCH_FRAMES_ON_DEMAND:
                chunk_cache_mode = ChunkCacheMode.FETCH_ON_DEMAND

        self._chunk_cache_mode = chunk_cache_mode

        if media_download_policy == MediaDownloadPolicy.PRELOAD_ALL:
            needed_chunks = {index // self._task.data_chunk_size for index in active_frame_indexes}
            self._ensure_chunks(task_id, needed_chunks)
            self._load_frame_image = self._load_frame_image_from_cache
            self._load_frame_bytes_impl = self._frame_bytes_from_cache
        elif media_download_policy == MediaDownloadPolicy.PREFETCH_CHUNKS_ONCE:
            if self._task.data_original_chunk_type != "imageset":
                raise UnsupportedDatasetError(
                    "Chunk prefetching is only supported for tasks with image chunks"
                )
            self._load_frame_image = self._load_frame_image_from_lazy_chunk_cache
            self._load_frame_bytes_impl = self._frame_bytes_from_lazy_chunk_cache
        elif media_download_policy == MediaDownloadPolicy.FETCH_FRAMES_ON_DEMAND:
            assert update_policy != UpdatePolicy.NEVER
            self._load_frame_image = self._load_frame_image_from_server
            self._load_frame_bytes_impl = self._frame_bytes_from_server
        else:
            assert False, "Unknown media download policy"

        if load_annotations:
            self._load_annotations(cache_manager, sorted(active_frame_indexes))
        else:
            self._frame_annotations = {
                frame_index: None for frame_index in sorted(active_frame_indexes)
            }

        # TODO: tracks?

        is_imageset = self._task.data_original_chunk_type == "imageset"

        self._samples = [
            Sample(
                frame_index=k,
                frame_name=data_meta.frames[k if is_imageset else 0].name,
                annotations=v,
                media=self._TaskMediaElement(self, k),
            )
            for k, v in self._frame_annotations.items()
        ]

        # Build an index from frame index to sample for fast lookups.
        self._samples_by_frame_index: dict[int, Sample] = {
            sample.frame_index: sample for sample in self._samples
        }

    @property
    def chunk_cache_mode(self) -> ChunkCacheMode | None:
        return self._chunk_cache_mode

    def _ensure_chunks(self, task_id, chunk_indexes):
        if self._task.data_original_chunk_type != "imageset":
            raise UnsupportedDatasetError(
                f"Preloading media data is only supported for tasks with image chunks;"
                f" current chunk type is {self._task.data_original_chunk_type!r}"
            )

        self._logger.info("Downloading chunks...")

        self._chunk_dir.mkdir(exist_ok=True, parents=True)

        with ThreadPoolExecutor(_NUM_DOWNLOAD_THREADS) as pool:

            def ensure_chunk(chunk_index):
                self._cache_manager.ensure_chunk(self._task, chunk_index)

            for _ in pool.map(ensure_chunk, sorted(chunk_indexes)):
                # just need to loop through all results so that any exceptions are propagated
                pass

        self._logger.info("All chunks downloaded")
        self._downloaded_chunk_indexes.update(chunk_indexes)

    def _load_annotations(self, cache_manager: CacheManager, frame_indexes: Iterable[int]) -> None:
        annotations = cache_manager.ensure_task_model(
            self._task.id,
            "annotations.json",
            models.LabeledData,
            self._task.get_annotations,
            "annotations",
        )

        self._frame_annotations = {frame_index: FrameAnnotations() for frame_index in frame_indexes}

        for tag in annotations.tags:
            # Some annotations may belong to deleted frames; skip those.
            if tag.frame in self._frame_annotations:
                self._frame_annotations[tag.frame].tags.append(tag)

        for shape in annotations.shapes:
            if shape.frame in self._frame_annotations:
                self._frame_annotations[shape.frame].shapes.append(shape)

    @property
    def labels(self) -> Sequence[models.ILabel]:
        """
        Returns the labels configured in the task.

        Clients must not modify the object returned by this property or its components.
        """
        return self._labels

    @property
    def samples(self) -> Sequence[Sample]:
        """
        Returns a sequence of all samples, in order of their frame indices.

        Note that the frame indices may not be contiguous, as deleted frames will not be included.

        Clients must not modify the object returned by this property or its components.
        """
        return self._samples

    def _frame_bytes_from_cache(self, frame_index: int) -> bytes:
        assert frame_index in self._frame_annotations

        chunk_index = frame_index // self._task.data_chunk_size
        member_index = frame_index % self._task.data_chunk_size

        with zipfile.ZipFile(self._chunk_dir / f"{chunk_index}.zip", "r") as chunk_zip:
            with chunk_zip.open(chunk_zip.infolist()[member_index]) as chunk_member:
                return chunk_member.read()

    def _load_frame_image_from_cache(self, frame_index: int) -> PIL.Image.Image:
        encoded = self._frame_bytes_from_cache(frame_index)
        return self._image_from_encoded(encoded)

    def _load_frame_image_from_lazy_chunk_cache(self, frame_index: int) -> PIL.Image:
        assert frame_index in self._frame_annotations

        chunk_index = frame_index // self._task.data_chunk_size
        if chunk_index not in self._downloaded_chunk_indexes:
            self._ensure_chunks(self._task.id, {chunk_index})

        return self._load_frame_image_from_cache(frame_index)

    def _frame_bytes_from_lazy_chunk_cache(self, frame_index: int) -> bytes:
        assert frame_index in self._frame_annotations

        chunk_index = frame_index // self._task.data_chunk_size
        if chunk_index not in self._downloaded_chunk_indexes:
            self._ensure_chunks(self._task.id, {chunk_index})

        return self._frame_bytes_from_cache(frame_index)

    def _load_frame_image_from_server(self, frame_index: int) -> PIL.Image:
        assert frame_index in self._frame_annotations

        encoded = self._frame_bytes_from_server(frame_index)
        return self._image_from_encoded(encoded)

    def _frame_bytes_from_server(self, frame_index: int) -> bytes:
        assert frame_index in self._frame_annotations

        frame_io = self._task.get_frame(frame_index, quality="original")
        if isinstance(frame_io, io.BytesIO):
            return frame_io.getbuffer().tobytes()
        return frame_io.read()

    def _image_from_encoded(self, encoded: bytes) -> PIL.Image.Image:
        buffer = io.BytesIO(encoded)
        image = PIL.Image.open(buffer)
        image.info["_encoded_bytes"] = encoded
        return image

    def _load_frame_bytes(self, frame_index: int) -> bytes:
        return self._load_frame_bytes_impl(frame_index)

    def get_sample_by_frame_index(self, frame_index: int) -> Sample:
        """
        Returns the sample for the given frame index.

        Raises KeyError if the frame index does not exist in the dataset.
        """
        return self._samples_by_frame_index[frame_index]

    def get_sample_by_frame_index(self, frame_index: int) -> Sample:
        """
        Returns the sample for the given frame index.

        Raises KeyError if the frame index does not exist in the dataset.
        """
        for sample in self._samples:
            if sample.frame_index == frame_index:
                return sample
        raise KeyError(frame_index)
