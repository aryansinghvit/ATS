from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.candidate import ApplicationStatus


class TalentSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    query: str = Field(min_length=3, max_length=500, description="Natural-language sourcing query")
    limit: int = Field(default=20, ge=1, le=50)
    min_similarity: float | None = Field(default=None, ge=0, le=1)


class PastApplication(BaseModel):
    application_id: uuid.UUID
    job_id: uuid.UUID
    job_title: str
    status: ApplicationStatus
    deterministic_match_score: float | None
    applied_at: datetime


class TalentMatch(BaseModel):
    candidate_id: uuid.UUID
    full_name: str | None
    email: str
    location: str | None
    similarity: float = Field(description="Cosine similarity between query and profile (0–1)")
    keyword_rank: float | None = Field(description="PostgreSQL full-text rank (ts_rank_cd)")
    hybrid_score: float = Field(description="Reciprocal-rank-fusion score used for ordering")
    matched_skills: list[str] = Field(description="Profile skills named in the query")
    skills: list[str]
    highlights: list[str] = Field(description="Profile snippets; «» marks query-term hits")
    applications: list[PastApplication]


class TalentSearchResponse(BaseModel):
    query: str
    model: str
    total_indexed: int
    results: list[TalentMatch]


class TalentPoolStats(BaseModel):
    model: str
    dimensions: int
    candidates: int
    indexed: int


class ReindexResponse(BaseModel):
    queued: int


class TalentSuggestion(BaseModel):
    candidate_id: uuid.UUID
    full_name: str | None
    email: str
    location: str | None
    similarity: float
    status: str
    skills: list[str]
    applications: list[PastApplication]


class ResurfacingResult(BaseModel):
    job_id: uuid.UUID
    threshold: float
    matches: int
    new_matches: int
    notified: int


class InviteToJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: uuid.UUID


class InviteToJobResponse(BaseModel):
    application_id: uuid.UUID
    job_id: uuid.UUID
    status: ApplicationStatus
    email_queued: bool
