"""Job requisition lifecycle (create, edit, publish) and the public careers view."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    UnprocessableError,
)
from app.core.markdown import render_markdown
from app.models.job import Job, JobStatus, PipelineStage
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.repositories.job_repository import JobRepository
from app.repositories.tenant_repository import TenantRepository
from app.schemas.job import JobCreate, JobDescriptionRequest, JobDraft, JobTaxonomy, JobUpdate
from app.services.job_generator import (
    JobDescriptionGenerator,
    TaxonomyExtractor,
    normalize_taxonomy,
)
from app.services.job_seo import build_job_posting_json_ld
from app.services.llm_client import LLMClient

# Default Kanban columns seeded for every new job (project.md Module 4 / task.md Prompt 3.3),
# with the automated actions each column triggers when a candidate enters it.
DEFAULT_PIPELINE: tuple[tuple[str, str, dict[str, list[str]]], ...] = (
    ("Applied", "applied", {}),
    ("Pre-Screen", "screen", {"on_enter": ["ai_pre_screen"]}),  # AI top-of-funnel filter first
    ("Tech Screen", "screen", {}),
    ("Interview", "interview", {"on_enter": ["interview_invitation_email"]}),
    ("Offer", "offer", {"on_enter": ["offer_email"]}),
    ("Hired", "hired", {}),
    ("Rejected", "rejected", {"on_enter": ["polite_rejection_email"]}),
)

ALLOWED_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.DRAFT: frozenset({JobStatus.PUBLISHED, JobStatus.ARCHIVED}),
    JobStatus.PUBLISHED: frozenset({JobStatus.PAUSED, JobStatus.CLOSED}),
    JobStatus.PAUSED: frozenset({JobStatus.PUBLISHED, JobStatus.CLOSED}),
    JobStatus.CLOSED: frozenset({JobStatus.PUBLISHED, JobStatus.ARCHIVED}),
    JobStatus.ARCHIVED: frozenset(),
}

_SLUG_INVALID = re.compile(r"[^a-z0-9]+")


def slugify(text: str, *, max_length: int = 80) -> str:
    slug = _SLUG_INVALID.sub("-", text.lower()).strip("-")[:max_length].strip("-")
    return slug or "job"


class JobService:
    def __init__(self, session: AsyncSession, *, llm: LLMClient | None = None) -> None:
        self._session = session
        self._jobs = JobRepository(session)
        self._tenants = TenantRepository(session)
        self._llm = llm

    # ------------------------------------------------------------------ AI generation
    async def generate_description(
        self, request: JobDescriptionRequest, *, tenant_id: uuid.UUID
    ) -> JobDraft:
        tenant = await self._tenant(tenant_id)
        return await JobDescriptionGenerator(self._require_llm()).generate(
            request, company_name=tenant.name
        )

    # ------------------------------------------------------------------------ CRUD
    async def create(self, payload: JobCreate, *, actor: User) -> Job:
        status = JobStatus(payload.status)
        if actor.role is UserRole.HIRING_MANAGER and status is not JobStatus.DRAFT:
            raise PermissionDeniedError("Hiring managers can only create draft requisitions")

        if payload.taxonomy is not None:
            taxonomy = normalize_taxonomy(payload.taxonomy)
        else:
            taxonomy = await TaxonomyExtractor(self._require_llm()).extract(
                title=payload.title, description_markdown=payload.description_markdown
            )
        if status is JobStatus.PUBLISHED:
            self._ensure_publishable(taxonomy)

        slug = payload.slug or slugify(payload.title)
        if payload.slug and await self._jobs.slug_exists(slug):
            raise ConflictError(f"A job with slug '{slug}' already exists")
        slug = slug if payload.slug else await self._unique_slug(slug)

        job = Job(
            id=uuid.uuid4(),
            tenant_id=actor.tenant_id,
            title=payload.title,
            slug=slug,
            department=payload.department,
            location=payload.location,
            remote_policy=payload.remote_policy.value,
            employment_type=payload.employment_type.value,
            seniority_level=payload.seniority_level,
            salary_min=payload.salary_min,
            salary_max=payload.salary_max,
            currency=payload.currency,
            description_markdown=payload.description_markdown,
            description_html=render_markdown(payload.description_markdown),
            taxonomy_json=taxonomy.model_dump(mode="json"),
            status=status,
            published_at=datetime.now(UTC) if status is JobStatus.PUBLISHED else None,
            creator_id=actor.id,
            scheduling_url=payload.scheduling_url,
        )
        self._jobs.add(job)
        self._jobs.add_stages(
            PipelineStage(
                tenant_id=actor.tenant_id,
                job_id=job.id,
                name=name,
                position_index=index,
                stage_type=stage_type,
                automated_actions_json=actions,
            )
            for index, (name, stage_type, actions) in enumerate(DEFAULT_PIPELINE)
        )
        await self._commit(f"A job with slug '{slug}' already exists")
        return job

    async def list(self, *, status: JobStatus | None = None) -> list[Job]:
        return await self._jobs.list(statuses=[status] if status else None)

    async def get(self, job_id: uuid.UUID) -> Job:
        job = await self._jobs.get(job_id)
        if job is None:
            raise NotFoundError("Job not found")
        return job

    async def update(self, job_id: uuid.UUID, payload: JobUpdate, *, actor: User) -> Job:
        job = await self.get(job_id)
        changes = payload.model_dump(exclude_unset=True)
        target_status = JobStatus(changes["status"]) if changes.get("status") else job.status

        if actor.role is UserRole.HIRING_MANAGER and (
            job.status is not JobStatus.DRAFT or target_status is not JobStatus.DRAFT
        ):
            raise PermissionDeniedError("Hiring managers can only edit draft requisitions")

        if payload.taxonomy is not None:
            job.taxonomy_json = normalize_taxonomy(payload.taxonomy).model_dump(mode="json")
        if changes.get("slug") and changes["slug"] != job.slug:
            if await self._jobs.slug_exists(changes["slug"], exclude_id=job.id):
                raise ConflictError(f"A job with slug '{changes['slug']}' already exists")
            job.slug = changes["slug"]
        for field in ("title", "department", "location", "seniority_level", "currency"):
            if field in changes and (changes[field] is not None or field == "seniority_level"):
                setattr(job, field, changes[field])
        for field in ("remote_policy", "employment_type"):
            if changes.get(field) is not None:
                setattr(job, field, str(changes[field]))
        for field in ("salary_min", "salary_max", "scheduling_url"):
            if field in changes:
                setattr(job, field, changes[field])
        if (
            job.salary_min is not None
            and job.salary_max is not None
            and job.salary_min > job.salary_max
        ):
            raise UnprocessableError("salary_min must not exceed salary_max")
        if changes.get("description_markdown") is not None:
            job.description_markdown = changes["description_markdown"]
            job.description_html = render_markdown(job.description_markdown)

        if target_status is not job.status:
            if target_status not in ALLOWED_TRANSITIONS[job.status]:
                raise UnprocessableError(
                    f"Cannot change job status from '{job.status}' to '{target_status}'"
                )
            if target_status is JobStatus.PUBLISHED:
                self._ensure_publishable(JobTaxonomy.model_validate(job.taxonomy_json))
                job.published_at = job.published_at or datetime.now(UTC)
            job.status = target_status

        await self._commit("Job update conflicts with another job")
        return job

    # -------------------------------------------------------------------- public view
    async def list_public(self, tenant_id: uuid.UUID) -> tuple[Tenant, list[Job]]:
        return await self._tenant(tenant_id), await self._jobs.list_published()

    async def get_public(self, tenant_id: uuid.UUID, slug: str) -> tuple[Tenant, Job, dict]:
        tenant = await self._tenant(tenant_id)
        job = await self._jobs.get_by_slug(slug)
        if job is None or job.status is not JobStatus.PUBLISHED:
            raise NotFoundError("Job not found")
        job_url = f"{get_settings().FRONTEND_URL.rstrip('/')}/careers/{tenant.subdomain}/{job.slug}"
        return tenant, job, build_job_posting_json_ld(job, tenant, job_url=job_url)

    # -------------------------------------------------------------------------- helpers
    def _require_llm(self) -> LLMClient:
        if self._llm is None:  # pragma: no cover - endpoints always inject a client
            raise RuntimeError("JobService was constructed without an LLM client")
        return self._llm

    async def _tenant(self, tenant_id: uuid.UUID) -> Tenant:
        tenant = await self._tenants.get(tenant_id)
        if tenant is None:
            raise NotFoundError("Workspace not found")
        return tenant

    @staticmethod
    def _ensure_publishable(taxonomy: JobTaxonomy) -> None:
        if not taxonomy.required_skills:
            raise UnprocessableError(
                "A job needs at least one required skill in its taxonomy before it can be "
                "published (the M3 extractor scores candidates against it)"
            )

    async def _unique_slug(self, base: str) -> str:
        if not await self._jobs.slug_exists(base):
            return base
        for suffix in range(2, 50):
            candidate = f"{base}-{suffix}"
            if not await self._jobs.slug_exists(candidate):
                return candidate
        return f"{base}-{uuid.uuid4().hex[:6]}"

    async def _commit(self, conflict_message: str) -> None:
        try:
            await self._session.commit()
        except IntegrityError:
            await self._session.rollback()
            raise ConflictError(conflict_message) from None
