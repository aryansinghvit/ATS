"""Workspace & billing management — Admin / Founder only (project.md Module 1 RBAC matrix)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import TenantDB, require_role
from app.models.user import User, UserRole
from app.schemas.tenant import BillingRead, TenantSettingsRead, TenantSettingsUpdate
from app.services.workspace_service import WorkspaceService

router = APIRouter(prefix="/workspace", tags=["workspace"])

AdminUser = Annotated[User, Depends(require_role([UserRole.ADMIN]))]


@router.get("/settings", response_model=TenantSettingsRead)
async def read_workspace_settings(admin: AdminUser, db: TenantDB) -> TenantSettingsRead:
    tenant = await WorkspaceService(db).get_tenant(admin.tenant_id)
    return TenantSettingsRead.model_validate(tenant)


@router.patch("/settings", response_model=TenantSettingsRead)
async def update_workspace_settings(
    update: TenantSettingsUpdate, admin: AdminUser, db: TenantDB
) -> TenantSettingsRead:
    tenant = await WorkspaceService(db).update_settings(admin.tenant_id, update)
    return TenantSettingsRead.model_validate(tenant)


@router.get("/billing", response_model=BillingRead)
async def read_billing(admin: AdminUser, db: TenantDB) -> BillingRead:
    return await WorkspaceService(db).billing_summary(admin.tenant_id)
