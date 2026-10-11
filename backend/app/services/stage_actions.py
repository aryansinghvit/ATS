"""Automated actions run when a card enters a pipeline stage ("on_enter" in
``pipeline_stages.automated_actions_json``). Handlers stage their work in the move's transaction;
anything slow (LLM generation, email delivery) is handed to background workers.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.candidate import JobApplication
from app.models.job import PipelineStage
from app.services.communications import CommunicationService
from app.services.communications_queue import CommunicationsQueue

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class StageActionContext:
    """Dependencies handlers need; injected by the endpoint so tests can substitute them."""

    communications_queue: CommunicationsQueue | None = None


StageActionHandler = Callable[
    [AsyncSession, JobApplication, PipelineStage, StageActionContext], Awaitable[str]
]


def _email_action(template_key: str) -> StageActionHandler:
    async def handler(
        session: AsyncSession,
        application: JobApplication,
        stage: PipelineStage,
        ctx: StageActionContext,
    ) -> str:
        email = await CommunicationService(session).queue(
            application, template_key, trigger=f"stage:{stage.stage_type}"
        )
        return "queued" if email else "skipped"

    return handler


async def _polite_rejection(
    session: AsyncSession,
    application: JobApplication,
    stage: PipelineStage,
    ctx: StageActionContext,
) -> str:
    email = await CommunicationService(session).queue_polite_rejection(application)
    return "scheduled" if email else "skipped"


async def _ai_pre_screen(
    session: AsyncSession,
    application: JobApplication,
    stage: PipelineStage,
    ctx: StageActionContext,
) -> str:
    if ctx.communications_queue is None:
        return "skipped"
    try:
        await ctx.communications_queue.enqueue_screening_generation(
            application.tenant_id, application.id
        )
    except Exception:  # noqa: BLE001 - a queue outage must not block the stage move
        logger.exception("Could not queue screening generation for %s", application.id)
        return "failed"
    return "queued"


STAGE_ACTION_HANDLERS: dict[str, StageActionHandler] = {
    "ai_pre_screen": _ai_pre_screen,
    "interview_invitation_email": _email_action("interview_invitation"),
    "offer_email": _email_action("offer_extended"),
    "polite_rejection_email": _polite_rejection,
}
