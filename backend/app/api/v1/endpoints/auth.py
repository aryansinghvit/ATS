from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, TenantDB, get_auth_claims, get_host_tenant_id
from app.core.database import get_session
from app.core.security import AuthClaims
from app.schemas.auth import MeResponse, SyncUserRequest, SyncUserResponse
from app.schemas.tenant import TenantRead
from app.schemas.user import UserRead
from app.services.auth_service import AuthService
from app.services.workspace_service import WorkspaceService

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/sync-user",
    response_model=SyncUserResponse,
    responses={
        status.HTTP_201_CREATED: {"description": "User (and possibly workspace) created"},
        status.HTTP_409_CONFLICT: {"description": "Subdomain or email already taken"},
    },
)
async def sync_user(
    payload: SyncUserRequest,
    response: Response,
    claims: Annotated[AuthClaims, Depends(get_auth_claims)],
    session: Annotated[AsyncSession, Depends(get_session)],
    host_tenant_id: Annotated[uuid.UUID | None, Depends(get_host_tenant_id)],
) -> SyncUserResponse:
    """Mirror the Clerk organization and user from the bearer token into PostgreSQL.

    The first call for a new Clerk organization (by an org admin) provisions the workspace.
    Subsequent calls are idempotent and refresh the user's profile and role.
    """
    result = await AuthService(session).sync_user(claims, payload, host_tenant_id=host_tenant_id)
    response.status_code = status.HTTP_201_CREATED if result.user_created else status.HTTP_200_OK
    return SyncUserResponse(
        tenant=TenantRead.model_validate(result.tenant),
        user=UserRead.model_validate(result.user),
        tenant_created=result.tenant_created,
        user_created=result.user_created,
    )


@router.get("/me", response_model=MeResponse)
async def read_me(current_user: CurrentUser, db: TenantDB) -> MeResponse:
    tenant = await WorkspaceService(db).get_tenant(current_user.tenant_id)
    return MeResponse(
        user=UserRead.model_validate(current_user), tenant=TenantRead.model_validate(tenant)
    )
