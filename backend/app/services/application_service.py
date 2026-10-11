"""Zero-friction candidate ingestion gateway (project.md Module 3).

Flow: presign upload -> browser PUTs the file straight to storage -> submit (``pending_extraction``,
queued for the M3 extractor) -> webhook stores the parsed profile -> candidate confirms it.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePath
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import signing
from app.core.config import get_settings
from app.core.database import AsyncSessionLocal, set_tenant_context
from app.core.exceptions import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    UnprocessableError,
)
from app.models.candidate import ApplicationStatus, Candidate, JobApplication
from app.models.job import Job, JobStatus
from app.repositories.candidate_repository import CandidateRepository
from app.repositories.job_repository import JobRepository
from app.schemas.application import (
    ConfirmProfileRequest,
    PresignUploadRequest,
    SubmitApplicationRequest,
)
from app.services.communications import CommunicationService
from app.services.storage import PresignedUpload, StorageService

STATUS_TOKEN_PURPOSE = "application-status"
_UNSAFE_FILENAME_CHARS = re.compile(r"[\x00-\x1f\x7f/\\]+")


@dataclass(frozen=True, slots=True)
class SubmittedApplication:
    application: JobApplication
    status_token: str


def create_status_token(tenant_id: uuid.UUID, application_id: uuid.UUID) -> str:
    expires = signing.expiry(get_settings().APPLICATION_STATUS_TOKEN_TTL_SECONDS)
    signature = signing.sign(STATUS_TOKEN_PURPOSE, tenant_id, application_id, expires)
    return f"{tenant_id}.{expires}.{signature}"


def verify_status_token(application_id: uuid.UUID, token: str) -> uuid.UUID:
    """Return the tenant id bound into a valid token; raise 403 otherwise."""
    try:
        tenant_raw, expires_raw, signature = token.split(".")
        tenant_id, expires = uuid.UUID(tenant_raw), int(expires_raw)
    except ValueError:
        raise PermissionDeniedError("Invalid status token") from None
    if signing.is_expired(expires) or not signing.verify(
        signature, STATUS_TOKEN_PURPOSE, tenant_id, application_id, expires
    ):
        raise PermissionDeniedError("Invalid or expired status token")
    return tenant_id


def _split_name(full_name: str | None) -> tuple[str | None, str | None]:
    parts = (full_name or "").split()
    if not parts:
        return None, None
    if len(parts) == 1:
        return parts[0][:100], None
    return " ".join(parts[:-1])[:100], parts[-1][:100]


def _safe_file_name(file_name: str) -> str:
    name = _UNSAFE_FILENAME_CHARS.sub("_", PurePath(file_name.replace("\\", "/")).name).strip()
    return (name or "resume")[:255]


class ApplicationService:
    def __init__(self, session: AsyncSession, storage: StorageService) -> None:
        self._session = session
        self._storage = storage
        self._jobs = JobRepository(session)
        self._candidates = CandidateRepository(session)

    async def _open_job(self, job_id: uuid.UUID) -> Job:
        job = await self._jobs.get(job_id)
        if job is None or job.status is not JobStatus.PUBLISHED:
            raise NotFoundError("This job is not accepting applications")
        return job

    async def presign_upload(
        self, payload: PresignUploadRequest, *, tenant_id: uuid.UUID
    ) -> PresignedUpload:
        await self._open_job(payload.job_id)
        if payload.file_size_bytes > self._storage.max_resume_size_bytes:
            raise UnprocessableError(
                f"Resumes must be at most {self._storage.max_resume_size_bytes // (1024 * 1024)} MB"
            )
        return await self._storage.generate_presigned_upload_url(
            payload.file_name, payload.mime_type, tenant_id
        )

    async def submit(
        self, payload: SubmitApplicationRequest, *, tenant_id: uuid.UUID
    ) -> SubmittedApplication:
        job = await self._open_job(payload.job_id)
        self._storage.parse_resume_key(payload.s3_key, tenant_id)
        if await self._candidates.resume_key_in_use(payload.s3_key):
            raise ConflictError("This resume upload has already been submitted")

        email = str(payload.email).lower()
        candidate = await self._candidates.get_by_email(email)
        if candidate is not None and await self._candidates.application_exists(
            job.id, candidate.id
        ):
            await self._storage.delete(payload.s3_key)
            raise ConflictError("You have already applied to this role")

        stored = await self._storage.verify_resume_upload(payload.s3_key, tenant_id)

        if candidate is None:
            first_name, last_name = _split_name(payload.full_name)
            candidate = Candidate(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                first_name=first_name,
                last_name=last_name,
                email=email,
                phone=payload.phone,
                linkedin_url=str(payload.linkedin_url) if payload.linkedin_url else None,
                original_resume_s3_key=stored.key,
                resume_file_name=_safe_file_name(payload.file_name),
                resume_mime_type=stored.mime_type,
                resume_file_size_bytes=stored.size_bytes,
            )
            self._candidates.add(candidate)
        # An existing candidate profile is never modified by an unauthenticated submission;
        # the new resume is attached to this application only.

        applied_stage = await self._jobs.get_stage_by_type(job.id, "applied")
        application = JobApplication(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            job_id=job.id,
            candidate_id=candidate.id,
            current_stage_id=applied_stage.id if applied_stage else None,
            status=ApplicationStatus.PENDING_EXTRACTION,
            resume_s3_key=stored.key,
        )
        self._candidates.add(application)
        try:
            await self._session.flush()
            await CommunicationService(self._session).queue(
                application, "application_received", trigger="application_submitted"
            )
            await self._session.commit()
        except IntegrityError:
            await self._session.rollback()
            raise ConflictError("You have already applied to this role") from None
        return SubmittedApplication(application, create_status_token(tenant_id, application.id))

    async def confirm(
        self, application_id: uuid.UUID, payload: ConfirmProfileRequest
    ) -> JobApplication:
        """Store the candidate-verified profile (Module 3 "5-second review")."""
        application = await self._candidates.get_application(application_id)
        if application is None:
            raise NotFoundError("Application not found")
        if not application.match_breakdown_json and not application.extraction_error:
            raise ConflictError("Your resume is still being processed; please wait a moment")
        if application.confirmed_at is not None:
            raise ConflictError("This application has already been confirmed")
        candidate = await self._candidates.get(application.candidate_id)
        if candidate is None:  # pragma: no cover - guaranteed by the foreign key
            raise NotFoundError("Application not found")

        profile = payload.model_dump(mode="json")
        profile["email"] = str(payload.email).lower()
        application.confirmed_profile_json = profile
        application.confirmed_at = datetime.now(UTC)

        # Only the application that created the candidate may update the shared profile;
        # a later anonymous application never overwrites an existing person's record.
        if candidate.original_resume_s3_key == application.resume_s3_key:
            if profile["email"] != candidate.email:
                clash = await self._candidates.get_by_email(profile["email"])
                if clash is not None and clash.id != candidate.id:
                    raise ConflictError("That email is already used by another applicant")
                candidate.email = profile["email"]
            candidate.first_name, candidate.last_name = _split_name(payload.full_name)
            candidate.phone = payload.phone
            candidate.linkedin_url = payload.linkedin_url
            candidate.github_url = payload.github_url
            candidate.location = payload.location
            candidate.parsed_profile_json = {
                **candidate.parsed_profile_json,
                "verified_skills": payload.skills,
            }
        try:
            await self._session.commit()
        except IntegrityError:
            await self._session.rollback()
            raise ConflictError("That email is already used by another applicant") from None
        return application


async def load_application_event(
    tenant_id: uuid.UUID, application_id: uuid.UUID
) -> dict[str, Any] | None:
    """Candidate-facing status snapshot for the SSE stream (opens its own short session).

    ``processing`` until the M3 webhook lands, then ``parsed`` (with the fields extracted from
    *this* application's resume, for the candidate to verify) or ``failed``.
    """
    async with AsyncSessionLocal() as session:
        await set_tenant_context(session, tenant_id)
        repo = CandidateRepository(session)
        application = await repo.get_application(application_id)
        if application is None:
            return None
        base = {"application_id": str(application_id)}
        if application.extraction_error:
            return {**base, "status": "failed", "confirmed": application.confirmed_at is not None}
        if not application.match_breakdown_json:
            return {**base, "status": "processing"}
        candidate = await repo.get(application.candidate_id)
        if candidate is None:  # pragma: no cover - guaranteed by the foreign key
            return None
        profile = application.extracted_profile_json
        contact = profile.get("contact") or {}
        stored_name = " ".join(p for p in (candidate.first_name, candidate.last_name) if p)
        return {
            **base,
            "status": "parsed",
            "confirmed": application.confirmed_at is not None,
            "candidate": {
                "full_name": contact.get("full_name") or stored_name or None,
                "email": candidate.email,
                "phone": contact.get("phone") or candidate.phone,
                "linkedin_url": contact.get("linkedin") or candidate.linkedin_url,
                "github_url": contact.get("github") or candidate.github_url,
                "location": contact.get("location") or candidate.location,
                "skills": [s for s in profile.get("skills", []) if isinstance(s, str)],
            },
        }
