from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.models.candidate import ApplicationStatus


class DossierCandidate(BaseModel):
    """Contact details; every PII field is None in a blind (redacted) dossier."""

    id: uuid.UUID
    display_name: str
    full_name: str | None
    email: str | None
    phone: str | None
    linkedin_url: str | None
    github_url: str | None
    portfolio_url: str | None
    location: str | None


class DossierResume(BaseModel):
    file_name: str | None
    kind: Literal["pdf", "docx"]
    mime_type: str
    url: str | None = Field(description="Expiring download URL; withheld in blind mode")
    url_expires_in: int | None


class SkillEvidence(BaseModel):
    skill: str
    evidence: str | None = None


class DossierMatch(BaseModel):
    extraction_status: Literal["pending", "complete", "failed"]
    extraction_error: str | None
    deterministic_match_score: float | None
    matched_required: list[SkillEvidence]
    missing_required: list[SkillEvidence]
    matched_preferred: list[SkillEvidence]
    missing_preferred: list[SkillEvidence]
    experience_match: dict[str, Any] | None


class DossierProfile(BaseModel):
    summary: str | None
    skills: list[str]
    verified_skills: list[str]
    work_experience: list[dict[str, Any]]
    education: list[dict[str, Any]]
    projects: list[dict[str, Any]]


class StageEvent(BaseModel):
    at: datetime
    from_stage: str | None
    to_stage: str | None
    actor_name: str | None


class DossierStage(BaseModel):
    id: uuid.UUID
    name: str
    stage_type: str


class DossierJob(BaseModel):
    id: uuid.UUID
    title: str


class CandidateDossier(BaseModel):
    application_id: uuid.UUID
    redacted: bool = Field(description="True for blind interviewers (no PII, score or resume)")
    job: DossierJob
    status: ApplicationStatus
    stage: DossierStage | None
    applied_at: datetime
    confirmed_at: datetime | None
    version: int
    candidate: DossierCandidate
    resume: DossierResume
    match: DossierMatch | None
    profile: DossierProfile
    stage_history: list[StageEvent]
