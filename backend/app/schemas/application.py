from __future__ import annotations

import uuid
from typing import Literal

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, EmailStr, Field

from app.models.candidate import ApplicationStatus

# Must stay in sync with app.services.storage.RESUME_MIME_TYPES.
ResumeMimeType = Literal[
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
]


class _PublicJobReference(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    tenant_subdomain: str = Field(min_length=1, max_length=63)
    job_id: uuid.UUID


class PresignUploadRequest(_PublicJobReference):
    file_name: str = Field(min_length=1, max_length=255)
    mime_type: ResumeMimeType
    file_size_bytes: int = Field(gt=0)


class PresignUploadResponse(BaseModel):
    upload_url: str
    method: Literal["PUT"]
    headers: dict[str, str]
    s3_key: str
    expires_in: int
    max_file_size_bytes: int


class SubmitApplicationRequest(_PublicJobReference):
    s3_key: str = Field(min_length=1, max_length=512)
    file_name: str = Field(min_length=1, max_length=255)
    email: EmailStr
    full_name: str | None = Field(default=None, max_length=200)
    phone: str | None = Field(default=None, max_length=50)
    linkedin_url: AnyHttpUrl | None = None


class SubmitApplicationResponse(BaseModel):
    application_id: uuid.UUID
    status: ApplicationStatus
    status_token: str = Field(description="Capability token for the application status stream")
    events_url: str


class ConfirmProfileRequest(BaseModel):
    """The candidate's verified version of the fields the M3 extractor parsed."""

    model_config = ConfigDict(str_strip_whitespace=True)

    full_name: str = Field(min_length=1, max_length=200)
    email: EmailStr
    phone: str | None = Field(default=None, max_length=50)
    linkedin_url: str | None = Field(default=None, max_length=500)
    github_url: str | None = Field(default=None, max_length=500)
    location: str | None = Field(default=None, max_length=150)
    skills: list[str] = Field(default_factory=list, max_length=100)


class ConfirmProfileResponse(BaseModel):
    application_id: uuid.UUID
    status: ApplicationStatus
    confirmed: bool
