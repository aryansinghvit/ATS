from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.candidate import Candidate, JobApplication


class CandidateRepository:
    """Candidates and their job applications. Tenant scoping is enforced by Row-Level Security."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, candidate_id: uuid.UUID) -> Candidate | None:
        return await self._session.get(Candidate, candidate_id)

    async def get_by_email(self, email: str) -> Candidate | None:
        result = await self._session.execute(
            select(Candidate).where(func.lower(Candidate.email) == email.lower())
        )
        return result.scalar_one_or_none()

    async def get_application(self, application_id: uuid.UUID) -> JobApplication | None:
        return await self._session.get(JobApplication, application_id)

    async def application_exists(self, job_id: uuid.UUID, candidate_id: uuid.UUID) -> bool:
        result = await self._session.execute(
            select(JobApplication.id)
            .where(JobApplication.job_id == job_id, JobApplication.candidate_id == candidate_id)
            .limit(1)
        )
        return result.first() is not None

    async def resume_key_in_use(self, s3_key: str) -> bool:
        result = await self._session.execute(
            select(JobApplication.id).where(JobApplication.resume_s3_key == s3_key).limit(1)
        )
        return result.first() is not None

    def add(self, entity: Candidate | JobApplication) -> None:
        self._session.add(entity)
