from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque

from django.conf import settings
from django.utils import timezone
from django_rq.queues import get_redis_connection
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

_QUEUE_CHANNEL_PREFIX = "cvat:function_queue"
_REQUEST_CHANNEL_PREFIX = "cvat:annotation_request"


def publish_queue_event(*, function_id: int, category: str, request_id: str) -> None:
    payload = {
        "function_id": function_id,
        "category": category,
        "request_id": request_id,
        "published_at": timezone.now().isoformat(),
    }
    _publish(_queue_channel(function_id), payload)


def publish_request_event(
    *,
    request_id: str,
    status: str,
    function_id: int | None = None,
    category: str | None = None,
) -> None:
    payload = {
        "request_id": request_id,
        "status": status,
        "function_id": function_id,
        "category": category,
        "published_at": timezone.now().isoformat(),
    }
    _publish(_request_channel(request_id), payload)


def queue_listener(function_id: int) -> "RedisNotificationListener":
    return RedisNotificationListener(channel=_queue_channel(function_id), threaded=False)


def request_listener(request_id: str) -> "RedisNotificationListener":
    return RedisNotificationListener(channel=_request_channel(request_id), threaded=True)


def _queue_channel(function_id: int) -> str:
    return f"{_QUEUE_CHANNEL_PREFIX}:{function_id}"


def _request_channel(request_id: str) -> str:
    return f"{_REQUEST_CHANNEL_PREFIX}:{request_id}"


def _publish(channel: str, payload: dict[str, Any]) -> None:
    try:
        connection = get_redis_connection(settings.REDIS_INMEM_SETTINGS)
        connection.publish(channel, json.dumps(payload))
    except Exception as exc:  # pragma: no cover - defensive logging
        logger.warning("Failed to publish %s event: %s", channel, exc, exc_info=True)


@dataclass
class _QueuedMessage:
    payload: dict[str, Any]


class RedisNotificationListener:
    """
    Subscribe to Redis pub/sub channels with optional background thread delivery.
    """

    def __init__(self, *, channel: str, threaded: bool) -> None:
        self._channel = channel
        self._threaded = threaded
        self._pubsub = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._queue: Deque[_QueuedMessage] = deque()
        self._condition = threading.Condition()
        self._connected = False

    def __enter__(self) -> "RedisNotificationListener":
        try:
            connection = get_redis_connection(settings.REDIS_INMEM_SETTINGS)
        except Exception as exc:  # pragma: no cover - Redis might be down
            logger.warning(
                "Redis connection unavailable for channel %s: %s", self._channel, exc, exc_info=True
            )
            return self

        self._pubsub = connection.pubsub(ignore_subscribe_messages=True)
        try:
            self._pubsub.subscribe(self._channel)
        except RedisError as exc:  # pragma: no cover - unlikely
            logger.warning("Failed to subscribe to %s: %s", self._channel, exc, exc_info=True)
            self._pubsub.close()
            self._pubsub = None
            return self

        self._connected = True
        if self._threaded:
            self._thread = threading.Thread(target=self._listen_forever, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self._stop_event.set()
        try:
            if self._pubsub:
                self._pubsub.close()
        finally:
            if self._thread and self._thread.is_alive():
                self._thread.join(timeout=1.0)

    @property
    def is_connected(self) -> bool:
        return self._connected

    def _listen_forever(self) -> None:
        assert self._pubsub
        try:
            for message in self._pubsub.listen():
                if self._stop_event.is_set():
                    break
                payload = self._decode_message(message)
                if not payload:
                    continue
                with self._condition:
                    self._queue.append(_QueuedMessage(payload=payload))
                    self._condition.notify_all()
        except RedisError:
            logger.warning("Redis pub/sub listener crashed on %s", self._channel, exc_info=True)
        finally:
            with self._condition:
                self._condition.notify_all()

    def next_message(self, *, timeout: float) -> dict[str, Any] | None:
        """
        Block until a message arrives or timeout (seconds) elapses.
        """

        if not self._connected:
            time.sleep(timeout)
            return None

        if self._threaded:
            with self._condition:
                message_ready = self._condition.wait_for(
                    lambda: bool(self._queue) or self._stop_event.is_set(),
                    timeout=timeout,
                )
                if not message_ready or not self._queue:
                    return None

                queued = self._queue.popleft()
                return queued.payload

        assert self._pubsub
        try:
            message = self._pubsub.get_message(
                timeout=timeout,
                ignore_subscribe_messages=True,
            )
        except RedisError:
            logger.warning("Redis pub/sub read failed on %s", self._channel, exc_info=True)
            self._connected = False
            return None

        return self._decode_message(message)

    def _decode_message(self, message: Any) -> dict[str, Any] | None:
        if not message or message.get("type") != "message":
            return None

        payload_raw = message.get("data")
        if isinstance(payload_raw, bytes):
            payload_raw = payload_raw.decode("utf-8", errors="ignore")

        if isinstance(payload_raw, (str, bytes)):
            try:
                return json.loads(payload_raw)
            except json.JSONDecodeError:
                logger.debug("Failed to decode pub/sub payload on %s", self._channel)
        return None
