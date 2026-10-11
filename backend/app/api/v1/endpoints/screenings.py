"""AI pre-screening (project.md Module 6): recruiter controls + the candidate portal API."""

from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import TenantDB, require_role
from app.core.config import get_settings
from app.core.database import get_session
from app.core.exceptions import ServiceError
from app.models.user import User, UserRole
from app.schemas.screening import (
    GenerateScreeningRequest,
    PortalAnswer,
    PortalSubmitResponse,
    PortalView,
    SaveTextAnswer,
    ScreeningRead,
)
from app.services.communications_queue import CommunicationsQueue, get_communications_queue
from app.services.llm_client import LLMClient, get_llm_client
from app.services.screening_generator import ScreeningService
from app.services.screening_portal import ScreeningPortalService
from app.services.storage import StorageService, get_storage_service

logger = logging.getLogger(__name__)
router = APIRouter(tags=["screenings"])
public_router = APIRouter(prefix="/public/screenings", tags=["public", "screenings"])

# Module 1 RBAC: "Trigger Automated Pre-Screens" — Admin & Recruiter only.
PreScreener = Annotated[User, Depends(require_role([UserRole.ADMIN, UserRole.RECRUITER]))]
Reviewer = Annotated[
    User, Depends(require_role([UserRole.ADMIN, UserRole.RECRUITER, UserRole.HIRING_MANAGER]))
]
Storage = Annotated[StorageService, Depends(get_storage_service)]
Session = Annotated[AsyncSession, Depends(get_session)]
Queue = Annotated[CommunicationsQueue, Depends(get_communications_queue)]


class PayloadTooLargeError(ServiceError):
    status_code = status.HTTP_413_CONTENT_TOO_LARGE


@router.post("/applications/{application_id}/screening-questions", response_model=ScreeningRead)
async def generate_screening_questions(
    application_id: uuid.UUID,
    payload: GenerateScreeningRequest,
    user: PreScreener,
    db: TenantDB,
    storage: Storage,
    llm: Annotated[LLMClient, Depends(get_llm_client)],
) -> ScreeningRead:
    """3–5 questions targeting the candidate's skill gaps (local LLM, template fallback)."""
    return await ScreeningService(db, llm, storage).generate(
        application_id, count=payload.count, created_by=user.id
    )


@router.post("/applications/{application_id}/screening/invite", response_model=ScreeningRead)
async def invite_to_screening(
    application_id: uuid.UUID, user: PreScreener, db: TenantDB, storage: Storage
) -> ScreeningRead:
    """Email the candidate a fresh single-use portal link (valid 72 h; older links stop working)."""
    return await ScreeningService(db, None, storage).invite(application_id)


@router.get("/applications/{application_id}/screening", response_model=ScreeningRead | None)
async def get_latest_screening(
    application_id: uuid.UUID, user: Reviewer, db: TenantDB, storage: Storage
) -> ScreeningRead | None:
    """Questions, the candidate's answers (with transcripts/audio) and the advisory scorecard."""
    return await ScreeningService(db, None, storage).latest(application_id)


# ------------------------------------------------------------------- candidate portal
@public_router.get("/{token}", response_model=PortalView)
async def open_screening(token: str, session: Session, storage: Storage) -> PortalView:
    return await ScreeningPortalService(session, storage).view(token)


@public_router.put("/{token}/answers/{index}", response_model=PortalAnswer)
async def save_text_answer(
    token: str, index: int, payload: SaveTextAnswer, session: Session, storage: Storage
) -> PortalAnswer:
    """Auto-save a written answer (switching from audio discards the recording)."""
    return await ScreeningPortalService(session, storage).save_text(token, index, payload.text)


@public_router.put("/{token}/answers/{index}/audio", response_model=PortalAnswer)
async def save_audio_answer(
    token: str, index: int, request: Request, session: Session, storage: Storage
) -> PortalAnswer:
    """Upload a browser recording as the raw request body (``Content-Type: audio/webm`` etc.)."""
    limit = get_settings().SCREENING_MAX_AUDIO_BYTES
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise PayloadTooLargeError("Recording is too large")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise PayloadTooLargeError("Recording is too large")
        chunks.append(chunk)
    return await ScreeningPortalService(session, storage).save_audio(
        token, index, b"".join(chunks), request.headers.get("content-type", "")
    )


@public_router.post(
    "/{token}/submit", response_model=PortalSubmitResponse, status_code=status.HTTP_202_ACCEPTED
)
async def submit_screening(
    token: str, session: Session, storage: Storage, queue: Queue
) -> PortalSubmitResponse:
    """Final submission (single use). Answers are transcribed and scored in the background."""
    screening = await ScreeningPortalService(session, storage).submit(token)
    try:
        await queue.enqueue_screening_evaluation(screening.tenant_id, screening.id)
    except Exception:  # noqa: BLE001 - answers are saved; evaluation can be re-queued
        logger.exception("Could not queue evaluation for screening %s", screening.id)
    return PortalSubmitResponse(status=screening.status, submitted_at=screening.submitted_at)
