from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    StringConstraints,
    model_validator,
)

from app.models.job import EmploymentType, JobStatus, RemotePolicy
from app.schemas.tenant import TenantBranding

SkillName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Money = Annotated[
    Decimal,
    Field(ge=0, max_digits=12, decimal_places=2),
    PlainSerializer(float, return_type=float, when_used="json"),
]
CurrencyCode = Annotated[str, StringConstraints(pattern=r"^[A-Za-z]{3}$", to_upper=True)]
HttpsUrl = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=r"^https://[^\s]+$", max_length=500),
]
Slug = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=255)]


# ---------------------------------------------------------------------------------- taxonomy
class RequiredSkill(BaseModel):
    name: SkillName
    min_years: float = Field(default=0, ge=0, le=40)
    weight: float = Field(default=1.0, gt=0, le=1)


class PreferredSkill(BaseModel):
    name: SkillName
    weight: float = Field(default=0.5, gt=0, le=1)


class JobTaxonomy(BaseModel):
    """Normalized skill taxonomy fed to the M3 extractor's deterministic match scoring."""

    required_skills: list[RequiredSkill] = Field(default_factory=list, max_length=50)
    preferred_skills: list[PreferredSkill] = Field(default_factory=list, max_length=50)
    min_total_experience_years: float = Field(default=0, ge=0, le=50)
    education_level: str | None = Field(default=None, max_length=200)


class _SalaryRange(BaseModel):
    salary_min: Money | None = None
    salary_max: Money | None = None

    @model_validator(mode="after")
    def _check_salary_range(self) -> _SalaryRange:
        if (
            self.salary_min is not None
            and self.salary_max is not None
            and self.salary_min > self.salary_max
        ):
            raise ValueError("salary_min must not exceed salary_max")
        return self


# ------------------------------------------------------------------------ AI description
class JobDescriptionRequest(_SalaryRange):
    """Rough recruiter notes plus optional facts that override what the model infers."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    prompt: str = Field(min_length=10, max_length=4000)
    title: str | None = Field(default=None, max_length=255)
    department: str | None = Field(default=None, max_length=100)
    location: str | None = Field(default=None, max_length=100)
    remote_policy: RemotePolicy | None = None
    employment_type: EmploymentType | None = None
    seniority_level: str | None = Field(default=None, max_length=50)
    currency: CurrencyCode | None = None


class JobDraft(_SalaryRange):
    """An editable job requisition draft; can be submitted to ``POST /jobs`` as-is."""

    title: str
    department: str
    location: str
    remote_policy: RemotePolicy
    employment_type: EmploymentType
    seniority_level: str | None
    currency: str
    description_markdown: str
    taxonomy: JobTaxonomy
    model: str


# --------------------------------------------------------------------------------- jobs CRUD
class JobCreate(_SalaryRange):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=255)
    slug: Slug | None = None
    department: str = Field(min_length=1, max_length=100)
    location: str = Field(min_length=1, max_length=100)
    remote_policy: RemotePolicy = RemotePolicy.REMOTE
    employment_type: EmploymentType = EmploymentType.FULL_TIME
    seniority_level: str | None = Field(default=None, max_length=50)
    currency: CurrencyCode = "USD"
    description_markdown: str = Field(min_length=1, max_length=50_000)
    taxonomy: JobTaxonomy | None = Field(
        default=None, description="Omit to have the AI extract it from the description."
    )
    scheduling_url: HttpsUrl | None = None
    status: Literal[JobStatus.DRAFT, JobStatus.PUBLISHED] = JobStatus.DRAFT


class JobUpdate(_SalaryRange):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str | None = Field(default=None, min_length=1, max_length=255)
    slug: Slug | None = None
    department: str | None = Field(default=None, min_length=1, max_length=100)
    location: str | None = Field(default=None, min_length=1, max_length=100)
    remote_policy: RemotePolicy | None = None
    employment_type: EmploymentType | None = None
    seniority_level: str | None = Field(default=None, max_length=50)
    currency: CurrencyCode | None = None
    description_markdown: str | None = Field(default=None, min_length=1, max_length=50_000)
    taxonomy: JobTaxonomy | None = None
    scheduling_url: HttpsUrl | None = None
    status: JobStatus | None = None


class JobListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    slug: str
    department: str
    location: str
    remote_policy: RemotePolicy
    employment_type: EmploymentType
    status: JobStatus
    published_at: datetime | None
    created_at: datetime
    updated_at: datetime


class JobRead(JobListItem):
    tenant_id: uuid.UUID
    seniority_level: str | None
    salary_min: Money | None
    salary_max: Money | None
    currency: str | None
    description_markdown: str
    description_html: str
    taxonomy: JobTaxonomy = Field(validation_alias="taxonomy_json")
    scheduling_url: str | None
    creator_id: uuid.UUID | None


# ------------------------------------------------------------------------- public careers
class PublicJobSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    slug: str
    department: str
    location: str
    remote_policy: RemotePolicy
    employment_type: EmploymentType
    seniority_level: str | None
    salary_min: Money | None
    salary_max: Money | None
    currency: str | None
    published_at: datetime | None


class PublicJobDetail(PublicJobSummary):
    description_html: str
    organization: TenantBranding
    json_ld: dict[str, Any] = Field(description="Schema.org JobPosting (Google for Jobs)")


class PublicJobBoard(BaseModel):
    organization: TenantBranding
    jobs: list[PublicJobSummary]
