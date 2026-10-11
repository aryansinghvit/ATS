"""In-app notifications (mentions, sourcing alerts). Rows are tenant-isolated by RLS and always
read through ``user_id = caller`` so a user only ever sees their own."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.models.collaboration import Notification
from app.schemas.collaboration import NotificationList, NotificationRead


def notify(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_ids: Iterable[uuid.UUID],
    type: str,
    title: str,
    body: str | None = None,
    link: str | None = None,
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
) -> int:
    """Stage notifications in the caller's transaction (committed with the triggering change)."""
    count = 0
    for user_id in dict.fromkeys(user_ids):
        session.add(
            Notification(
                tenant_id=tenant_id,
                user_id=user_id,
                type=type,
                title=title[:300],
                body=body,
                link=link,
                entity_type=entity_type,
                entity_id=entity_id,
            )
        )
        count += 1
    return count


class NotificationService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list(self, user_id: uuid.UUID, *, unread_only: bool, limit: int) -> NotificationList:
        query = select(Notification).where(Notification.user_id == user_id)
        if unread_only:
            query = query.where(Notification.read_at.is_(None))
        rows = (
            await self._session.execute(
                query.order_by(
                    Notification.read_at.is_not(None), Notification.created_at.desc()
                ).limit(limit)
            )
        ).scalars()
        unread = await self._session.scalar(
            select(func.count())
            .select_from(Notification)
            .where(Notification.user_id == user_id, Notification.read_at.is_(None))
        )
        return NotificationList(
            unread=unread or 0, items=[NotificationRead.model_validate(n) for n in rows]
        )

    async def mark_read(self, user_id: uuid.UUID, notification_id: uuid.UUID | None) -> int:
        statement = (
            update(Notification)
            .where(Notification.user_id == user_id, Notification.read_at.is_(None))
            .values(read_at=datetime.now(UTC))
        )
        if notification_id is not None:
            statement = statement.where(Notification.id == notification_id)
        result = await self._session.execute(statement)
        await self._session.commit()
        if notification_id is not None and result.rowcount == 0:
            exists = await self._session.scalar(
                select(Notification.id).where(
                    Notification.id == notification_id, Notification.user_id == user_id
                )
            )
            if exists is None:
                raise NotFoundError("Notification not found")
        return result.rowcount
