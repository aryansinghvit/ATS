"""Semantic talent pool: natural-language sourcing across every past applicant (Module 5)."""

from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select

from app.api.deps import TenantDB, require_role
from app.models.candidate import Candidate
from app.models.user import User, UserRole
from app.schemas.talent_pool import (
    InviteToJobRequest,
    InviteToJobResponse,
    ReindexResponse,
    ResurfacingResult,
    TalentPoolStats,
    TalentSearchRequest,
    TalentSearchResponse,
    TalentSuggestion,
)
from app.services.embedding_queue import EmbeddingQueue, get_embedding_queue
from app.services.embedding_service import Embedder, get_embedder
from app.services.events import EventBroker, get_event_broker
from app.services.extraction_queue import ExtractionQueue, get_extraction_queue
from app.services.pipeline_service import PipelineService
from app.services.resurfacing_service import ResurfacingService
from app.services.talent_pool_service import TalentPoolService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/talent-pool", tags=["talent-pool"])

# Sourcing exposes past applicants' PII, so blind interviewers are excluded.
Sourcer = Annotated[
    User, Depends(require_role([UserRole.ADMIN, UserRole.RECRUITER, UserRole.HIRING_MANAGER]))
]
Admin = Annotated[User, Depends(require_role([UserRole.ADMIN]))]
EmbedderDep = Annotated[Embedder, Depends(get_embedder)]


@router.post("/search", response_model=TalentSearchResponse)
async def search_talent_pool(
    request: TalentSearchRequest, user: Sourcer, db: TenantDB, embedder: EmbedderDep
) -> TalentSearchResponse:
    """Hybrid semantic + keyword search over the tenant's past applicants."""
    return await TalentPoolService(db, embedder).search(request, actor=user)


@router.get("/stats", response_model=TalentPoolStats)
async def talent_pool_stats(user: Sourcer, db: TenantDB, embedder: EmbedderDep) -> TalentPoolStats:
    return await TalentPoolService(db, embedder).stats()


@router.post("/reindex", response_model=ReindexResponse)
async def reindex_talent_pool(
    admin: Admin,
    db: TenantDB,
    queue: Annotated[EmbeddingQueue, Depends(get_embedding_queue)],
) -> ReindexResponse:
    """Queue every candidate of the workspace for (re-)embedding; unchanged profiles are skipped."""
    candidate_ids = list((await db.execute(select(Candidate.id))).scalars())
    for candidate_id in candidate_ids:
        await queue.enqueue_embedding(admin.tenant_id, candidate_id)
    return ReindexResponse(queued=len(candidate_ids))


# ---------------------------------------------------------------- resurfacing & invites
Recruiter = Annotated[User, Depends(require_role([UserRole.ADMIN, UserRole.RECRUITER]))]


@router.get("/jobs/{job_id}/suggestions", response_model=list[TalentSuggestion])
async def job_suggestions(job_id: uuid.UUID, user: Sourcer, db: TenantDB) -> list[TalentSuggestion]:
    """Past applicants resurfaced for this job (dismissed ones are hidden)."""
    return await ResurfacingService(db).suggestions(job_id)


@router.post("/jobs/{job_id}/resurface", response_model=ResurfacingResult)
async def resurface_now(
    job_id: uuid.UUID, user: Recruiter, db: TenantDB, embedder: EmbedderDep
) -> ResurfacingResult:
    """Re-run the talent-pool scan for a published job (also runs automatically on publish)."""
    return await ResurfacingService(db, embedder).scan(job_id)


@router.post("/jobs/{job_id}/suggestions/{candidate_id}/dismiss", status_code=204)
async def dismiss_suggestion(
    job_id: uuid.UUID, candidate_id: uuid.UUID, user: Recruiter, db: TenantDB
) -> None:
    await ResurfacingService(db).dismiss(job_id, candidate_id)


@router.post("/candidates/{candidate_id}/invite-to-job", response_model=InviteToJobResponse)
async def invite_to_job(
    candidate_id: uuid.UUID,
    payload: InviteToJobRequest,
    user: Recruiter,
    db: TenantDB,
    extraction_queue: Annotated[ExtractionQueue, Depends(get_extraction_queue)],
    broker: Annotated[EventBroker, Depends(get_event_broker)],
) -> InviteToJobResponse:
    """1-click: add a past applicant to the job's Applied column and send the invitation email."""
    result = await ResurfacingService(db).invite(candidate_id, payload.job_id, actor=user)
    application = result.application
    try:  # score the existing resume against this job's taxonomy
        await extraction_queue.enqueue_extraction(application.tenant_id, application.id)
    except Exception:  # noqa: BLE001 - the stalled-extraction sweeper retries later
        logger.exception("Could not enqueue extraction for invited application %s", application.id)
    pipeline = PipelineService(db, broker)
    await pipeline.publish(await pipeline.card(application.id), "card_updated")
    return InviteToJobResponse(
        application_id=application.id,
        job_id=application.job_id,
        status=application.status,
        email_queued=result.email_queued,
    )
