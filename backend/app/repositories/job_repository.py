from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.job import Job, JobStatus, PipelineStage


class JobRepository:
    """Jobs and their pipeline stages. Tenant scoping is enforced by Row-Level Security."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, job_id: uuid.UUID) -> Job | None:
        return await self._session.get(Job, job_id)

    async def get_by_slug(self, slug: str) -> Job | None:
        result = await self._session.execute(select(Job).where(Job.slug == slug))
        return result.scalar_one_or_none()

    async def slug_exists(self, slug: str, *, exclude_id: uuid.UUID | None = None) -> bool:
        query = select(Job.id).where(Job.slug == slug)
        if exclude_id is not None:
            query = query.where(Job.id != exclude_id)
        return (await self._session.execute(query.limit(1))).first() is not None

    async def list(self, *, statuses: Iterable[JobStatus] | None = None) -> list[Job]:
        query = select(Job).order_by(Job.created_at.desc(), Job.title)
        if statuses is not None:
            query = query.where(Job.status.in_(list(statuses)))
        return list((await self._session.execute(query)).scalars())

    async def list_published(self) -> list[Job]:
        query = (
            select(Job)
            .where(Job.status == JobStatus.PUBLISHED)
            .order_by(Job.published_at.desc().nulls_last(), Job.title)
        )
        return list((await self._session.execute(query)).scalars())

    async def get_stage_by_type(self, job_id: uuid.UUID, stage_type: str) -> PipelineStage | None:
        result = await self._session.execute(
            select(PipelineStage)
            .where(PipelineStage.job_id == job_id, PipelineStage.stage_type == stage_type)
            .order_by(PipelineStage.position_index)
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def list_stages(self, job_id: uuid.UUID) -> list[PipelineStage]:
        result = await self._session.execute(
            select(PipelineStage)
            .where(PipelineStage.job_id == job_id)
            .order_by(PipelineStage.position_index)
        )
        return list(result.scalars())

    def add(self, job: Job) -> None:
        self._session.add(job)

    def add_stages(self, stages: Iterable[PipelineStage]) -> None:
        self._session.add_all(list(stages))
