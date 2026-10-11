from __future__ import annotations

import uuid
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, EmailStr, Field, field_validator


class TemplateRead(BaseModel):
    key: str
    name: str
    description: str
    trigger: str
    subject: str
    body: str
    enabled: bool
    customized: bool
    variables: list[str]


class TemplateUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1, max_length=20_000)
    enabled: bool = True


class TemplatePreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: str | None = Field(default=None, max_length=300)
    body: str | None = Field(default=None, max_length=20_000)
    application_id: uuid.UUID | None = None


class TemplatePreview(BaseModel):
    subject: str
    body: str
    context: dict[str, str]


class CommunicationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scheduling_url: AnyHttpUrl | None = Field(
        default=None, description="Cal.com / Calendly link injected as {{scheduling_link}}"
    )
    rejection_delay_hours: int = Field(default=48, ge=24, le=72)
    timezone: str = "UTC"
    reply_to: EmailStr | None = None

    @field_validator("scheduling_url")
    @classmethod
    def _https_only(cls, value: AnyHttpUrl | None) -> AnyHttpUrl | None:
        if value is not None and value.scheme != "https":
            raise ValueError("Scheduling links must use https")
        return value

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"Unknown timezone '{value}'") from None
        return value


class OutboxEmailRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    template_key: str
    trigger: str
    to_email: str
    status: str
    scheduled_for: datetime
    sent_at: datetime | None
    attempts: int
    last_error: str | None
    subject: str | None
    body: str | None
