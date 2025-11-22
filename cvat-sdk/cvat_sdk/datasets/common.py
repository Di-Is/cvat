# Copyright (C) CVAT.ai Corporation
#
# SPDX-License-Identifier: MIT

import abc
from enum import Enum, auto
from typing import Optional

import attrs
import attrs.validators
import PIL.Image

import cvat_sdk.core
import cvat_sdk.core.exceptions
import cvat_sdk.models as models


class UnsupportedDatasetError(cvat_sdk.core.exceptions.CvatSdkException):
    pass


@attrs.frozen
class FrameAnnotations:
    """
    Contains annotations that pertain to a single frame.
    """

    tags: list[models.LabeledImage] = attrs.Factory(list)
    shapes: list[models.LabeledShape] = attrs.Factory(list)


class MediaElement(metaclass=abc.ABCMeta):
    """
    The media part of a dataset sample.
    """

    @abc.abstractmethod
    def load_image(self) -> PIL.Image.Image:
        """
        Loads the media data and returns it as a PIL Image object.
        """
        ...

    def load_encoded_bytes(self) -> bytes:
        """
        Returns the encoded image bytes if available.
        Default implementation raises NotImplementedError.
        """
        raise NotImplementedError("Encoded bytes are not provided for this media element")


@attrs.frozen
class Sample:
    """
    Represents an element of a dataset.
    """

    frame_index: int
    """Index of the corresponding frame in its task."""

    frame_name: str
    """File name of the frame in its task."""

    annotations: Optional[FrameAnnotations]
    """
    Annotations belonging to the frame.

    Will be None if the dataset was created without loading annotations.
    """

    media: MediaElement
    """Media data of the frame."""


class MediaDownloadPolicy(Enum):
    """Defines policies controlling when media data is downloaded."""

    PRELOAD_ALL = auto()
    """Download and cache all media data when the dataset object is created."""

    PREFETCH_CHUNKS_ONCE = auto()
    """Download each chunk once when a frame from it is needed, then reuse the cached chunk."""

    FETCH_FRAMES_ON_DEMAND = auto()
    """Download the media element for each frame whenever MediaElement.load_* is invoked."""


class ChunkCacheMode(Enum):
    """Defines when dataset chunks are cached while iterating over frames."""

    PREFETCH_CHUNKS_ONCE = auto()
    """Download each chunk once per run and reuse the cached chunk afterwards."""

    FETCH_ON_DEMAND = auto()
    """Fetch frames as-needed without caching chunk archives locally."""
