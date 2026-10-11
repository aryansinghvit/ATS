from __future__ import annotations

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, EmailStr, Field

from app.schemas.tenant import TenantRead
from app.schemas.user import UserRead


class OrganizationProfile(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=255)
    subdomain: str | None = Field(
        default=None,
        max_length=63,
        description="Desired workspace subdomain; defaults to the Clerk org slug, then the name.",
    )


class SyncUserRequest(BaseModel):
    """Profile data for the Clerk identity in the bearer token.

    Identity (Clerk user id, organization id, role) always comes from the verified token, never
    from this body. ``email`` is used only when the token has no ``email`` claim.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    email: EmailStr
    full_name: str = Field(min_length=1, max_length=255)
    department: str | None = Field(default=None, max_length=100)
    avatar_url: AnyHttpUrl | None = None
    organization: OrganizationProfile | None = Field(
        default=None, description="Required only when the organization's workspace is new."
    )


class SyncUserResponse(BaseModel):
    tenant: TenantRead
    user: UserRead
    tenant_created: bool
    user_created: bool


class MeResponse(BaseModel):
    user: UserRead
    tenant: TenantRead
