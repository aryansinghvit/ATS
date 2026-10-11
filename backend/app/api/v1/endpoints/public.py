"""Unauthenticated endpoints. The tenant is resolved from the Host header (subdomain/domain)."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import HostTenantId, PublicTenantDB
from app.schemas.tenant import TenantBranding
from app.services.workspace_service import WorkspaceService

router = APIRouter(prefix="/public", tags=["public"])


@router.get("/tenant", response_model=TenantBranding)
async def read_public_tenant(tenant_id: HostTenantId, db: PublicTenantDB) -> TenantBranding:
    """Branding for career pages served on ``<subdomain>.<root-domain>`` or a custom domain."""
    tenant = await WorkspaceService(db).get_tenant(tenant_id)
    return TenantBranding.model_validate(tenant)
