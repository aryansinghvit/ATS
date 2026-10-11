"""Reusable FastAPI dependencies: authentication, tenant binding and role-based access control."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Coroutine, Sequence
from typing import Annotated, Any

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db, get_session, set_tenant_context
from app.core.exceptions import NotFoundError, PermissionDeniedError
from app.core.security import AuthClaims, AuthenticationError, TokenVerifier, get_token_verifier
from app.core.tenancy import parse_tenant_host
from app.models.user import User, UserRole
from app.repositories.tenant_repository import TenantRepository
from app.repositories.user_repository import UserRepository

bearer_scheme = HTTPBearer(auto_error=False, description="Clerk session token (JWT)")


async def get_auth_claims(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    verifier: Annotated[TokenVerifier, Depends(get_token_verifier)],
) -> AuthClaims:
    if credentials is None:
        raise AuthenticationError("Missing bearer token")
    return await verifier.verify(credentials.credentials)


async def get_host_tenant_id(
    request: Request, session: Annotated[AsyncSession, Depends(get_session)]
) -> uuid.UUID | None:
    """Tenant implied by the Host header (tenant subdomain or custom domain), if any."""
    settings = get_settings()
    key = parse_tenant_host(
        request.headers.get("host"),
        root_domain=settings.PLATFORM_ROOT_DOMAIN,
        reserved_subdomains=settings.RESERVED_SUBDOMAINS,
    )
    if key is None:
        return None
    return await TenantRepository(session).resolve_id_by_host(key)


async def get_current_user(
    request: Request,
    claims: Annotated[AuthClaims, Depends(get_auth_claims)],
    session: Annotated[AsyncSession, Depends(get_session)],
    host_tenant_id: Annotated[uuid.UUID | None, Depends(get_host_tenant_id)],
) -> User:
    """Validate the JWT, bind its tenant to the DB session (RLS) and return the active user."""
    tenant_id = await TenantRepository(session).resolve_id_by_clerk_org(claims.org_id)
    if tenant_id is None:
        raise PermissionDeniedError(
            "Workspace is not provisioned; call POST /api/v1/auth/sync-user first"
        )
    if host_tenant_id is not None and host_tenant_id != tenant_id:
        raise PermissionDeniedError("Token does not belong to this workspace")

    request.state.tenant_id = tenant_id
    await set_tenant_context(session, tenant_id)

    user = await UserRepository(session).get_by_clerk_id(claims.user_id)
    if user is None:
        raise PermissionDeniedError(
            "User is not provisioned in this workspace; call POST /api/v1/auth/sync-user first"
        )
    if not user.is_active:
        raise PermissionDeniedError("User account is deactivated")
    if user.role != claims.role:
        user.role = claims.role  # Clerk is the source of truth for organization roles
        await session.commit()
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


async def get_tenant_db(
    current_user: CurrentUser, db: Annotated[AsyncSession, Depends(get_db)]
) -> AsyncSession:
    """Session bound to the authenticated user's tenant (resolution happens before ``get_db``)."""
    return db


TenantDB = Annotated[AsyncSession, Depends(get_tenant_db)]


async def require_host_tenant(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    host_tenant_id: Annotated[uuid.UUID | None, Depends(get_host_tenant_id)],
) -> uuid.UUID:
    """For public (no-login) routes: bind the tenant identified by the Host header."""
    if host_tenant_id is None:
        raise NotFoundError("No workspace is associated with this host")
    request.state.tenant_id = host_tenant_id
    await set_tenant_context(session, host_tenant_id)
    return host_tenant_id


HostTenantId = Annotated[uuid.UUID, Depends(require_host_tenant)]


async def get_public_tenant_db(
    tenant_id: HostTenantId, db: Annotated[AsyncSession, Depends(get_db)]
) -> AsyncSession:
    """Session bound to the Host-header tenant, for unauthenticated routes."""
    return db


PublicTenantDB = Annotated[AsyncSession, Depends(get_public_tenant_db)]


async def bind_tenant_by_subdomain(
    request: Request, session: AsyncSession, subdomain: str
) -> uuid.UUID:
    """For public routes addressed as /careers/{subdomain}/...: bind that tenant or 404."""
    tenant_id = await TenantRepository(session).resolve_id_by_subdomain(subdomain)
    if tenant_id is None:
        raise NotFoundError("Career site not found")
    request.state.tenant_id = tenant_id
    await set_tenant_context(session, tenant_id)
    return tenant_id


async def require_path_tenant(
    request: Request,
    tenant_subdomain: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> uuid.UUID:
    return await bind_tenant_by_subdomain(request, session, tenant_subdomain)


PathTenantId = Annotated[uuid.UUID, Depends(require_path_tenant)]


def require_role(
    allowed_roles: Sequence[UserRole],
) -> Callable[..., Coroutine[Any, Any, User]]:
    """Dependency factory: 403 unless the current user's role is in ``allowed_roles``."""
    allowed = frozenset(allowed_roles)
    allowed_names = ", ".join(sorted(role.value for role in allowed))

    async def _require_role(current_user: CurrentUser) -> User:
        if current_user.role not in allowed:
            raise PermissionDeniedError(f"This action requires one of the roles: {allowed_names}")
        return current_user

    return _require_role
