"""Pipeline conversion & time-to-hire analytics (tenant-scoped by Row-Level Security).

The funnel is cumulative: an application "reached" a stage if its furthest progressive stage
(any non-Rejected column it has ever been in) is at or beyond that stage, so cards that skip
columns still count and the funnel never increases. Stage entry times come from the
``stage_changed`` audit trail written by every Kanban move.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from statistics import mean, median

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.candidate import ApplicationStatus, JobApplication
from app.models.job import Job, PipelineStage
from app.schemas.analytics import (
    JobMetrics,
    PipelineAnalytics,
    StageFunnel,
    TimeToHire,
    Totals,
    WeeklyApplications,
)

WEEKS = 12
_CLOSED = {ApplicationStatus.HIRED, ApplicationStatus.REJECTED, ApplicationStatus.WITHDRAWN}


def _days(delta: timedelta) -> float:
    return delta.total_seconds() / 86_400


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(fraction * len(ordered)))], 1)


class AnalyticsService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def pipeline(
        self,
        *,
        job_id: uuid.UUID | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> PipelineAnalytics:
        jobs_query = select(Job)
        apps_query = select(JobApplication)
        if job_id is not None:
            jobs_query = jobs_query.where(Job.id == job_id)
            apps_query = apps_query.where(JobApplication.job_id == job_id)
        if date_from is not None:
            apps_query = apps_query.where(
                JobApplication.applied_at >= datetime.combine(date_from, time.min, UTC)
            )
        if date_to is not None:
            apps_query = apps_query.where(
                JobApplication.applied_at
                < datetime.combine(date_to + timedelta(days=1), time.min, UTC)
            )
        jobs = {job.id: job for job in (await self._session.execute(jobs_query)).scalars()}
        applications = [
            a for a in (await self._session.execute(apps_query)).scalars() if a.job_id in jobs
        ]
        stages = {
            s.id: s
            for s in (
                await self._session.execute(
                    select(PipelineStage).where(PipelineStage.job_id.in_(list(jobs)))
                )
            ).scalars()
        }
        app_ids = [a.id for a in applications]
        moves: dict[uuid.UUID, list[AuditLog]] = defaultdict(list)
        if app_ids:
            for log in (
                await self._session.execute(
                    select(AuditLog)
                    .where(AuditLog.action == "stage_changed", AuditLog.entity_id.in_(app_ids))
                    .order_by(AuditLog.created_at)
                )
            ).scalars():
                moves[log.entity_id].append(log)

        # Funnel columns are aggregated by name across jobs, ordered by their usual position.
        columns: dict[str, dict] = {}
        for stage in sorted(stages.values(), key=lambda s: s.position_index):
            if stage.stage_type == "rejected":
                continue
            column = columns.setdefault(
                stage.name,
                {"type": stage.stage_type, "positions": [], "reached": 0, "current": 0,
                 "rejected": 0, "stays": []},
            )  # fmt: skip
            column["positions"].append(stage.position_index)

        def stage_of(stage_id: object) -> PipelineStage | None:
            try:
                return stages.get(uuid.UUID(str(stage_id))) if stage_id else None
            except ValueError:
                return None

        hire_days: dict[uuid.UUID, float] = {}
        for application in applications:
            history = moves.get(application.id, [])
            visited = [stage_of(log.metadata_json.get("to_stage_id")) for log in history]
            current = (
                stages.get(application.current_stage_id) if application.current_stage_id else None
            )
            progressive = [
                s for s in [*visited, current] if s is not None and s.stage_type != "rejected"
            ]
            furthest = max((s.position_index for s in progressive), default=0)
            for column in columns.values():
                if min(column["positions"]) <= furthest or column["type"] == "applied":
                    column["reached"] += 1
            if current is not None and current.name in columns:
                columns[current.name]["current"] += 1

            # Time spent in each stage (completed stays only): from entering it until the next move.
            entered_at = application.applied_at
            entered_stage = next(
                (
                    s
                    for s in stages.values()
                    if s.job_id == application.job_id and s.stage_type == "applied"
                ),
                None,
            )
            for log in history:
                if entered_stage is not None and entered_stage.name in columns:
                    columns[entered_stage.name]["stays"].append(_days(log.created_at - entered_at))
                target = stage_of(log.metadata_json.get("to_stage_id"))
                source = stage_of(log.metadata_json.get("from_stage_id"))
                rejected_from = (
                    target is not None and target.stage_type == "rejected" and source is not None
                )
                if rejected_from and source.name in columns:
                    columns[source.name]["rejected"] += 1
                if (
                    target is not None
                    and target.stage_type == "hired"
                    and application.id not in hire_days
                ):
                    hire_days[application.id] = _days(log.created_at - application.applied_at)
                entered_at, entered_stage = log.created_at, target

        names = list(columns)
        funnel = []
        for index, name in enumerate(names):
            column = columns[name]
            previous = columns[names[index - 1]]["reached"] if index else None
            following = columns[names[index + 1]]["reached"] if index + 1 < len(names) else None
            funnel.append(
                StageFunnel(
                    name=name,
                    stage_type=column["type"],
                    position=min(column["positions"]),
                    reached=column["reached"],
                    current=column["current"],
                    conversion_from_previous=_ratio(column["reached"], previous)
                    if previous is not None
                    else None,
                    drop_off_rate=(
                        round(1 - following / column["reached"], 4)
                        if following is not None and column["reached"]
                        else None
                    ),
                    rejected_here=column["rejected"],
                    avg_days_in_stage=round(mean(column["stays"]), 2) if column["stays"] else None,
                )
            )

        statuses = [a.status for a in applications]
        hired = sum(1 for s in statuses if s is ApplicationStatus.HIRED)
        totals = Totals(
            applications=len(applications),
            in_progress=sum(1 for s in statuses if s not in _CLOSED),
            hired=hired,
            rejected=sum(1 for s in statuses if s is ApplicationStatus.REJECTED),
            withdrawn=sum(1 for s in statuses if s is ApplicationStatus.WITHDRAWN),
            hire_rate=_ratio(hired, len(applications)),
        )
        durations = list(hire_days.values())
        time_to_hire = TimeToHire(
            hired=len(durations),
            average_days=round(mean(durations), 1) if durations else None,
            median_days=round(median(durations), 1) if durations else None,
            p90_days=_percentile(durations, 0.9),
        )

        today = datetime.now(UTC).date()
        start = today - timedelta(days=today.weekday(), weeks=WEEKS - 1)
        weekly = {start + timedelta(weeks=i): 0 for i in range(WEEKS)}
        for application in applications:
            day = application.applied_at.date()
            week = day - timedelta(days=day.weekday())
            if week in weekly:
                weekly[week] += 1

        per_job = []
        for job in sorted(jobs.values(), key=lambda j: j.created_at, reverse=True):
            job_apps = [a for a in applications if a.job_id == job.id]
            scores = [
                float(a.deterministic_match_score)
                for a in job_apps
                if a.deterministic_match_score is not None
            ]
            job_hire_days = [hire_days[a.id] for a in job_apps if a.id in hire_days]
            per_job.append(
                JobMetrics(
                    job_id=job.id,
                    title=job.title,
                    status=job.status.value,
                    applications=len(job_apps),
                    in_progress=sum(1 for a in job_apps if a.status not in _CLOSED),
                    hired=sum(1 for a in job_apps if a.status is ApplicationStatus.HIRED),
                    rejected=sum(1 for a in job_apps if a.status is ApplicationStatus.REJECTED),
                    average_match_score=round(mean(scores), 1) if scores else None,
                    median_days_to_hire=round(median(job_hire_days), 1) if job_hire_days else None,
                )
            )

        return PipelineAnalytics(
            job_id=job_id,
            date_from=date_from,
            date_to=date_to,
            generated_at=datetime.now(UTC),
            totals=totals,
            funnel=funnel,
            time_to_hire=time_to_hire,
            weekly_applications=[
                WeeklyApplications(week_start=w, applications=n) for w, n in weekly.items()
            ],
            jobs=per_job,
        )
