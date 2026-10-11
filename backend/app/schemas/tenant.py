from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, field_validator

from app.core.tenancy import normalize_custom_domain
from app.models.tenant import HEX_COLOR_PATTERN

# settings_json keys owned by platform subsystems; tenants cannot write them directly.
RESERVED_SETTINGS_KEYS = frozenset({"billing"})


class TenantBranding(BaseModel):
    """Public, unauthenticated workspace branding (resolved from the Host header)."""

    model_config = ConfigDict(from_attributes=True)

    name: str
    subdomain: str
    logo_url: str | None
    primary_color: str


class TenantRead(TenantBranding):
    id: uuid.UUID
    custom_domain: str | None


class TenantSettingsRead(TenantRead):
    clerk_org_id: str | None
    settings: dict[str, Any] = Field(validation_alias="settings_json")
    created_at: datetime
    updated_at: datetime


class TenantSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    logo_url: AnyHttpUrl | None = None
    primary_color: str | None = Field(default=None, pattern=HEX_COLOR_PATTERN)
    custom_domain: str | None = None
    settings: dict[str, Any] | None = Field(
        default=None, description="Shallow-merged into the workspace settings."
    )

    @field_validator("custom_domain")
    @classmethod
    def _validate_domain(cls, value: str | None) -> str | None:
        return normalize_custom_domain(value) if value is not None else None

    @field_validator("settings")
    @classmethod
    def _reject_reserved_keys(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value and (reserved := RESERVED_SETTINGS_KEYS & value.keys()):
            raise ValueError(f"Settings keys are managed by the platform: {sorted(reserved)}")
        return value


class BillingRead(BaseModel):
    plan: str
    status: str
    seats_used: int
