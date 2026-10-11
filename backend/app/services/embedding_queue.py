"""Producer side of the ARQ embedding queue (consumer: app.workers.embedding_worker).

Kept separate from the extraction queue so embedding work never waits behind the M3 rate limit.
"""

from __future__ import annotations

import uuid
from typing import Protocol

from arq import ArqRedis, create_pool
from arq.connections import RedisSettings

from app.core.config import get_settings

EMBEDDING_QUEUE_NAME = "ats:embedding"
EMBED_CANDIDATE_TASK = "embed_candidate"
RESURFACE_JOB_TASK = "resurface_for_job"


class EmbeddingQueue(Protocol):
    async def enqueue_embedding(self, tenant_id: uuid.UUID, candidate_id: uuid.UUID) -> None: ...

    async def enqueue_resurfacing(self, tenant_id: uuid.UUID, job_id: uuid.UUID) -> None: ...


class ArqEmbeddingQueue:
    def __init__(self, redis_url: str) -> None:
        self._settings = RedisSettings.from_dsn(redis_url)
        self._pool: ArqRedis | None = None

    async def _redis(self) -> ArqRedis:
        if self._pool is None:
            self._pool = await create_pool(
                self._settings, default_queue_name=EMBEDDING_QUEUE_NAME, retry=0
            )
        return self._pool

    async def enqueue_embedding(self, tenant_id: uuid.UUID, candidate_id: uuid.UUID) -> None:
        # Deterministic id: a candidate already waiting in the queue is not queued twice.
        await (await self._redis()).enqueue_job(
            EMBED_CANDIDATE_TASK, str(tenant_id), str(candidate_id), _job_id=f"embed:{candidate_id}"
        )

    async def enqueue_resurfacing(self, tenant_id: uuid.UUID, job_id: uuid.UUID) -> None:
        await (await self._redis()).enqueue_job(
            RESURFACE_JOB_TASK, str(tenant_id), str(job_id), _job_id=f"resurface:{job_id}"
        )

    async def aclose(self) -> None:
        if self._pool is not None:
            await self._pool.aclose()
            self._pool = None


_queue: ArqEmbeddingQueue | None = None


def get_embedding_queue() -> EmbeddingQueue:
    global _queue
    if _queue is None:
        _queue = ArqEmbeddingQueue(get_settings().REDIS_URL)
    return _queue


async def close_embedding_queue() -> None:
    if _queue is not None:
        await _queue.aclose()
