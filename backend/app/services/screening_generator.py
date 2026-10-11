"""Dynamic AI pre-screening questions targeting a candidate's skill gaps (project.md Module 6).

Questions come from the local LLM (Ollama) under a strict schema. When the model is unavailable
(e.g. still downloading) or its output fails validation twice, a deterministic generator builds
questions from the same gap analysis, so recruiters are never blocked. Every question set records
which path produced it. Screening is advisory only: nothing here can reject a candidate.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import ConflictError, NotFoundError
from app.models.candidate import Candidate, JobApplication
from app.models.collaboration import Screening, ScreeningStatus
from app.models.job import Job
from app.schemas.job import JobTaxonomy
from app.schemas.screening import (
    ScreeningEvaluation,
    ScreeningQuestion,
    ScreeningRead,
    ScreeningResponseRead,
)
from app.services.communications import CommunicationService
from app.services.job_generator import generate_validated
from app.services.llm_client import LLMClient, LLMOutputError, LLMUnavailableError
from app.services.screening_portal import issue_invitation, new_nonce, portal_link
from app.services.storage import StorageService

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You write asynchronous pre-screening questions for job candidates.
Rules:
- Each question targets one listed skill gap or transition area and references the candidate's
  actual background (e.g. "You have deep React experience; how would you approach Vue 3 ...").
- Open-ended, answerable in about 60 seconds of speech or a short paragraph.
- Never ask about age, family, health, religion, nationality, gender, salary history or any
  other legally protected characteristic. No trick questions.
- Return exactly the requested number of questions as JSON matching the schema."""


class _LLMQuestions(BaseModel):
    questions: list[ScreeningQuestion] = Field(min_length=3, max_length=5)


@dataclass(frozen=True, slots=True)
class GapAnalysis:
    missing_required: list[str]
    missing_preferred: list[str]
    matched: list[str]
    candidate_skills: list[str]
    recent_roles: list[str]

    @property
    def gaps(self) -> list[str]:
        return [*self.missing_required, *self.missing_preferred]


def _names(items: Any) -> list[str]:
    names = []
    for item in items or []:
        name = item.get("skill") or item.get("name") if isinstance(item, dict) else item
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return names


def analyse_gaps(job: Job, application: JobApplication, profile: dict[str, Any]) -> GapAnalysis:
    """Gaps from the M3 deterministic breakdown; falls back to taxonomy vs. parsed skills."""
    breakdown = application.match_breakdown_json.get("breakdown", {})
    skills = [s for s in profile.get("skills", []) if isinstance(s, str)]
    if breakdown:
        missing_required = _names(breakdown.get("missing_required"))
        missing_preferred = _names(breakdown.get("missing_preferred"))
        matched = _names(breakdown.get("matched_required")) + _names(
            breakdown.get("matched_preferred")
        )
    else:
        taxonomy = JobTaxonomy.model_validate(job.taxonomy_json)
        have = {s.lower() for s in skills}
        missing_required = [s.name for s in taxonomy.required_skills if s.name.lower() not in have]
        missing_preferred = [
            s.name for s in taxonomy.preferred_skills if s.name.lower() not in have
        ]
        matched = [s.name for s in taxonomy.required_skills if s.name.lower() in have]
    roles = [
        " at ".join(p for p in (r.get("title"), r.get("company")) if p)
        for r in profile.get("work_experience", [])[:3]
        if isinstance(r, dict)
    ]
    return GapAnalysis(
        missing_required, missing_preferred, matched, skills, [r for r in roles if r]
    )


