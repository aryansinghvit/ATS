from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.candidate import ApplicationStatus
from app.models.job import JobStatus


class MatchChips(BaseModel):
    """Card badges: 🟢 matched core skills, 🟡 matched preferred skills, 🔴 missing required."""

    matched_required: list[str] = Field(default_factory=list)
    matched_preferred: list[str] = Field(default_factory=list)
    missing_required: list[str] = Field(default_factory=list)


class CardCandidate(BaseModel):
    id: uuid.UUID
    full_name: str | None
    email: str
    location: str | None
    linkedin_url: str | None


class PipelineCard(BaseModel):
    application_id: uuid.UUID
    job_id: uuid.UUID
    stage_id: uuid.UUID | None
    status: ApplicationStatus
    extraction_status: Literal["pending", "complete", "failed"]
    deterministic_match_score: float | None
    match: MatchChips
    candidate: CardCandidate
    applied_at: datetime
    stage_entered_at: datetime
    version: int


class PipelineColumn(BaseModel):
    id: uuid.UUID
    name: str
    stage_type: str
    position_index: int
    total_count: int = Field(description="Cards in the stage before filters are applied")
    cards: list[PipelineCard]


class PipelineJob(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    slug: str
    status: JobStatus
    required_skills: list[str] = Field(default_factory=list)


PipelineSort = Literal["default", "score", "applied_desc", "applied_asc", "missing_skills"]


class PipelineFilters(BaseModel):
    """Board view options. ``default``: Applied by score DESC NULLS LAST, later stages FIFO."""

    sort: PipelineSort = "default"
    min_score: float | None = Field(default=None, ge=0, le=100)
    missing_skill: str | None = Field(default=None, max_length=100)
    q: str | None = Field(default=None, max_length=200)

    @property
    def active(self) -> bool:
        return any(v is not None for v in (self.min_score, self.missing_skill, self.q))


class PipelineBoard(BaseModel):
    job: PipelineJob
    filters: PipelineFilters
    stages: list[PipelineColumn]


class MoveStageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to_stage_id: uuid.UUID
    expected_version: int = Field(ge=1, description="Card version the client last saw")
