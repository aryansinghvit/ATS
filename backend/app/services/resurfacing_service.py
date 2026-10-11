"""Proactive talent resurfacing (project.md Module 5) and 1-click invites to a new role.

When a job is published its requirements are embedded with the same on-device model as candidate
profiles, the tenant's talent pool is searched (RLS-scoped), and past applicants above the
similarity threshold who have not applied yet are recorded as suggestions; the job's creator gets
an in-app alert ("Found N past applicants with ≥70% match…").
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from pgvector.sqlalchemy import Vector
from sqlalchemy import bindparam, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import ConflictError, NotFoundError, UnprocessableError
from app.models.audit import AuditLog
from app.models.candidate import ApplicationStatus, Candidate, JobApplication
from app.models.collaboration import JobTalentMatch
from app.models.job import Job, JobStatus
from app.models.user import User, UserRole
from app.repositories.job_repository import JobRepository
from app.schemas.job import JobTaxonomy
from app.schemas.talent_pool import PastApplication, ResurfacingResult, TalentSuggestion
from app.services.communications import CommunicationService
from app.services.embedding_service import Embedder, embed_query
from app.services.notification_service import notify
from app.services.talent_pool_service import tune_ann

_NEAREST_SQL = text(
    """
    SELECT e.candidate_id, 1 - (e.embedding <=> :job_vector) AS similarity
    FROM candidate_embeddings e
    WHERE NOT EXISTS (
        SELECT 1 FROM job_applications a
        WHERE a.job_id = :job_id AND a.candidate_id = e.candidate_id
    )
    ORDER BY e.embedding <=> :job_vector
    LIMIT :limit
    """
).bindparams(bindparam("job_vector", type_=Vector(384)))


def job_profile_text(job: Job) -> str:
    """What the role needs, phrased like a sourcing query (embedded with the query instruction)."""
    taxonomy = JobTaxonomy.model_validate(job.taxonomy_json)
    lines = [f"{job.title} ({job.department})"]
    if taxonomy.required_skills:
        lines.append("Required skills: " + ", ".join(s.name for s in taxonomy.required_skills))
    if taxonomy.preferred_skills:
        lines.append("Preferred skills: " + ", ".join(s.name for s in taxonomy.preferred_skills))
    if taxonomy.min_total_experience_years:
        lines.append(f"Experience: {taxonomy.min_total_experience_years:g}+ years")
    lines.append(job.description_markdown[:1500])
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class InviteResult:
    application: JobApplication
    email_queued: bool


class ResurfacingService:
    def __init__(self, session: AsyncSession, embedder: Embedder | None = None) -> None:
        self._session = session
        self._embedder = embedder

    async def _job(self, job_id: uuid.UUID) -> Job:
        job = await self._session.get(Job, job_id)
        if job is None:
            raise NotFoundError("Job not found")
        return job

    async def scan(self, job_id: uuid.UUID, *, threshold: float | None = None) -> ResurfacingResult:
        settings = get_settings()
        threshold = settings.RESURFACING_THRESHOLD if threshold is None else threshold
        job = await self._job(job_id)
        if job.status is not JobStatus.PUBLISHED or self._embedder is None:
            return ResurfacingResult(
                job_id=job_id, threshold=threshold, matches=0, new_matches=0, notified=0
            )

        vector = await embed_query(self._embedder, job_profile_text(job))
        await tune_ann(self._session)
        rows = (
            await self._session.execute(
                _NEAREST_SQL,
                {"job_vector": vector, "job_id": job_id, "limit": settings.RESURFACING_LIMIT},
            )
        ).all()
        hits = {
            row.candidate_id: float(row.similarity) for row in rows if row.similarity >= threshold
        }

        existing = {
            m.candidate_id: m
            for m in (
                await self._session.execute(
                    select(JobTalentMatch).where(JobTalentMatch.job_id == job_id)
                )
            ).scalars()
        }
        new = 0
        for candidate_id, similarity in hits.items():
            match = existing.get(candidate_id)
            if match is None:
                self._session.add(
                    JobTalentMatch(
                        tenant_id=job.tenant_id,
                        job_id=job_id,
                        candidate_id=candidate_id,
                        similarity=Decimal(str(round(similarity, 4))),
                    )
                )
                new += 1
            elif match.status == "suggested":
                match.similarity = Decimal(str(round(similarity, 4)))

        notified = 0
        if new:
            recipients = await self._recipients(job)
            notified = notify(
                self._session,
                tenant_id=job.tenant_id,
                user_ids=recipients,
                type="resurfacing",
                title=(
                    f"Found {new} past applicant{'s' if new != 1 else ''} with "
                    f"≥{round(threshold * 100)}% match for {job.title}"
                ),
                body="Review them and invite the best fits with one click.",
                link=f"/workspace/jobs/{job.id}/pipeline?resurfaced=1",
                entity_type="job",
                entity_id=job.id,
            )
        await self._session.commit()
        return ResurfacingResult(
            job_id=job_id,
            threshold=threshold,
            matches=len(hits),
            new_matches=new,
            notified=notified,
        )

    async def _recipients(self, job: Job) -> list[uuid.UUID]:
        creator = await self._session.get(User, job.creator_id) if job.creator_id else None
        if creator is not None and creator.is_active:
            return [creator.id]
        admins = await self._session.execute(
            select(User.id).where(User.role == UserRole.ADMIN, User.is_active.is_(True))
        )
        return list(admins.scalars())

    async def suggestions(self, job_id: uuid.UUID) -> list[TalentSuggestion]:
        await self._job(job_id)
        rows = (
            await self._session.execute(
                select(JobTalentMatch, Candidate)
                .join(Candidate, Candidate.id == JobTalentMatch.candidate_id)
                .where(JobTalentMatch.job_id == job_id, JobTalentMatch.status != "dismissed")
                .order_by(JobTalentMatch.similarity.desc())
            )
        ).all()
        candidate_ids = [candidate.id for _, candidate in rows]
        history: dict[uuid.UUID, list[PastApplication]] = defaultdict(list)
        if candidate_ids:
            for application, title in (
                await self._session.execute(
                    select(JobApplication, Job.title)
                    .join(Job, Job.id == JobApplication.job_id)
                    .where(JobApplication.candidate_id.in_(candidate_ids))
                    .order_by(JobApplication.applied_at.desc())
                )
            ).all():
                score = application.deterministic_match_score
                history[application.candidate_id].append(
                    PastApplication(
                        application_id=application.id,
                        job_id=application.job_id,
                        job_title=title,
                        status=application.status,
                        deterministic_match_score=float(score) if score is not None else None,
                        applied_at=application.applied_at,
                    )
                )
        return [
            TalentSuggestion(
                candidate_id=candidate.id,
                full_name=" ".join(p for p in (candidate.first_name, candidate.last_name) if p)
                or None,
                email=candidate.email,
                location=candidate.location,
                similarity=float(match.similarity),
                status=match.status,
                skills=[
                    s for s in candidate.parsed_profile_json.get("skills", []) if isinstance(s, str)
                ],
                applications=history.get(candidate.id, []),
            )
            for match, candidate in rows
        ]

    async def dismiss(self, job_id: uuid.UUID, candidate_id: uuid.UUID) -> None:
        match = (
            await self._session.execute(
                select(JobTalentMatch).where(
                    JobTalentMatch.job_id == job_id, JobTalentMatch.candidate_id == candidate_id
                )
            )
        ).scalar_one_or_none()
        if match is None:
            raise NotFoundError("Suggestion not found")
        match.status = "dismissed"
        await self._session.commit()

    async def invite(
        self, candidate_id: uuid.UUID, job_id: uuid.UUID, *, actor: User
    ) -> InviteResult:
        """Add a past applicant to a published job's Applied column and email them."""
        job = await self._job(job_id)
        if job.status is not JobStatus.PUBLISHED:
            raise UnprocessableError("Candidates can only be invited to published jobs")
        candidate = await self._session.get(Candidate, candidate_id)
        if candidate is None:
            raise NotFoundError("Candidate not found")
        already = await self._session.scalar(
            select(JobApplication.id).where(
                JobApplication.job_id == job_id, JobApplication.candidate_id == candidate_id
            )
        )
        if already is not None:
            raise ConflictError("This candidate is already in the job's pipeline")

        applied = await JobRepository(self._session).get_stage_by_type(job_id, "applied")
        application = JobApplication(
            id=uuid.uuid4(),
            tenant_id=job.tenant_id,
            job_id=job_id,
            candidate_id=candidate_id,
            current_stage_id=applied.id if applied else None,
            # Re-scored against this job's taxonomy by the M3 extractor.
            status=ApplicationStatus.PENDING_EXTRACTION,
            resume_s3_key=candidate.original_resume_s3_key,
        )
        self._session.add(application)
        await self._session.flush()
        email = await CommunicationService(self._session).queue(
            application, "talent_invitation", trigger="talent_pool_invite"
        )
        match = (
            await self._session.execute(
                select(JobTalentMatch).where(
                    JobTalentMatch.job_id == job_id, JobTalentMatch.candidate_id == candidate_id
                )
            )
        ).scalar_one_or_none()
        if match is not None:
            match.status = "invited"
        self._session.add(
            AuditLog(
                tenant_id=job.tenant_id,
                actor_id=actor.id,
                action="candidate_invited",
                entity_type="job_application",
                entity_id=application.id,
                metadata_json={"job_id": str(job_id), "candidate_id": str(candidate_id)},
            )
        )
        await self._session.commit()
        return InviteResult(application=application, email_queued=email is not None)
