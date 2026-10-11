"""Producer side of the ARQ communications queue (consumer: app.workers.communications_worker)."""

from __future__ import annotations

import uuid
from typing import Protocol

from arq import ArqRedis, create_pool
from arq.connections import RedisSettings

from app.core.config import get_settings

COMMUNICATIONS_QUEUE_NAME = "ats:communications"
GENERATE_SCREENING_TASK = "generate_screening"
EVALUATE_SCREENING_TASK = "evaluate_screening"


class CommunicationsQueue(Protocol):
    async def enqueue_screening_generation(
        self, tenant_id: uuid.UUID, application_id: uuid.UUID
    ) -> None: ...

    async def enqueue_screening_evaluation(
        self, tenant_id: uuid.UUID, screening_id: uuid.UUID
    ) -> None: ...


class ArqCommunicationsQueue:
    def __init__(self, redis_url: str) -> None:
        self._settings = RedisSettings.from_dsn(redis_url)
        self._pool: ArqRedis | None = None

    async def _redis(self) -> ArqRedis:
        if self._pool is None:
            self._pool = await create_pool(
                self._settings, default_queue_name=COMMUNICATIONS_QUEUE_NAME, retry=0
            )
        return self._pool

    async def enqueue_screening_generation(
        self, tenant_id: uuid.UUID, application_id: uuid.UUID
    ) -> None:
        await (await self._redis()).enqueue_job(
            GENERATE_SCREENING_TASK,
            str(tenant_id),
            str(application_id),
            _job_id=f"screening:{application_id}",
        )

    async def enqueue_screening_evaluation(
        self, tenant_id: uuid.UUID, screening_id: uuid.UUID
    ) -> None:
        await (await self._redis()).enqueue_job(
            EVALUATE_SCREENING_TASK,
            str(tenant_id),
            str(screening_id),
            _job_id=f"evaluate:{screening_id}",
        )

    async def aclose(self) -> None:
        if self._pool is not None:
            await self._pool.aclose()
            self._pool = None


_queue: ArqCommunicationsQueue | None = None


def get_communications_queue() -> CommunicationsQueue:
    global _queue
    if _queue is None:
        _queue = ArqCommunicationsQueue(get_settings().REDIS_URL)
    return _queue


async def close_communications_queue() -> None:
    if _queue is not None:
        await _queue.aclose()
