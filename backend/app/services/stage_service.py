"""Per-job pipeline stage editor (task.md Prompt 7.3): add, rename, retype, reorder, delete."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError, UnprocessableError
from app.models.audit import AuditLog
from app.models.candidate import JobApplication
from app.models.job import Job, PipelineStage
from app.models.user import User
from app.schemas.admin import StageActionInfo, StageCreate, StageRead, StageReorder, StageUpdate
from app.services.stage_actions import STAGE_ACTION_HANDLERS

ACTION_DESCRIPTIONS = {
    "ai_pre_screen": "Generate AI screening questions and invite the candidate to the portal",
    "interview_invitation_email": "Email an interview invitation with the scheduling link",
    "offer_email": "Email the candidate that an offer is coming",
    "polite_rejection_email": "Schedule the polite, delayed rejection email",
}
TERMINAL_TYPES = ("hired", "rejected")


class StageService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @staticmethod
    def actions() -> list[StageActionInfo]:
        return [
            StageActionInfo(key=key, description=ACTION_DESCRIPTIONS.get(key, key))
            for key in STAGE_ACTION_HANDLERS
        ]

    async def _job(self, job_id: uuid.UUID) -> Job:
        job = await self._session.get(Job, job_id)
        if job is None:
            raise NotFoundError("Job not found")
        return job

    async def _stages(self, job_id: uuid.UUID) -> list[PipelineStage]:
        return list(
            (
                await self._session.execute(
                    select(PipelineStage)
                    .where(PipelineStage.job_id == job_id)
                    .order_by(PipelineStage.position_index, PipelineStage.created_at)
                )
            ).scalars()
        )

    async def list(self, job_id: uuid.UUID) -> list[StageRead]:
        await self._job(job_id)
        counts = dict(
            (
                await self._session.execute(
                    select(JobApplication.current_stage_id, func.count())
                    .where(JobApplication.job_id == job_id)
                    .group_by(JobApplication.current_stage_id)
                )
            ).all()
        )
        return [
            StageRead(
                id=stage.id,
                name=stage.name,
                stage_type=stage.stage_type,  # type: ignore[arg-type]
                position_index=stage.position_index,
                automated_actions=list(stage.automated_actions_json.get("on_enter", [])),
                applications=counts.get(stage.id, 0),
            )
            for stage in await self._stages(job_id)
        ]

    @staticmethod
    def _validate_actions(actions: list[str]) -> list[str]:
        unknown = sorted(set(actions) - set(STAGE_ACTION_HANDLERS))
        if unknown:
            raise UnprocessableError(f"Unknown stage actions: {', '.join(unknown)}")
        return list(dict.fromkeys(actions))

    @staticmethod
    def _renumber(stages: list[PipelineStage]) -> None:
        for index, stage in enumerate(stages):
            stage.position_index = index

    def _audit(self, actor: User, job: Job, action: str, **metadata: object) -> None:
        self._session.add(
            AuditLog(
                tenant_id=job.tenant_id,
                actor_id=actor.id,
                action=action,
                entity_type="job",
                entity_id=job.id,
                metadata_json=metadata,
            )
        )

    async def create(
        self, job_id: uuid.UUID, payload: StageCreate, *, actor: User
    ) -> list[StageRead]:
        job = await self._job(job_id)
        stages = await self._stages(job_id)
        if payload.stage_type == "applied":
            raise UnprocessableError("A pipeline has exactly one Applied stage")
        if any(s.name.lower() == payload.name.lower() for s in stages):
            raise ConflictError(f"A stage named '{payload.name}' already exists")
        stage = PipelineStage(
            id=uuid.uuid4(),
            tenant_id=job.tenant_id,
            job_id=job_id,
            name=payload.name,
            position_index=0,
            stage_type=payload.stage_type,
            automated_actions_json={"on_enter": self._validate_actions(payload.automated_actions)},
        )
        if payload.position_index is None:  # default: just before the terminal columns
            insert_at = next(
                (i for i, s in enumerate(stages) if s.stage_type in TERMINAL_TYPES), len(stages)
            )
        else:
            insert_at = max(1, min(payload.position_index, len(stages)))  # Applied stays first
        stages.insert(insert_at, stage)
        self._session.add(stage)
        self._renumber(stages)
        self._audit(actor, job, "stage_created", stage=payload.name, stage_type=payload.stage_type)
        await self._session.commit()
        return await self.list(job_id)

    async def update(
        self, job_id: uuid.UUID, stage_id: uuid.UUID, payload: StageUpdate, *, actor: User
    ) -> list[StageRead]:
        job = await self._job(job_id)
        stages = await self._stages(job_id)
        stage = next((s for s in stages if s.id == stage_id), None)
        if stage is None:
            raise NotFoundError("Stage not found")
        changes = payload.model_dump(exclude_unset=True)
        if changes.get("name") and changes["name"].lower() != stage.name.lower():
            if any(s.name.lower() == changes["name"].lower() for s in stages if s.id != stage_id):
                raise ConflictError(f"A stage named '{changes['name']}' already exists")
            stage.name = changes["name"]
        if changes.get("stage_type") and changes["stage_type"] != stage.stage_type:
            if stage.stage_type == "applied" or changes["stage_type"] == "applied":
                raise UnprocessableError("The Applied stage type cannot be changed or duplicated")
            stage.stage_type = changes["stage_type"]
        if payload.automated_actions is not None:
            stage.automated_actions_json = {
                "on_enter": self._validate_actions(payload.automated_actions)
            }
        self._audit(actor, job, "stage_updated", stage_id=str(stage_id), changes=sorted(changes))
        await self._session.commit()
        return await self.list(job_id)

    async def reorder(
        self, job_id: uuid.UUID, payload: StageReorder, *, actor: User
    ) -> list[StageRead]:
        job = await self._job(job_id)
        stages = {s.id: s for s in await self._stages(job_id)}
        if set(payload.stage_ids) != set(stages) or len(payload.stage_ids) != len(stages):
            raise UnprocessableError("Provide every stage of this job exactly once")
        ordered = [stages[i] for i in payload.stage_ids]
        if ordered[0].stage_type != "applied":
            raise UnprocessableError("The Applied stage must stay first")
        self._renumber(ordered)
        self._audit(actor, job, "stages_reordered", order=[s.name for s in ordered])
        await self._session.commit()
        return await self.list(job_id)

    async def delete(
        self, job_id: uuid.UUID, stage_id: uuid.UUID, *, actor: User
    ) -> list[StageRead]:
        job = await self._job(job_id)
        stages = await self._stages(job_id)
        stage = next((s for s in stages if s.id == stage_id), None)
        if stage is None:
            raise NotFoundError("Stage not found")
        if stage.stage_type == "applied":
            raise UnprocessableError("The Applied stage cannot be deleted")
        occupied = await self._session.scalar(
            select(func.count())
            .select_from(JobApplication)
            .where(JobApplication.current_stage_id == stage_id)
        )
        if occupied:
            raise ConflictError(
                f"Move the {occupied} candidate(s) out of '{stage.name}' before deleting it"
            )
        await self._session.delete(stage)
        self._renumber([s for s in stages if s.id != stage_id])
        self._audit(actor, job, "stage_deleted", stage=stage.name)
        await self._session.commit()
        return await self.list(job_id)
