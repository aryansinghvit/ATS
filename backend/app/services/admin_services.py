"""Team management, interviews (incl. Cal.com bookings), offer letters and GDPR erasure."""

from __future__ import annotations

import hashlib
import hmac
import logging
import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    UnprocessableError,
)
from app.core.pdf import render_text_pdf
from app.models.audit import AuditLog
from app.models.candidate import Candidate, JobApplication
from app.models.collaboration import Interview, Notification, Screening
from app.models.job import Job, PipelineStage
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.schemas.admin import (
    CandidateLookup,
    ErasureReport,
    InterviewCreate,
    InterviewRead,
    OfferLetterRead,
    OfferLetterRequest,
    TeamMemberUpdate,
)
from app.services.storage import StorageService

logger = logging.getLogger(__name__)


def _audit(tenant_id: uuid.UUID, actor_id: uuid.UUID | None, action: str, entity_type: str,
           entity_id: uuid.UUID, **metadata: Any) -> AuditLog:  # fmt: skip
    return AuditLog(
        tenant_id=tenant_id,
        actor_id=actor_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        metadata_json=metadata,
    )


# ----------------------------------------------------------------------------- team
class TeamService:
    """Roles are owned by Clerk (synced on every request); the ATS manages activation/department."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def update(self, user_id: uuid.UUID, payload: TeamMemberUpdate, *, actor: User) -> User:
        user = await self._session.get(User, user_id)
        if user is None:
            raise NotFoundError("User not found")
        changes = payload.model_dump(exclude_unset=True)
        if changes.get("is_active") is False and user.is_active:
            if user.id == actor.id:
                raise UnprocessableError("You cannot deactivate your own account")
            if user.role is UserRole.ADMIN:
                active_admins = await self._session.scalar(
                    select(func.count())
                    .select_from(User)
                    .where(User.role == UserRole.ADMIN, User.is_active.is_(True))
                )
                if (active_admins or 0) <= 1:
                    raise ConflictError("A workspace must keep at least one active admin")
        for field, value in changes.items():
            setattr(user, field, value)
        self._session.add(
            _audit(
                actor.tenant_id, actor.id, "team_member_updated", "user", user.id, changes=changes
            )
        )
        await self._session.commit()
        return user


# ----------------------------------------------------------------------- interviews
class InterviewService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _read(self, interview: Interview) -> InterviewRead:
        interviewer = (
            await self._session.get(User, interview.interviewer_id)
            if interview.interviewer_id
            else None
        )
        return InterviewRead.model_validate(interview).model_copy(
            update={"interviewer_name": interviewer.full_name if interviewer else None}
        )

    async def list(self, application_id: uuid.UUID) -> list[InterviewRead]:
        if await self._session.get(JobApplication, application_id) is None:
            raise NotFoundError("Application not found")
        interviews = (
            await self._session.execute(
                select(Interview)
                .where(Interview.application_id == application_id)
                .order_by(Interview.scheduled_start)
            )
        ).scalars()
        return [await self._read(i) for i in interviews]

    async def create(
        self, application_id: uuid.UUID, payload: InterviewCreate, *, actor: User
    ) -> InterviewRead:
        application = await self._session.get(JobApplication, application_id)
        if application is None:
            raise NotFoundError("Application not found")
        if payload.interviewer_id and await self._session.get(User, payload.interviewer_id) is None:
            raise UnprocessableError("Interviewer is not a member of this workspace")
        interview = Interview(
            tenant_id=application.tenant_id,
            application_id=application_id,
            interviewer_id=payload.interviewer_id,
            title=payload.title,
            scheduled_start=payload.scheduled_start,
            scheduled_end=payload.scheduled_end,
            meeting_url=payload.meeting_url,
            source="manual",
        )
        self._session.add(interview)
        self._session.add(
            _audit(
                application.tenant_id,
                actor.id,
                "interview_scheduled",
                "job_application",
                application_id,
                source="manual",
                start=payload.scheduled_start.isoformat(),
            )  # fmt: skip
        )
        await self._session.commit()
        return await self._read(interview)

    async def set_status(
        self, interview_id: uuid.UUID, status: str, *, actor: User
    ) -> InterviewRead:
        interview = await self._session.get(Interview, interview_id)
        if interview is None:
            raise NotFoundError("Interview not found")
        interview.status = status
        self._session.add(
            _audit(
                interview.tenant_id,
                actor.id,
                f"interview_{status}",
                "job_application",
                interview.application_id,
                interview_id=str(interview_id),
            )  # fmt: skip
        )
        await self._session.commit()
        return await self._read(interview)

    async def ingest_calcom(self, tenant_id: uuid.UUID, event: dict[str, Any]) -> str:
        """Record a Cal.com booking (BOOKING_CREATED/RESCHEDULED/CANCELLED) as an interview."""
        trigger = str(event.get("triggerEvent", ""))
        payload = event.get("payload") or {}
        uid = payload.get("uid") or payload.get("bookingId")
        if not uid or trigger not in {
            "BOOKING_CREATED",
            "BOOKING_RESCHEDULED",
            "BOOKING_CANCELLED",
        }:
            return "ignored"
        existing = (
            await self._session.execute(
                select(Interview).where(Interview.calendar_event_id == str(uid))
            )
        ).scalar_one_or_none()
        if trigger == "BOOKING_CANCELLED":
            if existing is None:
                return "ignored"
            existing.status = "cancelled"
            await self._session.commit()
            return "cancelled"

        emails = [
            a.get("email", "").lower() for a in payload.get("attendees", []) if a.get("email")
        ]
        candidate = (
            (
                await self._session.execute(
                    select(Candidate).where(func.lower(Candidate.email).in_(emails))
                )
            )
            .scalars()
            .first()
            if emails
            else None
        )
        if candidate is None:
            return "ignored"
        rows = (
            await self._session.execute(
                select(JobApplication, PipelineStage.stage_type)
                .outerjoin(PipelineStage, PipelineStage.id == JobApplication.current_stage_id)
                .where(JobApplication.candidate_id == candidate.id)
                .order_by(JobApplication.applied_at.desc())
            )
        ).all()
        if not rows:
            return "ignored"
        application = next((a for a, kind in rows if kind == "interview"), rows[0][0])
        organizer_email = ((payload.get("organizer") or {}).get("email") or "").lower()
        interviewer = (
            (
                await self._session.execute(
                    select(User).where(func.lower(User.email) == organizer_email)
                )
            ).scalar_one_or_none()
            if organizer_email
            else None
        )
        start = datetime.fromisoformat(str(payload["startTime"]).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(payload["endTime"]).replace("Z", "+00:00"))
        meeting = (payload.get("metadata") or {}).get("videoCallUrl") or payload.get("location")
        meeting = meeting if isinstance(meeting, str) and meeting.startswith("https://") else None
        if existing is None:
            existing = Interview(
                tenant_id=tenant_id,
                application_id=application.id,
                calendar_event_id=str(uid),
                source="calcom",
                title=str(payload.get("title") or "Interview")[:200],
                scheduled_start=start,
                scheduled_end=end,
            )
            self._session.add(existing)
        existing.scheduled_start, existing.scheduled_end = start, end
        existing.meeting_url = meeting
        existing.interviewer_id = interviewer.id if interviewer else existing.interviewer_id
        existing.status = "scheduled"
        self._session.add(
            _audit(
                tenant_id,
                None,
                "interview_scheduled",
                "job_application",
                application.id,
                source="calcom",
                trigger=trigger,
                start=start.isoformat(),
            )  # fmt: skip
        )
        await self._session.commit()
        return "recorded"


def verify_calcom_signature(body: bytes, header: str | None, secret: str | None) -> None:
    if not secret:
        raise PermissionDeniedError("Cal.com webhook is not configured for this workspace")
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not header or not hmac.compare_digest(header.strip(), expected):
        raise PermissionDeniedError("Invalid Cal.com webhook signature")


# --------------------------------------------------------------------------- offers
class OfferService:
    def __init__(self, session: AsyncSession, storage: StorageService) -> None:
        self._session = session
        self._storage = storage

    async def generate(
        self, application_id: uuid.UUID, payload: OfferLetterRequest, *, actor: User
    ) -> OfferLetterRead:
        application = await self._session.get(JobApplication, application_id)
        if application is None:
            raise NotFoundError("Application not found")
        candidate = await self._session.get(Candidate, application.candidate_id)
        job = await self._session.get(Job, application.job_id)
        tenant = await self._session.get(Tenant, application.tenant_id)
        if candidate is None or job is None or tenant is None:  # pragma: no cover - foreign keys
            raise NotFoundError("Application not found")
        name = (
            " ".join(p for p in (candidate.first_name, candidate.last_name) if p) or candidate.email
        )
        title = payload.job_title or job.title
        currency = payload.currency.upper()
        salary = f"{currency} {payload.base_salary:,.2f}"
        today = date.today()
        paragraphs: list[tuple[str, str]] = [
            ("title", f"{tenant.name} — Offer of Employment"),
            ("body", today.strftime("%B %d, %Y")),
            ("body", ""),
            ("body", f"Dear {name},"),
            ("body", ""),
            (
                "body",
                f"We are delighted to offer you the position of {title} at {tenant.name}. "
                "This letter summarises the key terms of our offer.",
            ),  # fmt: skip
            ("body", ""),
            ("heading", "Terms"),
            ("body", f"Position: {title}"),
            ("body", f"Base salary: {salary} per year"),
            ("body", f"Start date: {payload.start_date.strftime('%B %d, %Y')}"),
        ]
        if payload.equity:
            paragraphs.append(("body", f"Equity: {payload.equity}"))
        if payload.additional_terms:
            paragraphs += [
                ("body", ""),
                ("heading", "Additional terms"),
                ("body", payload.additional_terms),
            ]
        respond = (
            payload.respond_by.strftime("%B %d, %Y") if payload.respond_by else "within 7 days"
        )
        paragraphs += [
            ("body", ""),
            (
                "body",
                f"Please confirm your acceptance by {respond}. We are excited about the prospect "
                "of you joining the team.",
            ),  # fmt: skip
            ("body", ""),
            ("body", "Sincerely,"),
            ("body", f"The {tenant.name} hiring team"),
        ]
        pdf = render_text_pdf(paragraphs, title=f"Offer letter — {name}")
        key = f"tenants/{tenant.id}/offers/{application.id}/{uuid.uuid4().hex}.pdf"
        await self._storage.backend.put_bytes(key, pdf, content_type="application/pdf")
        previous = application.offer_letter_s3_key
        application.offer_letter_s3_key = key
        application.offer_details_json = {
            "candidate_name": name,
            "job_title": title,
            "base_salary": f"{payload.base_salary:.2f}",
            "currency": currency,
            "start_date": payload.start_date.isoformat(),
            "equity": payload.equity,
            "respond_by": payload.respond_by.isoformat() if payload.respond_by else None,
            "generated_at": datetime.now(UTC).isoformat(),
            "generated_by": str(actor.id),
        }
        self._session.add(
            _audit(
                tenant.id,
                actor.id,
                "offer_letter_generated",
                "job_application",
                application.id,
                currency=currency,
                start_date=payload.start_date.isoformat(),
            )  # fmt: skip
        )
        await self._session.commit()
        if previous:
            await self._storage.delete(previous)
        return await self.read(application_id)

    async def read(self, application_id: uuid.UUID) -> OfferLetterRead:
        application = await self._session.get(JobApplication, application_id)
        if application is None:
            raise NotFoundError("Application not found")
        if not application.offer_letter_s3_key:
            raise NotFoundError("No offer letter has been generated for this application")
        details = dict(application.offer_details_json)
        return OfferLetterRead(
            application_id=application.id,
            details={k: v for k, v in details.items() if k not in ("generated_at", "generated_by")},
            generated_at=datetime.fromisoformat(details["generated_at"]),
            download_url=await self._storage.get_presigned_download_url(
                application.offer_letter_s3_key
            ),
            expires_in=self._storage.presigned_url_ttl_seconds,
        )


# ------------------------------------------------------------ GDPR Article 17 erasure
class ComplianceService:
    def __init__(self, session: AsyncSession, storage: StorageService) -> None:
        self._session = session
        self._storage = storage

    async def find(self, email: str) -> list[CandidateLookup]:
        rows = (
            await self._session.execute(
                select(Candidate, func.count(JobApplication.id))
                .outerjoin(JobApplication, JobApplication.candidate_id == Candidate.id)
                .where(func.lower(Candidate.email) == email.lower())
                .group_by(Candidate.id)
            )
        ).all()
        return [
            CandidateLookup(
                candidate_id=c.id,
                email=c.email,
                full_name=" ".join(p for p in (c.first_name, c.last_name) if p) or None,
                applications=count,
                created_at=c.created_at,
            )
            for c, count in rows
        ]

    async def forget(self, candidate_id: uuid.UUID, *, actor: User) -> ErasureReport:
        """Erase a candidate and everything derived from them; keep an anonymised audit entry."""
        candidate = await self._session.get(Candidate, candidate_id)
        if candidate is None:
            raise NotFoundError("Candidate not found")
        applications = list(
            (
                await self._session.execute(
                    select(JobApplication).where(JobApplication.candidate_id == candidate_id)
                )
            ).scalars()
        )
        app_ids = [a.id for a in applications]
        keys = {candidate.original_resume_s3_key}
        keys |= {a.resume_s3_key for a in applications}
        keys |= {a.offer_letter_s3_key for a in applications if a.offer_letter_s3_key}
        if app_ids:
            for screening in (
                await self._session.execute(
                    select(Screening).where(Screening.application_id.in_(app_ids))
                )
            ).scalars():
                keys |= {r["audio_key"] for r in screening.responses_json if r.get("audio_key")}

        files_deleted = file_errors = 0
        for key in sorted(k for k in keys if k):
            try:
                await self._storage.delete(key)
                files_deleted += 1
            except Exception:  # noqa: BLE001 - recorded; erasure of DB data must still proceed
                logger.exception("Could not delete %s during erasure", key)
                file_errors += 1

        notifications_deleted = 0
        if app_ids:
            result = await self._session.execute(
                delete(Notification).where(Notification.entity_id.in_([*app_ids, candidate_id]))
            )
            notifications_deleted = result.rowcount
        tenant_id = candidate.tenant_id
        # Cascades remove applications, embeddings, notes, scorecards, screenings, interviews,
        # outbox emails and talent matches (all keyed to the candidate or its applications).
        await self._session.delete(candidate)
        erased_at = datetime.now(UTC)
        self._session.add(
            _audit(
                tenant_id,
                actor.id,
                "candidate_erased",
                "candidate",
                candidate_id,
                legal_basis="GDPR Art. 17",
                applications=len(app_ids),
                files_deleted=files_deleted,
                file_errors=file_errors,
            )  # fmt: skip
        )
        await self._session.commit()
        return ErasureReport(
            candidate_id=candidate_id,
            applications_deleted=len(app_ids),
            files_deleted=files_deleted,
            file_errors=file_errors,
            notifications_deleted=notifications_deleted,
            erased_at=erased_at,
        )


def calcom_secret(tenant: Tenant) -> str | None:
    return (tenant.settings_json.get("communications") or {}).get("calcom_webhook_secret")
