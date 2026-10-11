"""Phase 7 administration & reporting: analytics, GDPR erasure, stage editor, team, interviews,
offer letters and the Cal.com booking webhook."""

from __future__ import annotations

import json
import uuid
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import TenantDB, bind_tenant_by_subdomain, require_role
from app.core.database import get_session
from app.core.exceptions import UnprocessableError
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.schemas.admin import (
    CandidateLookup,
    ErasureReport,
    InterviewCreate,
    InterviewRead,
    InterviewUpdate,
    OfferLetterRead,
    OfferLetterRequest,
    StageActionInfo,
    StageCreate,
    StageRead,
    StageReorder,
    StageUpdate,
    TeamMemberUpdate,
)
from app.schemas.analytics import PipelineAnalytics
from app.schemas.user import UserRead
from app.services.admin_services import (
    ComplianceService,
    InterviewService,
    OfferService,
    TeamService,
    calcom_secret,
    verify_calcom_signature,
)
from app.services.analytics_service import AnalyticsService
from app.services.communications import CommunicationService
from app.services.stage_service import StageService
from app.services.storage import StorageService, get_storage_service

router = APIRouter()

Admin = Annotated[User, Depends(require_role([UserRole.ADMIN]))]
Recruiter = Annotated[User, Depends(require_role([UserRole.ADMIN, UserRole.RECRUITER]))]
Reviewer = Annotated[
    User, Depends(require_role([UserRole.ADMIN, UserRole.RECRUITER, UserRole.HIRING_MANAGER]))
]
Storage = Annotated[StorageService, Depends(get_storage_service)]


# ------------------------------------------------------------------------ analytics
@router.get("/analytics/pipeline", response_model=PipelineAnalytics, tags=["analytics"])
async def pipeline_analytics(
    user: Reviewer,
    db: TenantDB,
    job_id: uuid.UUID | None = None,
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
) -> PipelineAnalytics:
    """Stage conversion & drop-off, time-in-stage, time-to-hire and per-job metrics."""
    if date_from and date_to and date_from > date_to:
        raise UnprocessableError("'from' must be on or before 'to'")
    return await AnalyticsService(db).pipeline(job_id=job_id, date_from=date_from, date_to=date_to)


# -------------------------------------------------------------- GDPR Article 17
@router.get("/compliance/candidates", response_model=list[CandidateLookup], tags=["compliance"])
async def find_candidate(
    admin: Admin, db: TenantDB, storage: Storage, email: Annotated[str, Query(min_length=3)]
) -> list[CandidateLookup]:
    """Locate a data subject by email (e.g. for an erasure request)."""
    return await ComplianceService(db, storage).find(email)


@router.delete(
    "/compliance/candidates/{candidate_id}/forget",
    response_model=ErasureReport,
    tags=["compliance"],
)
async def forget_candidate(
    candidate_id: uuid.UUID, admin: Admin, db: TenantDB, storage: Storage
) -> ErasureReport:
    """Right to be forgotten: delete the candidate, every derived record and stored file."""
    return await ComplianceService(db, storage).forget(candidate_id, actor=admin)


# --------------------------------------------------------------------- stage editor
@router.get("/stage-actions", response_model=list[StageActionInfo], tags=["pipeline"])
async def stage_actions(user: Reviewer) -> list[StageActionInfo]:
    return StageService.actions()


@router.get("/jobs/{job_id}/stages", response_model=list[StageRead], tags=["pipeline"])
async def list_stages(job_id: uuid.UUID, user: Reviewer, db: TenantDB) -> list[StageRead]:
    return await StageService(db).list(job_id)


@router.post(
    "/jobs/{job_id}/stages",
    response_model=list[StageRead],
    status_code=status.HTTP_201_CREATED,
    tags=["pipeline"],
)
async def create_stage(
    job_id: uuid.UUID, payload: StageCreate, user: Recruiter, db: TenantDB
) -> list[StageRead]:
    return await StageService(db).create(job_id, payload, actor=user)


@router.put("/jobs/{job_id}/stages/order", response_model=list[StageRead], tags=["pipeline"])
async def reorder_stages(
    job_id: uuid.UUID, payload: StageReorder, user: Recruiter, db: TenantDB
) -> list[StageRead]:
    return await StageService(db).reorder(job_id, payload, actor=user)


