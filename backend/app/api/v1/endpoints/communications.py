"""Candidate email templates, communication settings and per-application email log."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import TenantDB, require_role
from app.core.exceptions import NotFoundError
from app.models.candidate import JobApplication
from app.models.user import User, UserRole
from app.schemas.communications import (
    CommunicationSettings,
    OutboxEmailRead,
    TemplatePreview,
    TemplatePreviewRequest,
    TemplateRead,
    TemplateUpdate,
)
from app.services.communications import CommunicationService

router = APIRouter(tags=["communications"])

Editor = Annotated[User, Depends(require_role([UserRole.ADMIN, UserRole.RECRUITER]))]
Reviewer = Annotated[
    User, Depends(require_role([UserRole.ADMIN, UserRole.RECRUITER, UserRole.HIRING_MANAGER]))
]


@router.get("/communications/templates", response_model=list[TemplateRead])
async def list_templates(user: Editor, db: TenantDB) -> list[TemplateRead]:
    return await CommunicationService(db).templates()


@router.put("/communications/templates/{key}", response_model=TemplateRead)
async def update_template(
    key: str, payload: TemplateUpdate, user: Editor, db: TenantDB
) -> TemplateRead:
    """Customise a template; only the documented ``{{variables}}`` are accepted."""
    return await CommunicationService(db).update_template(
        key, payload, tenant_id=user.tenant_id, actor=user
    )


@router.delete("/communications/templates/{key}", response_model=TemplateRead)
async def reset_template(key: str, user: Editor, db: TenantDB) -> TemplateRead:
    """Restore the built-in default."""
    return await CommunicationService(db).reset_template(key)


@router.post("/communications/templates/{key}/preview", response_model=TemplatePreview)
async def preview_template(
    key: str, payload: TemplatePreviewRequest, user: Editor, db: TenantDB
) -> TemplatePreview:
    return await CommunicationService(db).preview(
        key,
        subject=payload.subject,
        body=payload.body,
        application_id=payload.application_id,
        tenant_id=user.tenant_id,
    )


@router.get("/communications/settings", response_model=CommunicationSettings)
async def get_communication_settings(user: Editor, db: TenantDB) -> CommunicationSettings:
    return await CommunicationService(db).get_settings(user.tenant_id)


@router.put("/communications/settings", response_model=CommunicationSettings)
async def update_communication_settings(
    payload: CommunicationSettings, user: Editor, db: TenantDB
) -> CommunicationSettings:
    """Scheduling link ({{scheduling_link}}), polite rejection delay (24–72 h) and timezone."""
    return await CommunicationService(db).update_settings(user.tenant_id, payload)


@router.get("/applications/{application_id}/emails", response_model=list[OutboxEmailRead])
async def application_emails(
    application_id: uuid.UUID, user: Reviewer, db: TenantDB
) -> list[OutboxEmailRead]:
    if await db.get(JobApplication, application_id) is None:  # RLS hides other tenants
        raise NotFoundError("Application not found")
    emails = await CommunicationService(db).outbox_for(application_id)
    return [OutboxEmailRead.model_validate(email) for email in emails]
