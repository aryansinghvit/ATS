"""Real-time event fan-out for Kanban SSE streams.

``redis`` (default) uses Redis pub/sub so events raised in any API process or worker reach every
connected recruiter; ``memory`` keeps everything in-process (single-process dev and tests).
Publishing is best-effort: a broker outage never fails the request that produced the event.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache
from typing import Any, Protocol

import redis.asyncio as redis_asyncio

from app.core.config import get_settings

logger = logging.getLogger(__name__)


def pipeline_channel(tenant_id: uuid.UUID, job_id: uuid.UUID) -> str:
    return f"ats:pipeline:{tenant_id}:{job_id}"


class Subscription(Protocol):
    async def get(self, timeout: float) -> dict[str, Any] | None:
        """Next event, or None if nothing arrived within ``timeout`` seconds."""


class EventBroker(Protocol):
    async def publish(self, channel: str, event: dict[str, Any]) -> None: ...

    def subscribe(self, channel: str) -> Any:
        """Async context manager yielding a :class:`Subscription`."""


class _QueueSubscription:
    def __init__(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._queue = queue

    async def get(self, timeout: float) -> dict[str, Any] | None:
        try:
            return await asyncio.wait_for(self._queue.get(), timeout)
        except TimeoutError:
            return None


class InMemoryEventBroker:
    def __init__(self, max_backlog: int = 1000) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)
        self._max_backlog = max_backlog

    async def publish(self, channel: str, event: dict[str, Any]) -> None:
        for queue in list(self._subscribers.get(channel, ())):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning("Dropping event for a slow subscriber on %s", channel)

    @asynccontextmanager
    async def subscribe(self, channel: str) -> AsyncIterator[_QueueSubscription]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._max_backlog)
        self._subscribers[channel].add(queue)
        try:
            yield _QueueSubscription(queue)
        finally:
            self._subscribers[channel].discard(queue)
            if not self._subscribers[channel]:
                del self._subscribers[channel]


class _RedisSubscription:
    def __init__(self, pubsub: Any) -> None:
        self._pubsub = pubsub

    async def get(self, timeout: float) -> dict[str, Any] | None:
        message = await self._pubsub.get_message(ignore_subscribe_messages=True, timeout=timeout)
        if message is None:
            return None
        return json.loads(message["data"])


class RedisEventBroker:
    def __init__(self, url: str) -> None:
        self._redis = redis_asyncio.from_url(url, decode_responses=True)

    async def publish(self, channel: str, event: dict[str, Any]) -> None:
        await self._redis.publish(channel, json.dumps(event, default=str))

    @asynccontextmanager
    async def subscribe(self, channel: str) -> AsyncIterator[_RedisSubscription]:
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(channel)
        try:
            yield _RedisSubscription(pubsub)
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()

    async def aclose(self) -> None:
        await self._redis.aclose()


async def publish_safely(broker: EventBroker, channel: str, event: dict[str, Any]) -> None:
    try:
        await broker.publish(channel, event)
    except Exception:  # noqa: BLE001 - real-time fan-out must never fail the caller
        logger.exception("Failed to publish %s event on %s", event.get("type"), channel)


@lru_cache
def get_event_broker() -> EventBroker:
    settings = get_settings()
    if settings.EVENT_BROKER_BACKEND == "memory":
        return InMemoryEventBroker()
    return RedisEventBroker(settings.REDIS_URL)
