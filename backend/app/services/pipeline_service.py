"""Match-scored Kanban pipeline (project.md Module 4): board, stage moves, audit, live events."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    UnprocessableError,
)
from app.models.audit import AuditLog
from app.models.candidate import ApplicationStatus, Candidate, JobApplication
from app.models.job import Job, JobStatus, PipelineStage
from app.models.user import User, UserRole
from app.schemas.job import JobTaxonomy
from app.schemas.pipeline import (
    CardCandidate,
    MatchChips,
    MoveStageRequest,
    PipelineBoard,
    PipelineCard,
    PipelineColumn,
    PipelineFilters,
    PipelineJob,
)
from app.services.communications import CommunicationService
from app.services.events import EventBroker, pipeline_channel, publish_safely
from app.services.stage_actions import STAGE_ACTION_HANDLERS, StageActionContext

logger = logging.getLogger(__name__)

APPLIED_STAGE = "applied"
# Hiring managers may only move candidates into these stage types (Module 1 RBAC: "Allowed Stages").
HIRING_MANAGER_TARGET_STAGES = frozenset({"interview", "offer", "rejected"})
# Stage types that imply an application status; other stages keep the current status.
STAGE_STATUS = {
    "offer": ApplicationStatus.OFFERED,
    "hired": ApplicationStatus.HIRED,
    "rejected": ApplicationStatus.REJECTED,
}
_DECISION_STATUSES = frozenset(STAGE_STATUS.values())


def _skill_names(items: Any) -> list[str]:
    names = []
    for item in items or []:
        name = item.get("skill") or item.get("name") if isinstance(item, dict) else item
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return names


def _status_after_move(stage_type: str, current: ApplicationStatus) -> ApplicationStatus:
    if stage_type in STAGE_STATUS:
        return STAGE_STATUS[stage_type]
    if current in _DECISION_STATUSES:  # moved back out of offer/hired/rejected
        return ApplicationStatus.APPLIED
    return current


def _score_key(card: PipelineCard) -> tuple[bool, float]:
    score = card.deterministic_match_score
    return (score is None, -(score or 0.0))


def sort_cards(cards: list[PipelineCard], stage_type: str, mode: str) -> list[PipelineCard]:
    """Server-side ordering; the frontend mirrors it for optimistic and live updates."""
    if mode == "score" or (mode == "default" and stage_type == APPLIED_STAGE):
        return sorted(cards, key=lambda c: (*_score_key(c), c.applied_at))
    if mode == "applied_desc":
        return sorted(cards, key=lambda c: c.applied_at, reverse=True)
    if mode == "applied_asc":
        return sorted(cards, key=lambda c: c.applied_at)
    if mode == "missing_skills":
        # Fewest gaps first; candidates still being scored go last.
        return sorted(
            cards,
            key=lambda c: (
                c.extraction_status != "complete",
                len(c.match.missing_required),
                *_score_key(c),
            ),
        )
    return sorted(cards, key=lambda c: c.stage_entered_at)  # default for later stages: FIFO


def matches_filters(card: PipelineCard, filters: PipelineFilters) -> bool:
    if filters.min_score is not None and (
        card.deterministic_match_score is None or card.deterministic_match_score < filters.min_score
    ):
        return False
    if filters.missing_skill:
        wanted = filters.missing_skill.casefold()
        if wanted not in {skill.casefold() for skill in card.match.missing_required}:
            return False
    if filters.q:
        needle = filters.q.casefold()
        haystack = f"{card.candidate.full_name or ''} {card.candidate.email}".casefold()
        if needle not in haystack:
            return False
    return True


def build_card(application: JobApplication, candidate: Candidate) -> PipelineCard:
    breakdown = application.match_breakdown_json.get("breakdown", {})
    if application.extraction_error:
        extraction_status = "failed"
    elif application.match_breakdown_json:
        extraction_status = "complete"
    else:
        extraction_status = "pending"
    full_name = " ".join(p for p in (candidate.first_name, candidate.last_name) if p) or None
    score = application.deterministic_match_score
    return PipelineCard(
        application_id=application.id,
        job_id=application.job_id,
        stage_id=application.current_stage_id,
        status=application.status,
        extraction_status=extraction_status,
        deterministic_match_score=float(score) if score is not None else None,
        match=MatchChips(
            matched_required=_skill_names(breakdown.get("matched_required")),
            matched_preferred=_skill_names(breakdown.get("matched_preferred")),
            missing_required=_skill_names(breakdown.get("missing_required")),
        ),
        candidate=CardCandidate(
            id=candidate.id,
            full_name=full_name,
            email=candidate.email,
            location=candidate.location,
            linkedin_url=candidate.linkedin_url,
        ),
        applied_at=application.applied_at,
        stage_entered_at=application.stage_entered_at,
        version=application.version,
    )


class PipelineService:
    def __init__(
        self,
        session: AsyncSession,
        broker: EventBroker | None = None,
        actions: StageActionContext | None = None,
    ) -> None:
        self._session = session
        self._broker = broker
        self._actions = actions or StageActionContext()

    async def get_job(self, job_id: uuid.UUID) -> Job:
        job = await self._session.get(Job, job_id)
        if job is None:
            raise NotFoundError("Job not found")
        return job

    async def board(
        self, job_id: uuid.UUID, filters: PipelineFilters | None = None
    ) -> PipelineBoard:
        filters = filters or PipelineFilters()
        job = await self.get_job(job_id)
        stages = list(
            (
                await self._session.execute(
                    select(PipelineStage)
                    .where(PipelineStage.job_id == job_id)
                    .order_by(PipelineStage.position_index)
                )
            ).scalars()
        )
        rows = (
            await self._session.execute(
                select(JobApplication, Candidate)
                .join(Candidate, Candidate.id == JobApplication.candidate_id)
                .where(JobApplication.job_id == job_id)
                .order_by(
                    JobApplication.deterministic_match_score.desc().nulls_last(),
                    JobApplication.applied_at,
                )
            )
        ).all()

        applied_stage = next((s for s in stages if s.stage_type == APPLIED_STAGE), None)
        cards_by_stage: dict[uuid.UUID, list[PipelineCard]] = {stage.id: [] for stage in stages}
        for application, candidate in rows:
            stage_id = application.current_stage_id
            if stage_id not in cards_by_stage and applied_stage is not None:
                stage_id = applied_stage.id  # defensive: unassigned cards surface in Applied
            if stage_id in cards_by_stage:
                cards_by_stage[stage_id].append(build_card(application, candidate))

        columns = []
        for stage in stages:
            cards = cards_by_stage[stage.id]
            visible = [card for card in cards if matches_filters(card, filters)]
            columns.append(
                PipelineColumn(
                    id=stage.id,
                    name=stage.name,
                    stage_type=stage.stage_type,
                    position_index=stage.position_index,
                    total_count=len(cards),
                    cards=sort_cards(visible, stage.stage_type, filters.sort),
                )
            )
        taxonomy = JobTaxonomy.model_validate(job.taxonomy_json)
        return PipelineBoard(
            job=PipelineJob(
                id=job.id,
                title=job.title,
                slug=job.slug,
                status=job.status,
                required_skills=[skill.name for skill in taxonomy.required_skills],
            ),
            filters=filters,
            stages=columns,
        )

    async def card(self, application_id: uuid.UUID) -> PipelineCard:
        row = (
            await self._session.execute(
                select(JobApplication, Candidate)
                .join(Candidate, Candidate.id == JobApplication.candidate_id)
                .where(JobApplication.id == application_id)
                .execution_options(populate_existing=True)
            )
        ).one_or_none()
        if row is None:
            raise NotFoundError("Application not found")
        return build_card(*row)

    async def move(
        self,
        application_id: uuid.UUID,
        request: MoveStageRequest,
        *,
        actor: User,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> PipelineCard:
        application = await self._session.get(JobApplication, application_id)
        if application is None:
            raise NotFoundError("Application not found")
        job = await self.get_job(application.job_id)
        if job.status is JobStatus.ARCHIVED:
            raise UnprocessableError("This job is archived; its pipeline is read-only")
        target = await self._session.get(PipelineStage, request.to_stage_id)
        if target is None or target.job_id != application.job_id:
            raise UnprocessableError("The target stage does not belong to this job's pipeline")
        if (
            actor.role is UserRole.HIRING_MANAGER
            and target.stage_type not in HIRING_MANAGER_TARGET_STAGES
        ):
            raise PermissionDeniedError(
                "Hiring managers can move candidates only to Interview, Offer or Rejected"
            )
        if application.version != request.expected_version:
            raise ConflictError(
                f"This card changed since you loaded it (version {application.version}); "
                "refresh and try again"
            )
        if application.current_stage_id == target.id:
            return await self.card(application_id)

        source = (
            await self._session.get(PipelineStage, application.current_stage_id)
            if application.current_stage_id
            else None
        )
        from_status = application.status
        to_status = _status_after_move(target.stage_type, from_status)
        new_version = (
            await self._session.execute(
                update(JobApplication)
                .where(
                    JobApplication.id == application_id,
                    JobApplication.version == request.expected_version,
                )
                .values(
                    current_stage_id=target.id,
                    status=to_status,
                    version=JobApplication.version + 1,
                    stage_entered_at=datetime.now(UTC),
                )
                .returning(JobApplication.version)
                .execution_options(synchronize_session=False)
            )
        ).scalar_one_or_none()
        if new_version is None:  # lost a race with a concurrent move
            await self._session.rollback()
            raise ConflictError("This card was moved by someone else; refresh and try again")

        if target.stage_type != "rejected":  # never send a rejection after a reversed decision
            await CommunicationService(self._session).cancel_queued(
                application.id, {"rejection"}, "Candidate moved out of Rejected"
            )
        actions = await self._run_stage_actions(application, target)
        self._session.add(
            AuditLog(
                tenant_id=application.tenant_id,
                actor_id=actor.id,
                action="stage_changed",
                entity_type="job_application",
                entity_id=application.id,
                metadata_json={
                    "job_id": str(application.job_id),
                    "from_stage_id": str(source.id) if source else None,
                    "from_stage": source.name if source else None,
                    "to_stage_id": str(target.id),
                    "to_stage": target.name,
                    "from_status": from_status.value,
                    "to_status": to_status.value,
                    "version": new_version,
                    "stage_actions": actions,
                },
                ip_address=ip_address,
                user_agent=user_agent[:500] if user_agent else None,
            )
        )
        await self._session.commit()

        card = await self.card(application_id)
        await self.publish(card, "card_moved")
        return card

    async def _run_stage_actions(
        self, application: JobApplication, stage: PipelineStage
    ) -> list[dict[str, str]]:
        results = []
        for action in stage.automated_actions_json.get("on_enter", []):
            handler = STAGE_ACTION_HANDLERS.get(action)
            if handler is None:
                results.append({"action": action, "status": "unknown"})
                continue
            status = await handler(self._session, application, stage, self._actions)
            results.append({"action": action, "status": status})
        return results

    async def publish(self, card: PipelineCard, event_type: str) -> None:
        if self._broker is None:
            return
        tenant_id = (await self.get_job(card.job_id)).tenant_id
        await publish_safely(
            self._broker,
            pipeline_channel(tenant_id, card.job_id),
            {"type": event_type, "card": card.model_dump(mode="json")},
        )
