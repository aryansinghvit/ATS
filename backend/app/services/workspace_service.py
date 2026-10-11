"""Admin-only workspace settings and billing summary for the current tenant."""

from __future__ import annotations

import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError
from app.models.tenant import Tenant
from app.repositories.tenant_repository import TenantRepository
from app.repositories.user_repository import UserRepository
from app.schemas.tenant import BillingRead, TenantSettingsUpdate


class WorkspaceService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._tenants = TenantRepository(session)
        self._users = UserRepository(session)

    async def get_tenant(self, tenant_id: uuid.UUID) -> Tenant:
        tenant = await self._tenants.get(tenant_id)
        if tenant is None:
            raise NotFoundError("Workspace not found")
        return tenant

    async def update_settings(self, tenant_id: uuid.UUID, update: TenantSettingsUpdate) -> Tenant:
        tenant = await self.get_tenant(tenant_id)
        changes = update.model_dump(exclude_unset=True)

        if "custom_domain" in changes and changes["custom_domain"] != tenant.custom_domain:
            domain = changes["custom_domain"]
            if domain is not None:
                owner = await self._tenants.resolve_id_by_custom_domain(domain)
                if owner is not None and owner != tenant.id:
                    raise ConflictError(f"Domain '{domain}' is already in use")
            tenant.custom_domain = domain
        if "name" in changes and changes["name"] is not None:
            tenant.name = changes["name"]
        if "logo_url" in changes:
            tenant.logo_url = str(changes["logo_url"]) if changes["logo_url"] else None
        if "primary_color" in changes and changes["primary_color"] is not None:
            tenant.primary_color = changes["primary_color"].upper()
        if changes.get("settings"):
            tenant.settings_json = {**tenant.settings_json, **changes["settings"]}

        try:
            await self._session.commit()
        except IntegrityError:
            await self._session.rollback()
            raise ConflictError("Workspace settings conflict with another workspace") from None
        return tenant

    async def billing_summary(self, tenant_id: uuid.UUID) -> BillingRead:
        tenant = await self.get_tenant(tenant_id)
        billing = tenant.settings_json.get("billing", {})
        return BillingRead(
            plan=billing.get("plan", "trial"),
            status=billing.get("status", "active"),
            seats_used=await self._users.count_active(),
        )