def fallback_questions(job: Job, gaps: GapAnalysis, count: int) -> list[ScreeningQuestion]:
    """Deterministic, gap-targeted questions used when the local LLM is unavailable."""
    anchor = (gaps.matched or gaps.candidate_skills or ["your current stack"])[0]
    questions: list[ScreeningQuestion] = []
    for skill in gaps.missing_required:
        questions.append(
            ScreeningQuestion(
                question=(
                    f"Your background shows strong experience with {anchor}. This role relies on "
                    f"{skill}. How would you get productive with {skill} on a production project, "
                    f"and what from your {anchor} work would carry over?"
                ),
                targets=skill,
                rationale=f"{skill} is a required skill not evidenced in the resume.",
            )
        )
    for skill in gaps.missing_preferred:
        questions.append(
            ScreeningQuestion(
                question=(
                    f"Have you had any exposure to {skill}? If not, how would you evaluate whether "
                    f"it is the right fit for a project you are working on?"
                ),
                targets=skill,
                rationale=f"{skill} is a preferred skill not evidenced in the resume.",
            )
        )
    for skill in gaps.matched:
        questions.append(
            ScreeningQuestion(
                question=(
                    f"Walk us through the most complex problem you solved with {skill}: the "
                    f"constraints, the approach you chose, and how you measured the result."
                ),
                targets=skill,
                rationale=f"Confirms depth in {skill}, a matched requirement.",
            )
        )
    questions.append(
        ScreeningQuestion(
            question=(
                f"What draws you to the {job.title} role, and which part of your experience do "
                f"you expect to be most relevant in the first 90 days?"
            ),
            targets="Motivation & role fit",
            rationale="General role-fit question.",
        )
    )
    unique = list({q.question: q for q in questions}.values())
    return unique[:count]


async def generate_questions(
    llm: LLMClient | None, job: Job, gaps: GapAnalysis, count: int
) -> tuple[list[ScreeningQuestion], str, str | None]:
    """Returns (questions, source, detail)."""
    if llm is None:
        return fallback_questions(job, gaps, count), "fallback", "No LLM configured"
    taxonomy = JobTaxonomy.model_validate(job.taxonomy_json)
    prompt = (
        f"Role: {job.title}\n"
        f"Required skills: {', '.join(s.name for s in taxonomy.required_skills) or 'n/a'}\n"
        f"Preferred skills: {', '.join(s.name for s in taxonomy.preferred_skills) or 'n/a'}\n"
        f"Candidate skills: {', '.join(gaps.candidate_skills) or 'unknown'}\n"
        f"Recent roles: {'; '.join(gaps.recent_roles) or 'unknown'}\n"
        f"Missing required skills: {', '.join(gaps.missing_required) or 'none'}\n"
        f"Missing preferred skills: {', '.join(gaps.missing_preferred) or 'none'}\n"
        f"Write exactly {count} questions. Prioritise missing required skills; if there are fewer "
        f"gaps than questions, probe depth in the matched skills."
    )
    try:
        result = await generate_validated(llm, _LLMQuestions, system=SYSTEM_PROMPT, prompt=prompt)
    except (LLMUnavailableError, LLMOutputError) as exc:
        logger.warning("Screening generation fell back to templates: %s", exc.detail)
        return fallback_questions(job, gaps, count), "fallback", exc.detail
    questions = result.questions[:count]
    if len(questions) < count:  # top up from the deterministic set
        extra = [
            q
            for q in fallback_questions(job, gaps, 5)
            if q.question not in {x.question for x in questions}
        ]
        questions += extra[: count - len(questions)]
    return questions, "ai", None


