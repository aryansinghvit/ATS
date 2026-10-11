"""M3 extractor -> ATS webhook envelope (project.md §6.3)."""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class CallbackContext(BaseModel):
    tenant_id: uuid.UUID
    application_id: uuid.UUID
    candidate_id: uuid.UUID


class SkillEvidence(BaseModel):
    model_config = ConfigDict(extra="allow")

    skill: str
    evidence: str | None = None


class MatchBreakdown(BaseModel):
    matched_required: list[SkillEvidence | str] = Field(default_factory=list)
    missing_required: list[SkillEvidence | str] = Field(default_factory=list)
    matched_preferred: list[SkillEvidence | str] = Field(default_factory=list)
    missing_preferred: list[SkillEvidence | str] = Field(default_factory=list)


class DeterministicMatch(BaseModel):
    model_config = ConfigDict(extra="allow")

    overall_score: float = Field(ge=0, le=100)
    breakdown: MatchBreakdown = Field(default_factory=MatchBreakdown)
    experience_match: dict[str, Any] | None = None


class ExtractorWebhookPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    tracking_id: str = Field(min_length=1, max_length=100)
    callback_context: CallbackContext
    status: str = Field(description='"success", or a failure status such as "failed"/"error"')
    execution_metrics: dict[str, Any] | None = None
    candidate_profile: dict[str, Any] | None = None
    deterministic_match: DeterministicMatch | None = None
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status.lower() == "success" and self.deterministic_match is not None
