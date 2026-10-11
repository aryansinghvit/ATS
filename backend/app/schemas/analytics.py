from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field


class StageFunnel(BaseModel):
    name: str
    stage_type: str
    position: int
    reached: int = Field(description="Applications that reached this stage or a later one")
    current: int = Field(description="Applications sitting in this stage now")
    conversion_from_previous: float | None = Field(description="reached / reached(previous)")
    drop_off_rate: float | None = Field(description="1 - reached(next) / reached")
    rejected_here: int = Field(description="Moved to Rejected from this stage")
    avg_days_in_stage: float | None


class TimeToHire(BaseModel):
    hired: int
    average_days: float | None
    median_days: float | None
    p90_days: float | None


class Totals(BaseModel):
    applications: int
    in_progress: int
    hired: int
    rejected: int
    withdrawn: int
    hire_rate: float | None


class WeeklyApplications(BaseModel):
    week_start: date
    applications: int


class JobMetrics(BaseModel):
    job_id: uuid.UUID
    title: str
    status: str
    applications: int
    in_progress: int
    hired: int
    rejected: int
    average_match_score: float | None
    median_days_to_hire: float | None


class PipelineAnalytics(BaseModel):
    job_id: uuid.UUID | None
    date_from: date | None
    date_to: date | None
    generated_at: datetime
    totals: Totals
    funnel: list[StageFunnel]
    time_to_hire: TimeToHire
    weekly_applications: list[WeeklyApplications]
    jobs: list[JobMetrics]
