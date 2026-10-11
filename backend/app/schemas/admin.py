"""Schemas for Phase 7 administration: stages, team, interviews, offers, GDPR erasure."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

StageType = Literal["applied", "screen", "interview", "offer", "hired", "rejected"]


# ------------------------------------------------------------------------ stage editor
class StageRead(BaseModel):
    id: uuid.UUID
    name: str
    stage_type: StageType
    position_index: int
    automated_actions: list[str]
    applications: int


class StageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=100)
    stage_type: StageType
    position_index: int | None = Field(default=None, ge=0)
    automated_actions: list[str] = Field(default_factory=list, max_length=5)


class StageUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=1, max_length=100)
    stage_type: StageType | None = None
    automated_actions: list[str] | None = Field(default=None, max_length=5)


class StageReorder(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage_ids: list[uuid.UUID] = Field(min_length=1)


class StageActionInfo(BaseModel):
    key: str
    description: str


# --------------------------------------------------------------------------- team
class TeamMemberUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    department: str | None = Field(default=None, max_length=100)
    is_active: bool | None = None


# ----------------------------------------------------------------------- interviews
class InterviewCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=200)
    interviewer_id: uuid.UUID | None = None
    scheduled_start: datetime
    scheduled_end: datetime
    meeting_url: str | None = Field(default=None, max_length=500, pattern=r"^https://")

    @model_validator(mode="after")
    def _ordered(self) -> InterviewCreate:
        if self.scheduled_end <= self.scheduled_start:
            raise ValueError("scheduled_end must be after scheduled_start")
        if self.scheduled_start.tzinfo is None:
            raise ValueError("Times must include a timezone offset")
        return self


class InterviewUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["scheduled", "cancelled", "completed"]


class InterviewRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    application_id: uuid.UUID
    interviewer_id: uuid.UUID | None
    interviewer_name: str | None = None
    title: str
    scheduled_start: datetime
    scheduled_end: datetime
    meeting_url: str | None
    source: str
    status: str
    created_at: datetime


# ---------------------------------------------------------------------------- offers
class OfferLetterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    base_salary: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    currency: str = Field(default="USD", pattern=r"^[A-Za-z]{3}$")
    start_date: date
    job_title: str | None = Field(default=None, max_length=255)
    equity: str | None = Field(default=None, max_length=200)
    respond_by: date | None = None
    additional_terms: str | None = Field(default=None, max_length=2000)


class OfferLetterRead(BaseModel):
    application_id: uuid.UUID
    details: dict[str, str | None]
    generated_at: datetime
    download_url: str
    expires_in: int


# ---------------------------------------------------------------- GDPR Article 17
class CandidateLookup(BaseModel):
    candidate_id: uuid.UUID
    email: EmailStr
    full_name: str | None
    applications: int
    created_at: datetime


class ErasureReport(BaseModel):
    candidate_id: uuid.UUID
    applications_deleted: int
    files_deleted: int
    file_errors: int
    notifications_deleted: int
    erased_at: datetime
    status: Literal["erased"] = "erased"
