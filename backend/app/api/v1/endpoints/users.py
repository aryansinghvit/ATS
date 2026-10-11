from __future__ import annotations

import uuid

from fastapi import APIRouter

from app.api.deps import CurrentUser, TenantDB
from app.core.exceptions import NotFoundError
from app.repositories.user_repository import UserRepository
from app.schemas.user import UserRead

router = APIRouter(prefix="/users", tags=["users"])


@router.get("", response_model=list[UserRead])
async def list_users(current_user: CurrentUser, db: TenantDB) -> list[UserRead]:
    """Team directory of the caller's workspace."""
    return [UserRead.model_validate(user) for user in await UserRepository(db).list()]


@router.get("/{user_id}", response_model=UserRead)
async def get_user(user_id: uuid.UUID, current_user: CurrentUser, db: TenantDB) -> UserRead:
    user = await UserRepository(db).get(user_id)
    if user is None:  # also the outcome for users of other tenants (hidden by RLS)
        raise NotFoundError("User not found")
    return UserRead.model_validate(user)