class ScreeningService:
    def __init__(
        self, session: AsyncSession, llm: LLMClient | None, storage: StorageService | None = None
    ) -> None:
        self._session = session
        self._llm = llm
        self._storage = storage

    async def _load(self, application_id: uuid.UUID) -> tuple[JobApplication, Job, dict[str, Any]]:
        application = await self._session.get(JobApplication, application_id)
        if application is None:
            raise NotFoundError("Application not found")
        job = await self._session.get(Job, application.job_id)
        candidate = await self._session.get(Candidate, application.candidate_id)
        if job is None or candidate is None:  # pragma: no cover - foreign keys
            raise NotFoundError("Application not found")
        profile = application.extracted_profile_json or candidate.parsed_profile_json or {}
        return application, job, profile

    async def _latest(self, application_id: uuid.UUID) -> Screening | None:
        return (
            await self._session.execute(
                select(Screening)
                .where(Screening.application_id == application_id)
                .order_by(Screening.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    async def latest(self, application_id: uuid.UUID) -> ScreeningRead | None:
        if await self._session.get(JobApplication, application_id) is None:
            raise NotFoundError("Application not found")  # also other tenants' (RLS-hidden) ids
        screening = await self._latest(application_id)
        return await self._read_full(screening) if screening else None

    async def invite(self, application_id: uuid.UUID) -> ScreeningRead:
        """Send (or re-send) the candidate a fresh 72-hour single-use portal link."""
        application = await self._session.get(JobApplication, application_id)
        if application is None:
            raise NotFoundError("Application not found")
        screening = await self._latest(application_id)
        if screening is None or not screening.questions_json:
            raise ConflictError("Generate screening questions before inviting the candidate")
        if screening.status is ScreeningStatus.COMPLETED:
            raise ConflictError("The candidate already submitted this screening")
        await issue_invitation(self._session, screening)
        await CommunicationService(self._session).cancel_queued(
            application_id, {"screening_invitation"}, "Superseded by a newer invitation"
        )
        await CommunicationService(self._session).queue(
            application, "screening_invitation", trigger="screening_invited"
        )
        await self._session.commit()
        return await self._read_full(screening)

    async def generate(
        self, application_id: uuid.UUID, *, count: int = 3, created_by: uuid.UUID | None = None
    ) -> ScreeningRead:
        application, job, profile = await self._load(application_id)
        latest = await self._latest(application_id)
        if latest is not None and latest.status in (
            ScreeningStatus.OPENED,
            ScreeningStatus.COMPLETED,
        ):
            raise ConflictError(
                "The candidate has already opened this screening; its questions can't change"
            )
        gaps = analyse_gaps(job, application, profile)
        questions, source, detail = await generate_questions(self._llm, job, gaps, count)

        # Regenerating replaces a not-yet-started screening instead of piling up new ones.
        screening = (
            await self._session.execute(
                select(Screening).where(
                    Screening.application_id == application_id,
                    Screening.status == ScreeningStatus.PENDING,
                )
            )
        ).scalar_one_or_none()
        if screening is None:
            screening = Screening(
                tenant_id=application.tenant_id,
                application_id=application_id,
                access_token=new_nonce(),
                status=ScreeningStatus.PENDING,
                expires_at=datetime.now(UTC)
                + timedelta(hours=get_settings().SCREENING_TOKEN_TTL_HOURS),
            )
            self._session.add(screening)
        screening.questions_json = [q.model_dump() for q in questions]
        screening.generation_source = source
        screening.created_by = created_by
        screening.ai_evaluation_json = {
            "generation": {
                "source": source,
                "detail": detail,
                "model": self._llm.model if self._llm and source == "ai" else None,
                "skill_gaps": gaps.gaps,
            }
        }
        await self._session.commit()
        return await self._read_full(screening)

    async def _read_full(self, screening: Screening) -> ScreeningRead:
        read = self._read(screening)
        answers = {int(r["index"]): r for r in screening.responses_json}
        responses = []
        for index, question in enumerate(screening.questions_json):
            entry = answers.get(index, {})
            audio_url = None
            if entry.get("audio_key") and self._storage is not None:
                audio_url = await self._storage.get_presigned_download_url(entry["audio_key"])
            updated = entry.get("updated_at")
            responses.append(
                ScreeningResponseRead(
                    index=index,
                    question=question["question"],
                    targets=question.get("targets", ""),
                    type=entry.get("type"),
                    text=entry.get("text"),
                    transcript=entry.get("transcript"),
                    transcription_engine=entry.get("transcription_engine"),
                    transcription_error=entry.get("transcription_error"),
                    audio_url=audio_url,
                    updated_at=datetime.fromisoformat(updated) if updated else None,
                )
            )
        evaluation = screening.ai_evaluation_json.get("evaluation")
        open_link = screening.status in (ScreeningStatus.PENDING, ScreeningStatus.OPENED) and (
            screening.invited_at is not None
        )
        return read.model_copy(
            update={
                "invited_at": screening.invited_at,
                "opened_at": screening.opened_at,
                "submitted_at": screening.submitted_at,
                "candidate_link": portal_link(screening) if open_link else None,
                "responses": responses,
                "evaluation_status": screening.ai_evaluation_json.get(
                    "evaluation_status", "not_started"
                ),
                "evaluation": ScreeningEvaluation.model_validate(evaluation)
                if evaluation
                else None,
            }
        )

    @staticmethod
    def _read(screening: Screening) -> ScreeningRead:
        generation = screening.ai_evaluation_json.get("generation", {})
        return ScreeningRead(
            id=screening.id,
            application_id=screening.application_id,
            status=screening.status,
            source=screening.generation_source,  # type: ignore[arg-type]
            source_detail=generation.get("detail"),
            model=generation.get("model"),
            skill_gaps=generation.get("skill_gaps", []),
            questions=[ScreeningQuestion.model_validate(q) for q in screening.questions_json],
            expires_at=screening.expires_at,
            created_at=screening.created_at,
        )
