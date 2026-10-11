"""Interview scorecards, the team notes feed and in-app notifications (project.md Module 7)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import CurrentUser, TenantDB, require_role
from app.models.user import User, UserRole
from app.schemas.collaboration import (
    NoteCreate,
    NoteRead,
    NotificationList,
    ScorecardBoard,
    ScorecardRead,
    ScorecardUpsert,
)
from app.services.collaboration_service import CollaborationService
from app.services.notification_service import NotificationService

router = APIRouter(tags=["collaboration"])

# Notes are team discussion; blind interviewers are kept out to protect independent scoring.
TeamMember = Annotated[
    User, Depends(require_role([UserRole.ADMIN, UserRole.RECRUITER, UserRole.HIRING_MANAGER]))
]


@router.get("/applications/{application_id}/scorecards", response_model=ScorecardBoard)
async def list_scorecards(
    application_id: uuid.UUID, user: CurrentUser, db: TenantDB
) -> ScorecardBoard:
    """The caller's scorecard plus the colleagues' scorecards their role may see."""
    return await CollaborationService(db).scorecards(application_id, viewer=user)


@router.put("/applications/{application_id}/scorecards/me", response_model=ScorecardRead)
async def save_my_scorecard(
    application_id: uuid.UUID, payload: ScorecardUpsert, user: CurrentUser, db: TenantDB
) -> ScorecardRead:
    """Save a draft, or submit (``submit: true``) to lock it."""
    return await CollaborationService(db).save_scorecard(application_id, payload, evaluator=user)


@router.get("/applications/{application_id}/notes", response_model=list[NoteRead])
async def list_notes(application_id: uuid.UUID, user: TeamMember, db: TenantDB) -> list[NoteRead]:
    return await CollaborationService(db).notes(application_id)


@router.post(
    "/applications/{application_id}/notes",
    response_model=NoteRead,
    status_code=status.HTTP_201_CREATED,
)
async def add_note(
    application_id: uuid.UUID, payload: NoteCreate, user: TeamMember, db: TenantDB
) -> NoteRead:
    """Add a note; ``@[Name](user-id)`` mentions notify those teammates."""
    return await CollaborationService(db).add_note(application_id, payload, author=user)


@router.get("/notifications", response_model=NotificationList)
async def list_notifications(
    user: CurrentUser,
    db: TenantDB,
    unread_only: bool = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> NotificationList:
    return await NotificationService(db).list(user.id, unread_only=unread_only, limit=limit)


@router.post("/notifications/{notification_id}/read")
async def mark_notification_read(
    notification_id: uuid.UUID, user: CurrentUser, db: TenantDB
) -> dict[str, int]:
    return {"updated": await NotificationService(db).mark_read(user.id, notification_id)}


@router.post("/notifications/read-all")
async def mark_all_notifications_read(user: CurrentUser, db: TenantDB) -> dict[str, int]:
    return {"updated": await NotificationService(db).mark_read(user.id, None)}
