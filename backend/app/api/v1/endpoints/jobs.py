"""Job requisitions (recruiter workspace) and the public careers view (project.md Module 2).

RBAC (Module 1 matrix): Admin & Recruiter create/publish; Hiring Manager drafts only;
Interviewers may read requisitions but never write them.
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi import status as http_status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, PathTenantId, TenantDB, require_role
from app.core.database import get_session
from app.models.job import JobStatus
from app.models.user import User, UserRole
from app.schemas.job import (
    JobCreate,
    JobDescriptionRequest,
    JobDraft,
    JobListItem,
    JobRead,
    JobUpdate,
    PublicJobBoard,
    PublicJobDetail,
    PublicJobSummary,
)
from app.schemas.tenant import TenantBranding
from app.services.embedding_queue import EmbeddingQueue, get_embedding_queue
from app.services.job_service import JobService
from app.services.llm_client import LLMClient, get_llm_client

router = APIRouter(prefix="/jobs", tags=["jobs"])
public_router = APIRouter(prefix="/public/jobs", tags=["public"])

JobAuthor = Annotated[
    User,
    Depends(require_role([UserRole.ADMIN, UserRole.RECRUITER, UserRole.HIRING_MANAGER])),
]
LLM = Annotated[LLMClient, Depends(get_llm_client)]
EmbeddingQueueDep = Annotated[EmbeddingQueue, Depends(get_embedding_queue)]
logger = logging.getLogger(__name__)


async def _queue_resurfacing(
    queue: EmbeddingQueue, tenant_id: uuid.UUID, job_id: uuid.UUID
) -> None:
    try:
        await queue.enqueue_resurfacing(tenant_id, job_id)
    except Exception:  # noqa: BLE001 - publishing must not fail because sourcing alerts are down
        logger.exception("Could not queue talent resurfacing for job %s", job_id)


@router.post("/generate-description", response_model=JobDraft)
async def generate_job_description(
    request: JobDescriptionRequest, author: JobAuthor, db: TenantDB, llm: LLM
) -> JobDraft:
    """Turn rough recruiter notes into a structured, inclusive draft with an M3-ready taxonomy."""
    return await JobService(db, llm=llm).generate_description(request, tenant_id=author.tenant_id)


@router.post("", response_model=JobRead, status_code=http_status.HTTP_201_CREATED)
async def create_job(
    payload: JobCreate, author: JobAuthor, db: TenantDB, llm: LLM, queue: EmbeddingQueueDep
) -> JobRead:
    job = await JobService(db, llm=llm).create(payload, actor=author)
    if job.status is JobStatus.PUBLISHED:
        await _queue_resurfacing(queue, job.tenant_id, job.id)
    return JobRead.model_validate(job)


@router.get("", response_model=list[JobListItem])
async def list_jobs(
    current_user: CurrentUser, db: TenantDB, status: JobStatus | None = None
) -> list[JobListItem]:
    return [JobListItem.model_validate(job) for job in await JobService(db).list(status=status)]


@router.get("/{job_id}", response_model=JobRead)
async def get_job(job_id: uuid.UUID, current_user: CurrentUser, db: TenantDB) -> JobRead:
    return JobRead.model_validate(await JobService(db).get(job_id))


@router.patch("/{job_id}", response_model=JobRead)
async def update_job(
    job_id: uuid.UUID, payload: JobUpdate, author: JobAuthor, db: TenantDB, queue: EmbeddingQueueDep
) -> JobRead:
    service = JobService(db)
    previous_status = (await service.get(job_id)).status
    job = await service.update(job_id, payload, actor=author)
    if job.status is JobStatus.PUBLISHED and previous_status is not JobStatus.PUBLISHED:
        await _queue_resurfacing(queue, job.tenant_id, job.id)
    return JobRead.model_validate(job)


# ----------------------------------------------------------------------------- public
@public_router.get("/{tenant_subdomain}", response_model=PublicJobBoard)
async def list_public_jobs(
    tenant_id: PathTenantId, db: Annotated[AsyncSession, Depends(get_session)]
) -> PublicJobBoard:
    tenant, jobs = await JobService(db).list_public(tenant_id)
    return PublicJobBoard(
        organization=TenantBranding.model_validate(tenant),
        jobs=[PublicJobSummary.model_validate(job) for job in jobs],
    )


@public_router.get("/{tenant_subdomain}/{slug}", response_model=PublicJobDetail)
async def get_public_job(
    slug: str, tenant_id: PathTenantId, db: Annotated[AsyncSession, Depends(get_session)]
) -> PublicJobDetail:
    tenant, job, json_ld = await JobService(db).get_public(tenant_id, slug)
    return PublicJobDetail(
        **PublicJobSummary.model_validate(job).model_dump(),
        description_html=job.description_html,
        organization=TenantBranding.model_validate(tenant),
        json_ld=json_ld,
    )
