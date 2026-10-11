"""Producer side of the ARQ extraction queue (consumer: app.workers.extraction_worker)."""

from __future__ import annotations

import uuid
from typing import Protocol

from arq import ArqRedis, create_pool
from arq.connections import RedisSettings

from app.core.config import get_settings

EXTRACTION_QUEUE_NAME = "ats:extraction"
EXTRACT_RESUME_TASK = "extract_resume"


class ExtractionQueue(Protocol):
    async def enqueue_extraction(self, tenant_id: uuid.UUID, application_id: uuid.UUID) -> None: ...


class ArqExtractionQueue:
    def __init__(self, redis_url: str) -> None:
        self._settings = RedisSettings.from_dsn(redis_url)
        self._pool: ArqRedis | None = None

    async def _redis(self) -> ArqRedis:
        if self._pool is None:
            self._pool = await create_pool(
                self._settings, default_queue_name=EXTRACTION_QUEUE_NAME, retry=0
            )
        return self._pool

    async def enqueue_extraction(self, tenant_id: uuid.UUID, application_id: uuid.UUID) -> None:
        pool = await self._redis()
        # A deterministic job id makes duplicate submissions of the same application a no-op.
        await pool.enqueue_job(
            EXTRACT_RESUME_TASK,
            str(tenant_id),
            str(application_id),
            _job_id=f"extract:{application_id}",
        )

    async def aclose(self) -> None:
        if self._pool is not None:
            await self._pool.aclose()
            self._pool = None


_queue: ArqExtractionQueue | None = None


def get_extraction_queue() -> ExtractionQueue:
    global _queue
    if _queue is None:
        _queue = ArqExtractionQueue(get_settings().REDIS_URL)
    return _queue


async def close_extraction_queue() -> None:
    if _queue is not None:
        await _queue.aclose()
