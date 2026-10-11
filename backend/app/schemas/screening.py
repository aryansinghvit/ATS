from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.collaboration import ScreeningStatus


class ScreeningQuestion(BaseModel):
    question: str = Field(min_length=15, max_length=600)
    targets: str = Field(min_length=1, max_length=120, description="Skill gap or area probed")
    rationale: str = Field(default="", max_length=400)


class GenerateScreeningRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    count: int = Field(default=3, ge=3, le=5)


# --------------------------------------------------------------------- recruiter view
class QuestionRubric(BaseModel):
    index: int
    technical_depth: int = Field(ge=1, le=5)
    clarity: int = Field(ge=1, le=5)
    relevance: int = Field(ge=1, le=5)
    justification: str = ""


class ScreeningEvaluation(BaseModel):
    """Advisory-only rubric scorecard. It never changes the candidate's stage or status."""

    source: Literal["ai", "heuristic"]
    model: str | None
    overall_score: float = Field(description="Mean rubric rating, 1.0–5.0")
    category_averages: dict[str, float]
    rubric: list[QuestionRubric]
    summary: str
    strengths: list[str]
    probe_questions: list[str]
    advisory: Literal[True] = True
    requires_human_review: Literal[True] = True
    note: str | None = None
    evaluated_at: datetime


class ScreeningResponseRead(BaseModel):
    index: int
    question: str
    targets: str
    type: Literal["text", "audio"] | None
    text: str | None
    transcript: str | None
    transcription_engine: str | None
    transcription_error: str | None
    audio_url: str | None
    updated_at: datetime | None


class ScreeningRead(BaseModel):
    id: uuid.UUID
    application_id: uuid.UUID
    status: ScreeningStatus
    source: Literal["ai", "fallback"]
    source_detail: str | None
    model: str | None
    skill_gaps: list[str]
    questions: list[ScreeningQuestion]
    expires_at: datetime
    created_at: datetime
    invited_at: datetime | None = None
    opened_at: datetime | None = None
    submitted_at: datetime | None = None
    candidate_link: str | None = Field(
        default=None, description="Signed portal link (only while the screening is open)"
    )
    responses: list[ScreeningResponseRead] = Field(default_factory=list)
    evaluation_status: Literal["not_started", "pending", "completed"] = "not_started"
    evaluation: ScreeningEvaluation | None = None


# ------------------------------------------------------------------- candidate portal
class PortalQuestion(BaseModel):
    index: int
    question: str


class PortalAnswer(BaseModel):
    index: int
    type: Literal["text", "audio"]
    text: str | None
    has_audio: bool
    audio_bytes: int | None
    updated_at: datetime


class PortalLimits(BaseModel):
    max_audio_seconds: int
    max_audio_bytes: int
    max_text_chars: int


class PortalView(BaseModel):
    status: ScreeningStatus
    company_name: str
    logo_url: str | None
    primary_color: str
    job_title: str
    candidate_first_name: str | None
    expires_at: datetime
    questions: list[PortalQuestion]
    answers: list[PortalAnswer]
    limits: PortalLimits


class SaveTextAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(max_length=4000)


class PortalSubmitResponse(BaseModel):
    status: ScreeningStatus
    submitted_at: datetime
