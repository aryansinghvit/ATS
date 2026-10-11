from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import HostTenantKey
from app.models.tenant import Tenant


class TenantRepository:
    """Tenant data access.

    ``resolve_*`` call SECURITY DEFINER functions and work before a tenant is bound; every other
    query is filtered by Row-Level Security to the session's current tenant.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _resolve(self, function_name: str, key: str) -> uuid.UUID | None:
        function = getattr(func, function_name)
        return (await self._session.execute(select(function(key)))).scalar_one_or_none()

    async def resolve_id_by_clerk_org(self, clerk_org_id: str) -> uuid.UUID | None:
        return await self._resolve("app_resolve_tenant_by_clerk_org", clerk_org_id)

    async def resolve_id_by_subdomain(self, subdomain: str) -> uuid.UUID | None:
        return await self._resolve("app_resolve_tenant_by_subdomain", subdomain)

    async def resolve_id_by_custom_domain(self, domain: str) -> uuid.UUID | None:
        return await self._resolve("app_resolve_tenant_by_custom_domain", domain)

    async def resolve_id_by_host(self, key: HostTenantKey) -> uuid.UUID | None:
        if key.kind == "subdomain":
            return await self.resolve_id_by_subdomain(key.value)
        return await self.resolve_id_by_custom_domain(key.value)

    async def get(self, tenant_id: uuid.UUID) -> Tenant | None:
        return await self._session.get(Tenant, tenant_id)

    def add(self, tenant: Tenant) -> None:
        self._session.add(tenant)
