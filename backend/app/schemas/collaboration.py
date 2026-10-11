from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.collaboration import Recommendation


class ScorecardScores(BaseModel):
    technical_competence: int | None = Field(default=None, ge=1, le=5)
    communication: int | None = Field(default=None, ge=1, le=5)
    problem_solving: int | None = Field(default=None, ge=1, le=5)


class ScorecardUpsert(ScorecardScores):
    """Save the caller's own scorecard; ``submit=true`` locks it (all ratings required)."""

    model_config = ConfigDict(extra="forbid")

    overall_recommendation: Recommendation | None = None
    notes_markdown: str | None = Field(default=None, max_length=10_000)
    submit: bool = False

    @model_validator(mode="after")
    def _complete_when_submitting(self) -> ScorecardUpsert:
        if self.submit:
            missing = [
                name
                for name in ("technical_competence", "communication", "problem_solving")
                if getattr(self, name) is None
            ]
            if self.overall_recommendation is None:
                missing.append("overall_recommendation")
            if missing:
                raise ValueError(f"Required to submit: {', '.join(missing)}")
        return self


class ScorecardRead(ScorecardScores):
    id: uuid.UUID
    evaluator_id: uuid.UUID
    evaluator_name: str | None
    evaluator_role: str
    overall_recommendation: Recommendation | None
    notes_markdown: str | None
    is_submitted: bool
    submitted_at: datetime | None
    updated_at: datetime


class ScorecardSummary(BaseModel):
    submitted: int
    averages: dict[str, float | None]
    recommendations: dict[str, int]


class ScorecardBoard(BaseModel):
    mine: ScorecardRead | None
    others: list[ScorecardRead]
    can_view_others: bool
    hidden_count: int = Field(description="Submitted colleague scorecards hidden from the caller")
    visibility_rule: str
    summary: ScorecardSummary | None


class NoteCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    content: str = Field(
        min_length=1,
        max_length=5000,
        description="Plain text; mention teammates with @[Display Name](user-uuid)",
    )


class Mention(BaseModel):
    user_id: uuid.UUID
    name: str


class NoteRead(BaseModel):
    id: uuid.UUID
    author_id: uuid.UUID
    author_name: str | None
    content: str
    mentions: list[Mention]
    created_at: datetime


class NotificationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    type: str
    title: str
    body: str | None
    link: str | None
    entity_type: str | None
    entity_id: uuid.UUID | None
    read_at: datetime | None
    created_at: datetime


class NotificationList(BaseModel):
    unread: int
    items: list[NotificationRead]
