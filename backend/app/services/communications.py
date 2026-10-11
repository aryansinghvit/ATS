"""Candidate communications (project.md Module 7): templates, outbox, polite-delay rejections.

Every candidate email goes through ``email_outbox``. Rows are rendered at *send* time (so template
edits and corrected candidate data apply) and delivered by the communications worker. Rejections
are never sent immediately: they are scheduled 24–72 h out (tenant setting, default 48 h) and then
moved into business hours (Mon–Fri 09:00–17:00 in the tenant's timezone). If the candidate leaves
the Rejected stage before that, the email is cancelled.
"""

from __future__ import annotations

import asyncio
import logging
import re
import smtplib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from email.message import EmailMessage
from functools import lru_cache
from typing import Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import AsyncSessionLocal, set_tenant_context
from app.core.exceptions import NotFoundError, UnprocessableError
from app.models.candidate import Candidate, JobApplication
from app.models.collaboration import EmailOutbox, EmailStatus, EmailTemplate
from app.models.job import Job, PipelineStage
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.communications import (
    CommunicationSettings,
    TemplatePreview,
    TemplateRead,
    TemplateUpdate,
)

logger = logging.getLogger(__name__)

VARIABLE_PATTERN = re.compile(r"{{\s*([A-Za-z_][A-Za-z0-9_]*)\s*}}")
ALLOWED_VARIABLES = (
    "candidate_name",
    "candidate_first_name",
    "job_title",
    "company_name",
    "scheduling_link",
    "job_link",
    "stage_name",
    "screening_link",
)
NO_SCHEDULING_LINK = "our recruiting team will reach out shortly to find a time"
BUSINESS_START, BUSINESS_END = time(9, 0), time(17, 0)


@dataclass(frozen=True, slots=True)
class TemplateSpec:
    name: str
    description: str
    trigger: str
    subject: str
    body: str


TEMPLATES: dict[str, TemplateSpec] = {
    "application_received": TemplateSpec(
        "Application received",
        "Sent right after a candidate submits an application.",
        "Application submitted",
        "We received your application for {{job_title}}",
        "Hi {{candidate_first_name}},\n\nThank you for applying for the {{job_title}} role at "
        "{{company_name}}. Our team reviews every application personally, and we will be in touch "
        "about next steps.\n\nBest regards,\nThe {{company_name}} recruiting team",
    ),
    "interview_invitation": TemplateSpec(
        "Invitation to interview",
        "Sent when a candidate enters an Interview stage.",
        "Moved to Interview",
        "Interview invitation: {{job_title}} at {{company_name}}",
        "Hi {{candidate_first_name}},\n\nWe enjoyed learning about your background and would love "
        "to invite you to interview for the {{job_title}} role.\n\nPlease choose a time that works "
        "for you: {{scheduling_link}}\n\nLooking forward to speaking with you,\n"
        "The {{company_name}} recruiting team",
    ),
    "offer_extended": TemplateSpec(
        "Offer extended",
        "Sent when a candidate enters the Offer stage.",
        "Moved to Offer",
        "Good news from {{company_name}}",
        "Hi {{candidate_first_name}},\n\nWe are delighted to let you know that we would like to "
        "offer you the {{job_title}} role. A member of our team will contact you shortly with the "
        "details.\n\nCongratulations,\nThe {{company_name}} team",
    ),
    "rejection": TemplateSpec(
        "Polite rejection (delayed)",
        "Queued when a candidate enters Rejected; delivered after the polite delay, in business "
        "hours, and cancelled if the candidate is moved out of Rejected first.",
        "Moved to Rejected",
        "Your application for {{job_title}} at {{company_name}}",
        "Hi {{candidate_first_name}},\n\nThank you for the time and interest you invested in the "
        "{{job_title}} role. After careful consideration we have decided to move forward with "
        "other candidates whose experience more closely matches our current needs.\n\nWe were "
        "glad to get to know you and will keep your profile in mind for future opportunities.\n\n"
        "Warm regards,\nThe {{company_name}} recruiting team",
    ),
    "screening_invitation": TemplateSpec(
        "Pre-screening invitation",
        "Sent when a candidate is invited to the asynchronous screening portal.",
        "Moved to Pre-Screen / invited",
        "A few quick questions about the {{job_title}} role",
        "Hi {{candidate_first_name}},\n\nThanks again for applying to {{company_name}}. As a next "
        "step, please answer a few short questions about your experience — it takes about 10 "
        "minutes, there is no login, and you can reply in writing or by recording audio:\n\n"
        "{{screening_link}}\n\nThe link is personal, works once, and expires in 72 hours.\n\n"
        "Best regards,\nThe {{company_name}} recruiting team",
    ),
    "talent_invitation": TemplateSpec(
        "Talent pool invitation",
        "Sent when a recruiter invites a past applicant to a new role.",
        "Invited from talent pool",
        "A new role at {{company_name}} you might like: {{job_title}}",
        "Hi {{candidate_first_name}},\n\nYou applied to {{company_name}} before, and we think your "
        "experience is a great fit for a new {{job_title}} role. We have added you to the "
        "candidate list, so there is nothing more to submit; you can read about the role here: "
        "{{job_link}}\n\nBest regards,\nThe {{company_name}} recruiting team",
    ),
}


