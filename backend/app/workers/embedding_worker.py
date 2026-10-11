"""ARQ worker that embeds candidate profiles into the semantic talent pool.

Run from ``backend/``:  ``arq app.workers.embedding_worker.WorkerSettings``

FastEmbed runs on CPU inside this process (the ONNX model loads once, on the first job).
"""

from __future__ import annotations

import uuid
from typing import Any

from arq.connections import RedisSettings
from arq.worker import func

from app.core.config import get_settings
from app.core.database import AsyncSessionLocal, engine, set_tenant_context
from app.services.embedding_queue import (
    EMBED_CANDIDATE_TASK,
    EMBEDDING_QUEUE_NAME,
    RESURFACE_JOB_TASK,
)
from app.services.embedding_service import EmbeddingService, get_embedder
from app.services.resurfacing_service import ResurfacingService


async def startup(ctx: dict[str, Any]) -> None:
    ctx.setdefault("embedder", get_embedder())


async def shutdown(ctx: dict[str, Any]) -> None:
    await engine.dispose()


async def embed_candidate(ctx: dict[str, Any], tenant_id: str, candidate_id: str) -> str:
    async with AsyncSessionLocal() as session:
        await set_tenant_context(session, uuid.UUID(tenant_id))
        return await EmbeddingService(session, ctx["embedder"]).index_candidate(
            uuid.UUID(candidate_id)
        )


async def resurface_for_job(ctx: dict[str, Any], tenant_id: str, job_id: str) -> dict[str, Any]:
    """Job published -> search the talent pool and alert the recruiter about strong matches."""
    async with AsyncSessionLocal() as session:
        await set_tenant_context(session, uuid.UUID(tenant_id))
        result = await ResurfacingService(session, ctx["embedder"]).scan(uuid.UUID(job_id))
        return result.model_dump(mode="json")


class WorkerSettings:
    functions = [
        func(embed_candidate, name=EMBED_CANDIDATE_TASK, max_tries=3),
        func(resurface_for_job, name=RESURFACE_JOB_TASK, max_tries=3),
    ]
    on_startup = startup
    on_shutdown = shutdown
    queue_name = EMBEDDING_QUEUE_NAME
    redis_settings = RedisSettings.from_dsn(get_settings().REDIS_URL)
    max_jobs = 2
    job_timeout = 120
    keep_result = 0  # lets the same candidate be re-queued as soon as its last job finishes
