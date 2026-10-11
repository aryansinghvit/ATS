"""Recruiter Kanban pipeline (project.md Module 4).

RBAC: Admin & Recruiter move freely; Hiring Managers only into Interview/Offer/Rejected;
Blind Interviewers have no pipeline access (scores and PII are redacted for them).
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse

from app.api.deps import TenantDB, require_role
from app.core.config import get_settings
from app.models.user import User, UserRole
from app.schemas.pipeline import (
    MoveStageRequest,
    PipelineBoard,
    PipelineCard,
    PipelineFilters,
    PipelineSort,
)
from app.services.communications_queue import CommunicationsQueue, get_communications_queue
from app.services.events import EventBroker, get_event_broker, pipeline_channel
from app.services.pipeline_service import PipelineService
from app.services.stage_actions import StageActionContext

router = APIRouter(tags=["pipeline"])

PipelineUser = Annotated[
    User,
    Depends(require_role([UserRole.ADMIN, UserRole.RECRUITER, UserRole.HIRING_MANAGER])),
]
Broker = Annotated[EventBroker, Depends(get_event_broker)]


@router.get("/jobs/{job_id}/pipeline", response_model=PipelineBoard)
async def get_pipeline(
    job_id: uuid.UUID,
    user: PipelineUser,
    db: TenantDB,
    sort: PipelineSort = "default",
    min_score: Annotated[float | None, Query(ge=0, le=100)] = None,
    missing_skill: Annotated[str | None, Query(max_length=100)] = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
) -> PipelineBoard:
    """All stages with their cards.

    Default order: Applied by deterministic score DESC NULLS LAST, later stages FIFO.
    ``sort``: score | applied_desc | applied_asc | missing_skills. Filters: ``min_score``,
    ``missing_skill`` (candidates lacking that required skill), ``q`` (name/email).
    """
    filters = PipelineFilters(sort=sort, min_score=min_score, missing_skill=missing_skill, q=q)
    return await PipelineService(db).board(job_id, filters)


@router.patch("/applications/{application_id}/move-stage", response_model=PipelineCard)
async def move_stage(
    application_id: uuid.UUID,
    payload: MoveStageRequest,
    request: Request,
    user: PipelineUser,
    db: TenantDB,
    broker: Broker,
    communications_queue: Annotated[CommunicationsQueue, Depends(get_communications_queue)],
) -> PipelineCard:
    """Move a card; ``expected_version`` must match (optimistic concurrency) or 409 is returned.

    Entering a stage runs its automated actions (emails, AI pre-screen) atomically with the move.
    """
    actions = StageActionContext(communications_queue=communications_queue)
    return await PipelineService(db, broker, actions).move(
        application_id,
        payload,
        actor=user,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )


@router.get("/jobs/{job_id}/pipeline/stream")
async def stream_pipeline(
    job_id: uuid.UUID, request: Request, user: PipelineUser, db: TenantDB, broker: Broker
) -> StreamingResponse:
    """SSE: ``card_moved`` and ``card_updated`` (extraction results) events for one job board.

    Authenticate with the normal ``Authorization: Bearer`` header (use a fetch-based SSE client;
    tokens are never accepted in the URL).
    """
    await PipelineService(db).get_job(job_id)
    channel = pipeline_channel(user.tenant_id, job_id)
    await db.close()  # don't hold a pooled DB connection for the lifetime of the stream
    settings = get_settings()

    async def events() -> AsyncIterator[str]:
        async with broker.subscribe(channel) as subscription:
            yield "retry: 3000\n\n: connected\n\n"
            deadline = time.monotonic() + settings.PIPELINE_STREAM_MAX_SECONDS
            while (remaining := deadline - time.monotonic()) > 0:
                event = await subscription.get(
                    timeout=min(settings.PIPELINE_STREAM_KEEPALIVE_SECONDS, remaining)
                )
                if event is None:
                    if await request.is_disconnected():
                        return
                    yield ": keep-alive\n\n"
                    continue
                data = json.dumps(event, separators=(",", ":"))
                yield f"event: {event['type']}\ndata: {data}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