def template_variables(text_: str) -> list[str]:
    return list(dict.fromkeys(VARIABLE_PATTERN.findall(text_)))


def validate_template(subject: str, body: str) -> None:
    unknown = sorted(
        set(template_variables(subject) + template_variables(body)) - set(ALLOWED_VARIABLES)
    )
    if unknown:
        raise UnprocessableError(
            f"Unknown template variables: {', '.join(unknown)}. "
            f"Allowed: {', '.join('{{' + v + '}}' for v in ALLOWED_VARIABLES)}"
        )


def render(text_: str, context: dict[str, str], *, single_line: bool = False) -> str:
    rendered = VARIABLE_PATTERN.sub(lambda m: context.get(m.group(1), ""), text_)
    return " ".join(rendered.split()) if single_line else rendered


def polite_send_time(now: datetime, delay_hours: int, timezone: str) -> datetime:
    """``now + delay``, moved forward into the next Mon–Fri 09:00–17:00 window (tenant time)."""
    tz = ZoneInfo(timezone)
    local = (now + timedelta(hours=delay_hours)).astimezone(tz)
    while True:
        if local.weekday() >= 5:  # Saturday / Sunday -> next day 09:00
            local = datetime.combine(local.date() + timedelta(days=1), BUSINESS_START, tz)
        elif local.time() < BUSINESS_START:
            local = datetime.combine(local.date(), BUSINESS_START, tz)
        elif local.time() >= BUSINESS_END:
            local = datetime.combine(local.date() + timedelta(days=1), BUSINESS_START, tz)
        else:
            return local.astimezone(UTC)


# --------------------------------------------------------------------------- delivery
class EmailSender(Protocol):
    async def send(self, *, to: str, subject: str, body: str, reply_to: str | None) -> None: ...


class LogEmailSender:
    """Development/test transport: logs and keeps delivered messages in memory."""

    def __init__(self) -> None:
        self.sent: list[dict[str, str | None]] = []

    async def send(self, *, to: str, subject: str, body: str, reply_to: str | None) -> None:
        self.sent.append({"to": to, "subject": subject, "body": body, "reply_to": reply_to})
        logger.info("EMAIL to=%s subject=%r", to, subject)


class SmtpEmailSender:
    def __init__(self) -> None:
        self._settings = get_settings()

    def _send_sync(self, message: EmailMessage) -> None:
        settings = self._settings
        with smtplib.SMTP(settings.SMTP_HOST or "", settings.SMTP_PORT, timeout=30) as smtp:
            if settings.SMTP_STARTTLS:
                smtp.starttls()
            if settings.SMTP_USERNAME and settings.SMTP_PASSWORD:
                smtp.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD.get_secret_value())
            smtp.send_message(message)

    async def send(self, *, to: str, subject: str, body: str, reply_to: str | None) -> None:
        message = EmailMessage()
        message["From"] = self._settings.EMAIL_FROM
        message["To"] = to
        message["Subject"] = subject
        if reply_to:
            message["Reply-To"] = reply_to
        message.set_content(body)
        await asyncio.to_thread(self._send_sync, message)


