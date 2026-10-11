"""ARQ worker for candidate communications and AI pre-screening.

Run from ``backend/``:  ``arq app.workers.communications_worker.WorkerSettings``

* ``dispatch_due_emails`` (cron, every minute): delivers queued outbox emails whose time has come
  (immediate notifications within a minute; polite rejections after their delay).
* ``generate_screening``: AI questions for candidates entering Pre-Screen (template fallback when
  the local LLM is unavailable), then the candidate is emailed a single-use portal link.
* ``evaluate_screening``: transcribe audio answers (faster-whisper) and attach the advisory
  rubric scorecard (local LLM, heuristic fallback). Never changes the candidate's stage/status.
"""

from __future__ import annotations

import uuid
from typing import Any

from arq import cron
from arq.connections import RedisSettings
from arq.worker import func

from app.core.config import get_settings
from app.core.database import AsyncSessionLocal, engine, set_tenant_context
from app.services.communications import dispatch_due_emails, get_email_sender
from app.services.communications_queue import (
    COMMUNICATIONS_QUEUE_NAME,
    EVALUATE_SCREENING_TASK,
    GENERATE_SCREENING_TASK,
)
from app.services.events import get_event_broker
from app.services.llm_client import get_llm_client
from app.services.screening_evaluator import ScreeningEvaluator, get_transcriber
from app.services.screening_generator import ScreeningService
from app.services.storage import get_storage_service


async def startup(ctx: dict[str, Any]) -> None:
    ctx.setdefault("sender", get_email_sender())
    ctx.setdefault("llm", get_llm_client())
    ctx.setdefault("storage", get_storage_service())
    ctx.setdefault("transcriber", get_transcriber())
    ctx.setdefault("broker", get_event_broker())


async def shutdown(ctx: dict[str, Any]) -> None:
    await engine.dispose()


async def dispatch_emails(ctx: dict[str, Any]) -> dict[str, int]:
    return await dispatch_due_emails(ctx["sender"])


async def generate_screening(ctx: dict[str, Any], tenant_id: str, application_id: str) -> str:
    """Stage-triggered: generate questions, then invite the candidate to the portal."""
    async with AsyncSessionLocal() as session:
        await set_tenant_context(session, uuid.UUID(tenant_id))
        service = ScreeningService(session, ctx["llm"], ctx.get("storage"))
        existing = await service.latest(uuid.UUID(application_id))
        if existing is not None and existing.status.value in ("opened", "completed"):
            return "already_started"
        screening = await service.generate(uuid.UUID(application_id))
        await service.invite(uuid.UUID(application_id))
        return screening.source


async def evaluate_screening(ctx: dict[str, Any], tenant_id: str, screening_id: str) -> float:
    async with AsyncSessionLocal() as session:
        await set_tenant_context(session, uuid.UUID(tenant_id))
        evaluation = await ScreeningEvaluator(
            session, ctx["storage"], ctx["llm"], ctx["transcriber"], ctx.get("broker")
        ).evaluate(uuid.UUID(screening_id))
        return evaluation.overall_score


class WorkerSettings:
    functions = [
        func(generate_screening, name=GENERATE_SCREENING_TASK, max_tries=2),
        func(evaluate_screening, name=EVALUATE_SCREENING_TASK, max_tries=3),
    ]
    cron_jobs = [cron(dispatch_emails, second=0)]
    on_startup = startup
    on_shutdown = shutdown
    queue_name = COMMUNICATIONS_QUEUE_NAME
    redis_settings = RedisSettings.from_dsn(get_settings().REDIS_URL)
    max_jobs = 4
    job_timeout = 300
    keep_result = 0
