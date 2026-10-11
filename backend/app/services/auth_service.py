"""Synchronises Clerk identities (organization + user) into PostgreSQL tenants and users."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import set_tenant_context
from app.core.exceptions import ConflictError, PermissionDeniedError, UnprocessableError
from app.core.security import AuthClaims
from app.core.tenancy import normalize_subdomain
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.repositories.tenant_repository import TenantRepository
from app.repositories.user_repository import UserRepository
from app.schemas.auth import SyncUserRequest


@dataclass(frozen=True, slots=True)
class SyncResult:
    tenant: Tenant
    user: User
    tenant_created: bool
    user_created: bool


class AuthService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._tenants = TenantRepository(session)
        self._users = UserRepository(session)

    async def sync_user(
        self,
        claims: AuthClaims,
        payload: SyncUserRequest,
        *,
        host_tenant_id: uuid.UUID | None = None,
    ) -> SyncResult:
        tenant_id = await self._tenants.resolve_id_by_clerk_org(claims.org_id)
        if tenant_id is None:
            if host_tenant_id is not None:
                raise PermissionDeniedError(
                    "New workspaces must be provisioned from the platform domain"
                )
            tenant, tenant_created = await self._provision_tenant(claims, payload)
        else:
            if host_tenant_id is not None and host_tenant_id != tenant_id:
                raise PermissionDeniedError("Token does not belong to this workspace")
            await set_tenant_context(self._session, tenant_id)
            tenant = await self._tenants.get(tenant_id)
            tenant_created = False
        if tenant is None:  # pragma: no cover - resolved ids are always visible to their tenant
            raise ConflictError("Workspace could not be loaded; retry")

        user, user_created = await self._upsert_user(tenant, claims, payload)
        try:
            await self._session.commit()
        except IntegrityError:
            await self._session.rollback()
            raise ConflictError("Concurrent update to this account; retry the request") from None
        return SyncResult(tenant, user, tenant_created, user_created)

    async def _provision_tenant(
        self, claims: AuthClaims, payload: SyncUserRequest
    ) -> tuple[Tenant, bool]:
        if claims.role is not UserRole.ADMIN:
            raise PermissionDeniedError("Only an organization admin can provision a new workspace")
        if payload.organization is None:
            raise UnprocessableError("'organization' is required to provision a new workspace")

        org = payload.organization
        try:
            subdomain = normalize_subdomain(
                org.subdomain or claims.org_slug or org.name,
                reserved_subdomains=get_settings().RESERVED_SUBDOMAINS,
            )
        except ValueError as exc:
            raise UnprocessableError(str(exc)) from None
        if await self._tenants.resolve_id_by_subdomain(subdomain) is not None:
            raise ConflictError(f"Subdomain '{subdomain}' is already taken")

        # RLS WITH CHECK on tenants requires the new row's id to be the bound tenant.
        tenant = Tenant(
            id=uuid.uuid4(), clerk_org_id=claims.org_id, name=org.name, subdomain=subdomain
        )
        await set_tenant_context(self._session, tenant.id)
        self._tenants.add(tenant)
        try:
            await self._session.flush()
        except IntegrityError:
            # Lost a race: the org was provisioned (or the subdomain taken) concurrently.
            await self._session.rollback()
            existing_id = await self._tenants.resolve_id_by_clerk_org(claims.org_id)
            if existing_id is None:
                raise ConflictError(f"Subdomain '{subdomain}' is already taken") from None
            await set_tenant_context(self._session, existing_id)
            existing = await self._tenants.get(existing_id)
            if existing is None:  # pragma: no cover
                raise ConflictError("Workspace could not be loaded; retry") from None
            return existing, False
        return tenant, True

    async def _upsert_user(
        self, tenant: Tenant, claims: AuthClaims, payload: SyncUserRequest
    ) -> tuple[User, bool]:
        email = str(claims.email or payload.email).lower()
        avatar_url = str(payload.avatar_url) if payload.avatar_url else None

        user = await self._users.get_by_clerk_id(claims.user_id)
        clash = await self._users.get_by_email(email)
        if clash is not None and (user is None or clash.id != user.id):
            raise ConflictError("This email is already registered to another workspace member")

        if user is None:
            user = User(
                tenant_id=tenant.id,
                clerk_user_id=claims.user_id,
                email=email,
                full_name=payload.full_name,
                role=claims.role,
                department=payload.department,
                avatar_url=avatar_url,
            )
            self._users.add(user)
            await self._session.flush()
            return user, True

        if not user.is_active:
            raise PermissionDeniedError("User account is deactivated")
        user.email = email
        user.full_name = payload.full_name
        user.role = claims.role  # Clerk is the source of truth for organization roles
        if payload.department is not None:
            user.department = payload.department
        if avatar_url is not None:
            user.avatar_url = avatar_url
        await self._session.flush()
        return user, False
