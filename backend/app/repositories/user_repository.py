from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User


class UserRepository:
    """User data access. Tenant scoping is enforced by Row-Level Security, not by these queries."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, user_id: uuid.UUID) -> User | None:
        return await self._session.get(User, user_id)

    async def get_by_clerk_id(self, clerk_user_id: str) -> User | None:
        result = await self._session.execute(
            select(User).where(User.clerk_user_id == clerk_user_id)
        )
        return result.scalar_one_or_none()

    async def get_by_email(self, email: str) -> User | None:
        result = await self._session.execute(
            select(User).where(func.lower(User.email) == email.lower())
        )
        return result.scalar_one_or_none()

    async def list(self, *, include_inactive: bool = False) -> list[User]:
        query = select(User).order_by(User.created_at, User.email)
        if not include_inactive:
            query = query.where(User.is_active.is_(True))
        return list((await self._session.execute(query)).scalars())

    async def count_active(self) -> int:
        result = await self._session.execute(
            select(func.count()).select_from(User).where(User.is_active.is_(True))
        )
        return result.scalar_one()

    def add(self, user: User) -> None:
        self._session.add(user)
