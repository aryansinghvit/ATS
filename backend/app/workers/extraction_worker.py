"""ARQ worker that feeds resumes to the M3 extractor (project.md §6.2 / §6.4).

Run from ``backend/``:  ``arq app.workers.extraction_worker.WorkerSettings``

* Concurrency is capped at ``M3_MAX_CONCURRENCY`` (1), matching the extractor's
  ``asyncio.Semaphore(1)`` GPU lock, so bursts of applicants queue here rather than on the M3 box.
* 503 / timeouts / connection errors back off 5s -> 15s -> 45s; after that the application stays
  ``pending_extraction`` and the sweeper re-queues it once the extractor reports healthy.
* The M3 response (202 + tracking id) is recorded; results arrive later via the HMAC webhook.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from arq import Retry, cron
from arq.connections import RedisSettings
from arq.worker import func
from sqlalchemy import select, text

from app.core.config import get_settings
from app.core.database import AsyncSessionLocal, engine, set_tenant_context
from app.models.candidate import ApplicationStatus, Candidate, JobApplication
from app.models.job import Job
from app.services.events import get_event_broker
from app.services.extraction_queue import EXTRACT_RESUME_TASK, EXTRACTION_QUEUE_NAME
from app.services.job_generator import build_m3_job_description
from app.services.m3_client import M3ExtractorClient, M3PermanentError, M3TransientError
from app.services.pipeline_service import PipelineService
from app.services.storage import EXTENSION_MIME_TYPES, get_storage_service

logger = logging.getLogger(__name__)

SWEEP_BATCH_SIZE = 100


async def startup(ctx: dict[str, Any]) -> None:
    ctx.setdefault("m3", M3ExtractorClient.from_settings(get_settings()))
    ctx.setdefault("storage", get_storage_service())
    ctx.setdefault("broker", get_event_broker())


async def shutdown(ctx: dict[str, Any]) -> None:
    await ctx["m3"].aclose()
    await engine.dispose()


async def _record_failure(
    ctx: dict[str, Any], session: Any, application: JobApplication, reason: str
) -> None:
    application.extraction_error = reason[:1000]
    if application.status is ApplicationStatus.PENDING_EXTRACTION:
        application.status = ApplicationStatus.APPLIED
    await session.commit()
    pipeline = PipelineService(session, ctx["broker"])
    await pipeline.publish(await pipeline.card(application.id), "card_updated")


async def extract_resume(ctx: dict[str, Any], tenant_id: str, application_id: str) -> str:
    settings = get_settings()
    tenant, app_id = uuid.UUID(tenant_id), uuid.UUID(application_id)
    async with AsyncSessionLocal() as session:
        await set_tenant_context(session, tenant)
        # Row lock: a second worker (or a sweeper duplicate) skips instead of double-dispatching.
        application = (
            await session.execute(
                select(JobApplication)
                .where(JobApplication.id == app_id)
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if application is None:
            return "missing_or_locked"
        if (
            application.extractor_tracking_id
            or application.extraction_error
            or application.match_breakdown_json
        ):
            return "already_dispatched"

        job = await session.get(Job, application.job_id)
        candidate = await session.get(Candidate, application.candidate_id)
        if job is None or candidate is None:  # pragma: no cover - guaranteed by foreign keys
            return "missing"
        try:
            resume = await ctx["storage"].backend.read_bytes(application.resume_s3_key)
        except FileNotFoundError:
            await _record_failure(ctx, session, application, "Resume file is missing from storage")
            return "failed"

        extension = application.resume_s3_key.rsplit(".", 1)[-1]
        try:
            tracking_id = await ctx["m3"].submit_extraction(
                file_name=f"resume.{extension}",
                file_bytes=resume,
                mime_type=EXTENSION_MIME_TYPES[extension],
                job_description=build_m3_job_description(job),
                webhook_url=settings.m3_webhook_url,
                callback_context={
                    "tenant_id": tenant_id,
                    "application_id": application_id,
                    "candidate_id": str(candidate.id),
                },
            )
        except M3TransientError as exc:
            delays = settings.m3_retry_delays
            attempt = ctx.get("job_try", 1)
            await session.rollback()
            if attempt <= len(delays):
                logger.warning("M3 busy for %s (attempt %d): %s", application_id, attempt, exc)
                raise Retry(defer=delays[attempt - 1]) from exc
            logger.error("M3 unavailable for %s after %d attempts", application_id, attempt)
            return "deferred_to_sweeper"
        except M3PermanentError as exc:
            await _record_failure(ctx, session, application, str(exc))
            return "failed"

        application.extractor_tracking_id = tracking_id
        await session.commit()
        return "dispatched"


async def sweep_stalled_extractions(ctx: dict[str, Any]) -> int:
    """Re-queue applications that never reached the extractor (Module 3 fallback strategy)."""
    if not await ctx["m3"].is_healthy():
        return 0
    settings = get_settings()
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text("SELECT tenant_id, application_id FROM app_list_stalled_extractions(:s, :n)"),
                {"s": settings.EXTRACTION_SWEEP_STALE_SECONDS, "n": SWEEP_BATCH_SIZE},
            )
        ).all()
    stamp = int(time.time())
    for row in rows:
        await ctx["redis"].enqueue_job(
            EXTRACT_RESUME_TASK,
            str(row.tenant_id),
            str(row.application_id),
            _job_id=f"extract:{row.application_id}:sweep:{stamp}",
        )
    return len(rows)


class WorkerSettings:
    functions = [
        func(
            extract_resume,
            name=EXTRACT_RESUME_TASK,
            max_tries=len(get_settings().m3_retry_delays) + 1,
        )
    ]
    cron_jobs = [cron(sweep_stalled_extractions, minute=set(range(0, 60, 5)))]
    on_startup = startup
    on_shutdown = shutdown
    queue_name = EXTRACTION_QUEUE_NAME
    redis_settings = RedisSettings.from_dsn(get_settings().REDIS_URL)
    max_jobs = get_settings().M3_MAX_CONCURRENCY
    job_timeout = 120
    keep_result = 60