@router.patch("/jobs/{job_id}/stages/{stage_id}", response_model=list[StageRead], tags=["pipeline"])
async def update_stage(
    job_id: uuid.UUID, stage_id: uuid.UUID, payload: StageUpdate, user: Recruiter, db: TenantDB
) -> list[StageRead]:
    return await StageService(db).update(job_id, stage_id, payload, actor=user)


@router.delete(
    "/jobs/{job_id}/stages/{stage_id}", response_model=list[StageRead], tags=["pipeline"]
)
async def delete_stage(
    job_id: uuid.UUID, stage_id: uuid.UUID, user: Recruiter, db: TenantDB
) -> list[StageRead]:
    return await StageService(db).delete(job_id, stage_id, actor=user)


# ------------------------------------------------------------------------------ team
@router.patch("/users/{user_id}", response_model=UserRead, tags=["users"])
async def update_team_member(
    user_id: uuid.UUID, payload: TeamMemberUpdate, admin: Admin, db: TenantDB
) -> UserRead:
    """Activate/deactivate or set department. Roles are managed in Clerk (the identity provider)."""
    return UserRead.model_validate(await TeamService(db).update(user_id, payload, actor=admin))


# ------------------------------------------------------------------------ interviews
@router.get(
    "/applications/{application_id}/interviews",
    response_model=list[InterviewRead],
    tags=["interviews"],
)
async def list_interviews(
    application_id: uuid.UUID, user: Reviewer, db: TenantDB
) -> list[InterviewRead]:
    return await InterviewService(db).list(application_id)


@router.post(
    "/applications/{application_id}/interviews",
    response_model=InterviewRead,
    status_code=status.HTTP_201_CREATED,
    tags=["interviews"],
)
async def schedule_interview(
    application_id: uuid.UUID, payload: InterviewCreate, user: Reviewer, db: TenantDB
) -> InterviewRead:
    return await InterviewService(db).create(application_id, payload, actor=user)


@router.patch("/interviews/{interview_id}", response_model=InterviewRead, tags=["interviews"])
async def update_interview(
    interview_id: uuid.UUID, payload: InterviewUpdate, user: Reviewer, db: TenantDB
) -> InterviewRead:
    return await InterviewService(db).set_status(interview_id, payload.status, actor=user)


class CalcomSecret(BaseModel):
    secret: str | None = Field(default=None, min_length=16, max_length=200)


@router.put("/communications/calcom-webhook", tags=["communications"])
async def set_calcom_webhook_secret(payload: CalcomSecret, user: Recruiter, db: TenantDB) -> dict:
    """Store the signing secret configured on the Cal.com webhook (write-only)."""
    configured = await CommunicationService(db).set_calcom_secret(user.tenant_id, payload.secret)
    tenant = await db.get(Tenant, user.tenant_id)
    return {
        "configured": configured,
        "webhook_path": f"/api/v1/webhooks/scheduling/calcom/{tenant.subdomain if tenant else ''}",
    }


@router.post("/webhooks/scheduling/calcom/{tenant_subdomain}", tags=["webhooks"])
async def calcom_booking_webhook(
    tenant_subdomain: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, str]:
    """Cal.com booking created/rescheduled/cancelled -> interview record (HMAC-SHA256 signed)."""
    tenant_id = await bind_tenant_by_subdomain(request, session, tenant_subdomain)
    body = await request.body()
    tenant = await session.get(Tenant, tenant_id)
    verify_calcom_signature(
        body, request.headers.get("x-cal-signature-256"), calcom_secret(tenant) if tenant else None
    )
    try:
        event: dict[str, Any] = json.loads(body)
    except ValueError:
        raise UnprocessableError("Invalid JSON payload") from None
    return {"status": await InterviewService(session).ingest_calcom(tenant_id, event)}


# ---------------------------------------------------------------------- offer letter
@router.post(
    "/applications/{application_id}/offer-letter", response_model=OfferLetterRead, tags=["offers"]
)
async def generate_offer_letter(
    application_id: uuid.UUID,
    payload: OfferLetterRequest,
    user: Recruiter,
    db: TenantDB,
    storage: Storage,
) -> OfferLetterRead:
    """Generate (or regenerate) the offer letter PDF from the given terms."""
    return await OfferService(db, storage).generate(application_id, payload, actor=user)


@router.get(
    "/applications/{application_id}/offer-letter", response_model=OfferLetterRead, tags=["offers"]
)
async def get_offer_letter(
    application_id: uuid.UUID, user: Reviewer, db: TenantDB, storage: Storage
) -> OfferLetterRead:
    return await OfferService(db, storage).read(application_id)
