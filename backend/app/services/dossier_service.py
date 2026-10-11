"""Split-screen candidate dossier (project.md Module 4) with blind-interviewer redaction (§8.2)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.models.audit import AuditLog
from app.models.candidate import Candidate, JobApplication
from app.models.job import Job, PipelineStage
from app.models.user import User, UserRole
from app.schemas.dossier import (
    CandidateDossier,
    DossierCandidate,
    DossierJob,
    DossierMatch,
    DossierProfile,
    DossierResume,
    DossierStage,
    SkillEvidence,
    StageEvent,
)
from app.services.storage import EXTENSION_MIME_TYPES, StorageService

_EXPERIENCE_KEYS = (
    "company",
    "title",
    "location",
    "start_date",
    "end_date",
    "highlights",
    "technologies",
)
_EDUCATION_KEYS = ("institution", "degree", "field_of_study", "graduation_year")
_PROJECT_KEYS = ("name", "description", "technologies", "url", "highlights")
# §8.2 blind mode removes names, contact details, addresses and graduation years.
_BLIND_EDUCATION_DROP = frozenset({"graduation_year"})


def _records(items: Any, keys: tuple[str, ...], drop: frozenset[str] = frozenset()) -> list[dict]:
    if not isinstance(items, list):
        return []
    return [
        {key: item[key] for key in keys if key in item and key not in drop}
        for item in items
        if isinstance(item, dict)
    ]


def _strings(items: Any) -> list[str]:
    return [item for item in items if isinstance(item, str)] if isinstance(items, list) else []


def _evidence(items: Any) -> list[SkillEvidence]:
    result = []
    for item in items or []:
        if isinstance(item, str):
            result.append(SkillEvidence(skill=item))
        elif isinstance(item, dict) and isinstance(item.get("skill") or item.get("name"), str):
            result.append(
                SkillEvidence(
                    skill=item.get("skill") or item["name"], evidence=item.get("evidence")
                )
            )
    return result


class DossierService:
    def __init__(self, session: AsyncSession, storage: StorageService) -> None:
        self._session = session
        self._storage = storage

    async def get(self, application_id: uuid.UUID, *, viewer: User) -> CandidateDossier:
        application = await self._session.get(JobApplication, application_id)
        if application is None:
            raise NotFoundError("Application not found")
        candidate = await self._session.get(Candidate, application.candidate_id)
        job = await self._session.get(Job, application.job_id)
        if candidate is None or job is None:  # pragma: no cover - guaranteed by foreign keys
            raise NotFoundError("Application not found")
        stage = (
            await self._session.get(PipelineStage, application.current_stage_id)
            if application.current_stage_id
            else None
        )
        blind = viewer.role is UserRole.INTERVIEWER

        # This application's extraction wins; fall back to the candidate's primary profile.
        profile = application.extracted_profile_json or candidate.parsed_profile_json or {}
        extension = application.resume_s3_key.rsplit(".", 1)[-1]
        resume_url = None
        ttl = None
        if not blind:
            resume_url = await self._storage.get_presigned_download_url(application.resume_s3_key)
            ttl = self._storage.presigned_url_ttl_seconds

        full_name = " ".join(p for p in (candidate.first_name, candidate.last_name) if p) or None
        candidate_view = DossierCandidate(
            id=candidate.id,
            display_name=f"Candidate {str(candidate.id)[:8].upper()}"
            if blind
            else full_name or candidate.email,
            full_name=None if blind else full_name,
            email=None if blind else candidate.email,
            phone=None if blind else candidate.phone,
            linkedin_url=None if blind else candidate.linkedin_url,
            github_url=None if blind else candidate.github_url,
            portfolio_url=None if blind else candidate.portfolio_url,
            location=None if blind else candidate.location,
        )

        breakdown = application.match_breakdown_json.get("breakdown", {})
        match = None
        if not blind:  # deterministic score is redacted for blind interviewers (Module 1 RBAC)
            score = application.deterministic_match_score
            match = DossierMatch(
                extraction_status="failed"
                if application.extraction_error
                else "complete"
                if application.match_breakdown_json
                else "pending",
                extraction_error=application.extraction_error,
                deterministic_match_score=float(score) if score is not None else None,
                matched_required=_evidence(breakdown.get("matched_required")),
                missing_required=_evidence(breakdown.get("missing_required")),
                matched_preferred=_evidence(breakdown.get("matched_preferred")),
                missing_preferred=_evidence(breakdown.get("missing_preferred")),
                experience_match=application.match_breakdown_json.get("experience_match"),
            )

        dossier = CandidateDossier(
            application_id=application.id,
            redacted=blind,
            job=DossierJob(id=job.id, title=job.title),
            status=application.status,
            stage=DossierStage(id=stage.id, name=stage.name, stage_type=stage.stage_type)
            if stage
            else None,
            applied_at=application.applied_at,
            confirmed_at=application.confirmed_at,
            version=application.version,
            candidate=candidate_view,
            resume=DossierResume(
                file_name=None if blind else candidate.resume_file_name,
                kind="docx" if extension == "docx" else "pdf",
                mime_type=EXTENSION_MIME_TYPES.get(extension, "application/pdf"),
                url=resume_url,
                url_expires_in=ttl,
            ),
            match=match,
            profile=DossierProfile(
                summary=None if blind else profile.get("summary"),
                skills=_strings(profile.get("skills")),
                verified_skills=_strings(
                    application.confirmed_profile_json.get("skills")
                    or candidate.parsed_profile_json.get("verified_skills")
                ),
                work_experience=_records(profile.get("work_experience"), _EXPERIENCE_KEYS),
                education=_records(
                    profile.get("education"),
                    _EDUCATION_KEYS,
                    _BLIND_EDUCATION_DROP if blind else frozenset(),
                ),
                projects=_records(profile.get("projects"), _PROJECT_KEYS),
            ),
            stage_history=await self._stage_history(application.id),
        )

        self._session.add(
            AuditLog(
                tenant_id=application.tenant_id,
                actor_id=viewer.id,
                action="dossier_viewed",
                entity_type="job_application",
                entity_id=application.id,
                metadata_json={"redacted": blind, "resume_url_issued": resume_url is not None},
            )
        )
        await self._session.commit()
        return dossier

    async def _stage_history(self, application_id: uuid.UUID) -> list[StageEvent]:
        rows = (
            await self._session.execute(
                select(AuditLog, User.full_name)
                .outerjoin(User, User.id == AuditLog.actor_id)
                .where(
                    AuditLog.entity_id == application_id,
                    AuditLog.action == "stage_changed",
                )
                .order_by(AuditLog.created_at)
            )
        ).all()
        return [
            StageEvent(
                at=log.created_at,
                from_stage=log.metadata_json.get("from_stage"),
                to_stage=log.metadata_json.get("to_stage"),
                actor_name=actor_name,
            )
            for log, actor_name in rows
        ]