@lru_cache
def get_email_sender() -> EmailSender:
    return SmtpEmailSender() if get_settings().EMAIL_BACKEND == "smtp" else LogEmailSender()


# ---------------------------------------------------------------------------- service
class CommunicationService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # settings ------------------------------------------------------------------------
    async def _tenant(self, tenant_id: uuid.UUID) -> Tenant:
        tenant = await self._session.get(Tenant, tenant_id)
        if tenant is None:
            raise NotFoundError("Workspace not found")
        return tenant

    async def get_settings(self, tenant_id: uuid.UUID) -> CommunicationSettings:
        tenant = await self._tenant(tenant_id)
        stored = tenant.settings_json.get("communications") or {}
        defaults = {"rejection_delay_hours": get_settings().DEFAULT_REJECTION_DELAY_HOURS}
        known = {k: v for k, v in stored.items() if k in CommunicationSettings.model_fields}
        return CommunicationSettings.model_validate({**defaults, **known})

    async def update_settings(
        self, tenant_id: uuid.UUID, payload: CommunicationSettings
    ) -> CommunicationSettings:
        tenant = await self._tenant(tenant_id)
        stored = tenant.settings_json.get("communications") or {}
        tenant.settings_json = {
            **tenant.settings_json,
            # Keep keys owned by other endpoints (e.g. the write-only Cal.com webhook secret).
            "communications": {**stored, **payload.model_dump(mode="json")},
        }
        await self._session.commit()
        return payload

    async def set_calcom_secret(self, tenant_id: uuid.UUID, secret: str | None) -> bool:
        tenant = await self._tenant(tenant_id)
        stored = dict(tenant.settings_json.get("communications") or {})
        if secret:
            stored["calcom_webhook_secret"] = secret
        else:
            stored.pop("calcom_webhook_secret", None)
        tenant.settings_json = {**tenant.settings_json, "communications": stored}
        await self._session.commit()
        return bool(secret)

    # templates -----------------------------------------------------------------------
    async def _overrides(self) -> dict[str, EmailTemplate]:
        return {t.key: t for t in (await self._session.execute(select(EmailTemplate))).scalars()}

    @staticmethod
    def _spec(key: str) -> TemplateSpec:
        try:
            return TEMPLATES[key]
        except KeyError:
            raise NotFoundError(f"Unknown email template '{key}'") from None

    def _read(self, key: str, override: EmailTemplate | None) -> TemplateRead:
        spec = self._spec(key)
        subject = override.subject if override else spec.subject
        body = override.body if override else spec.body
        return TemplateRead(
            key=key,
            name=spec.name,
            description=spec.description,
            trigger=spec.trigger,
            subject=subject,
            body=body,
            enabled=override.enabled if override else True,
            customized=override is not None,
            variables=template_variables(subject + "\n" + body),
        )

    async def templates(self) -> list[TemplateRead]:
        overrides = await self._overrides()
        return [self._read(key, overrides.get(key)) for key in TEMPLATES]

    async def template(self, key: str) -> TemplateRead:
        self._spec(key)
        return self._read(key, (await self._overrides()).get(key))

    async def update_template(
        self, key: str, payload: TemplateUpdate, *, tenant_id: uuid.UUID, actor: User
    ) -> TemplateRead:
        self._spec(key)
        validate_template(payload.subject, payload.body)
        override = (await self._overrides()).get(key)
        if override is None:
            override = EmailTemplate(tenant_id=tenant_id, key=key, subject="", body="")
            self._session.add(override)
        override.subject = payload.subject
        override.body = payload.body
        override.enabled = payload.enabled
        override.updated_by = actor.id
        await self._session.commit()
        return self._read(key, override)

    async def reset_template(self, key: str) -> TemplateRead:
        override = (await self._overrides()).get(key)
        if override is not None:
            await self._session.delete(override)
            await self._session.commit()
        return self._read(key, None)

    # rendering context ---------------------------------------------------------------
    async def context_for(self, application: JobApplication) -> dict[str, str]:
        candidate = await self._session.get(Candidate, application.candidate_id)
        job = await self._session.get(Job, application.job_id)
        tenant = await self._tenant(application.tenant_id)
        stage = (
            await self._session.get(PipelineStage, application.current_stage_id)
            if application.current_stage_id
            else None
        )
        if candidate is None or job is None:  # pragma: no cover - guaranteed by foreign keys
            raise NotFoundError("Application not found")
        settings = await self.get_settings(application.tenant_id)
        full_name = " ".join(p for p in (candidate.first_name, candidate.last_name) if p)
        scheduling = job.scheduling_url or (
            str(settings.scheduling_url) if settings.scheduling_url else None
        )
        frontend = get_settings().FRONTEND_URL.rstrip("/")
        return {
            "candidate_name": full_name or "there",
            "candidate_first_name": candidate.first_name or "there",
            "job_title": job.title,
            "company_name": tenant.name,
            "scheduling_link": scheduling or NO_SCHEDULING_LINK,
            "job_link": f"{frontend}/careers/{tenant.subdomain}/{job.slug}",
            "stage_name": stage.name if stage else "",
            "screening_link": await self._screening_link(application.id),
        }

    async def _screening_link(self, application_id: uuid.UUID) -> str:
        from app.models.collaboration import Screening, ScreeningStatus
        from app.services.screening_portal import portal_link

        screening = (
            await self._session.execute(
                select(Screening)
                .where(
                    Screening.application_id == application_id,
                    Screening.status.in_([ScreeningStatus.PENDING, ScreeningStatus.OPENED]),
                    Screening.invited_at.is_not(None),
                )
                .order_by(Screening.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return portal_link(screening) if screening else ""

    async def preview(
        self,
        key: str,
        *,
        subject: str | None,
        body: str | None,
        application_id: uuid.UUID | None,
        tenant_id: uuid.UUID,
    ) -> TemplatePreview:
        current = await self.template(key)
        subject = subject if subject is not None else current.subject
        body = body if body is not None else current.body
        validate_template(subject, body)
        if application_id is not None:
            application = await self._session.get(JobApplication, application_id)
            if application is None:
                raise NotFoundError("Application not found")
            context = await self.context_for(application)
        else:
            tenant = await self._tenant(tenant_id)
            settings = await self.get_settings(tenant_id)
            context = {
                "candidate_name": "Alex Mercer",
                "candidate_first_name": "Alex",
                "job_title": "Senior Backend Engineer",
                "company_name": tenant.name,
                "scheduling_link": str(settings.scheduling_url)
                if settings.scheduling_url
                else NO_SCHEDULING_LINK,
                "job_link": f"{get_settings().FRONTEND_URL.rstrip('/')}/careers/{tenant.subdomain}",
                "stage_name": "Interview",
                "screening_link": (
                    f"{get_settings().FRONTEND_URL.rstrip('/')}/screen/<personal-link>"
                ),
            }
        return TemplatePreview(
            subject=render(subject, context, single_line=True),
            body=render(body, context),
            context=context,
        )

    # outbox --------------------------------------------------------------------------
    async def queue(
        self,
        application: JobApplication,
        template_key: str,
        *,
        trigger: str,
        scheduled_for: datetime | None = None,
    ) -> EmailOutbox | None:
        """Stage an outbox row in the caller's transaction; skips disabled templates/duplicates."""
        self._spec(template_key)
        override = (
            await self._session.execute(
                select(EmailTemplate).where(EmailTemplate.key == template_key)
            )
        ).scalar_one_or_none()
        if override is not None and not override.enabled:
            return None
        duplicate = await self._session.scalar(
            select(EmailOutbox.id).where(
                EmailOutbox.application_id == application.id,
                EmailOutbox.template_key == template_key,
                EmailOutbox.status == EmailStatus.QUEUED,
            )
        )
        if duplicate is not None:
            return None
        candidate = await self._session.get(Candidate, application.candidate_id)
        if candidate is None:  # pragma: no cover - foreign key
            return None
        email = EmailOutbox(
            tenant_id=application.tenant_id,
            application_id=application.id,
            template_key=template_key,
            trigger=trigger,
            to_email=candidate.email,
            status=EmailStatus.QUEUED,
            scheduled_for=scheduled_for or datetime.now(UTC),
        )
        self._session.add(email)
        return email

    async def queue_polite_rejection(self, application: JobApplication) -> EmailOutbox | None:
        settings = await self.get_settings(application.tenant_id)
        send_at = polite_send_time(
            datetime.now(UTC), settings.rejection_delay_hours, settings.timezone
        )
        return await self.queue(
            application, "rejection", trigger="stage:rejected", scheduled_for=send_at
        )

    async def cancel_queued(
        self, application_id: uuid.UUID, template_keys: set[str], reason: str
    ) -> int:
        result = await self._session.execute(
            update(EmailOutbox)
            .where(
                EmailOutbox.application_id == application_id,
                EmailOutbox.template_key.in_(template_keys),
                EmailOutbox.status == EmailStatus.QUEUED,
            )
            .values(status=EmailStatus.CANCELLED, last_error=reason)
            .execution_options(synchronize_session=False)
        )
        return result.rowcount

    async def outbox_for(self, application_id: uuid.UUID) -> list[EmailOutbox]:
        return list(
            (
                await self._session.execute(
                    select(EmailOutbox)
                    .where(EmailOutbox.application_id == application_id)
                    .order_by(EmailOutbox.created_at)
                )
            ).scalars()
        )


# -------------------------------------------------------------------------- dispatcher
async def _deliver_one(sender: EmailSender, tenant_id: uuid.UUID, email_id: uuid.UUID) -> str:
    settings = get_settings()
    async with AsyncSessionLocal() as session:
        await set_tenant_context(session, tenant_id)
        email = (
            await session.execute(
                select(EmailOutbox)
                .where(EmailOutbox.id == email_id, EmailOutbox.status == EmailStatus.QUEUED)
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if email is None:
            return "skipped"
        application = (
            await session.get(JobApplication, email.application_id)
            if email.application_id
            else None
        )
        service = CommunicationService(session)
        if application is None:
            email.status, email.last_error = EmailStatus.CANCELLED, "Application no longer exists"
            await session.commit()
            return "cancelled"
        if email.template_key == "rejection":
            stage = (
                await session.get(PipelineStage, application.current_stage_id)
                if application.current_stage_id
                else None
            )
            if stage is None or stage.stage_type != "rejected":  # dignity guard at send time
                email.status = EmailStatus.CANCELLED
                email.last_error = "Candidate is no longer in a Rejected stage"
                await session.commit()
                return "cancelled"
        template = await service.template(email.template_key)
        if not template.enabled:
            email.status, email.last_error = EmailStatus.CANCELLED, "Template disabled"
            await session.commit()
            return "cancelled"
        context = await service.context_for(application)
        subject = render(template.subject, context, single_line=True)[:300]
        body = render(template.body, context)
        reply_to = (await service.get_settings(tenant_id)).reply_to
        try:
            await sender.send(to=email.to_email, subject=subject, body=body, reply_to=reply_to)
        except Exception as exc:  # noqa: BLE001 - transport errors are recorded and retried
            email.attempts += 1
            email.last_error = f"{exc.__class__.__name__}: {exc}"[:1000]
            if email.attempts >= settings.EMAIL_MAX_ATTEMPTS:
                email.status = EmailStatus.FAILED
            else:
                email.scheduled_for = datetime.now(UTC) + timedelta(minutes=5 * email.attempts)
            await session.commit()
            return "failed"
        email.status = EmailStatus.SENT
        email.sent_at = datetime.now(UTC)
        email.subject, email.body = subject, body
        email.attempts += 1
        await session.commit()
        return "sent"


async def dispatch_due_emails(sender: EmailSender, *, limit: int = 100) -> dict[str, int]:
    """Deliver every queued email whose time has come, across tenants."""
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text("SELECT tenant_id, email_id FROM app_due_emails(:limit)"), {"limit": limit}
            )
        ).all()
    counts: dict[str, int] = {}
    for row in rows:
        outcome = await _deliver_one(sender, row.tenant_id, row.email_id)
        counts[outcome] = counts.get(outcome, 0) + 1
    return counts
