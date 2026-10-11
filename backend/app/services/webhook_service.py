"""Applies M3 extractor webhook results to candidates and applications (project.md §6.3)."""

from __future__ import annotations

import hashlib
import hmac
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import set_tenant_context
from app.core.exceptions import ConflictError, NotFoundError, ServiceError
from app.models.candidate import ApplicationStatus, Candidate, JobApplication
from app.schemas.pipeline import PipelineCard
from app.schemas.webhook import ExtractorWebhookPayload
from app.services.events import EventBroker
from app.services.pipeline_service import PipelineService

SIGNATURE_PREFIX = "sha256="


class WebhookAuthError(ServiceError):
    status_code = 401


class WebhookNotConfiguredError(ServiceError):
    status_code = 503


def sign_payload(body: bytes, secret: str) -> str:
    return SIGNATURE_PREFIX + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify_signature(body: bytes, header: str | None) -> None:
    """Constant-time HMAC-SHA256 check of the raw request body (fails closed)."""
    secret = get_settings().M3_WEBHOOK_SECRET.get_secret_value()
    if not secret:
        raise WebhookNotConfiguredError("Extractor webhook secret is not configured")
    if not header:
        raise WebhookAuthError("Missing X-Webhook-Signature header")
    expected = sign_payload(body, secret)
    provided = header.strip()
    if not provided.startswith(SIGNATURE_PREFIX):
        provided = SIGNATURE_PREFIX + provided
    if not hmac.compare_digest(provided, expected):
        raise WebhookAuthError("Invalid webhook signature")


def _split_name(full_name: Any) -> tuple[str | None, str | None]:
    parts = full_name.split() if isinstance(full_name, str) else []
    if not parts:
        return None, None
    if len(parts) == 1:
        return parts[0][:100], None
    return " ".join(parts[:-1])[:100], parts[-1][:100]


def _text(value: Any, limit: int) -> str | None:
    return value.strip()[:limit] if isinstance(value, str) and value.strip() else None


class ExtractorWebhookService:
    def __init__(self, session: AsyncSession, broker: EventBroker | None = None) -> None:
        self._session = session
        self._pipeline = PipelineService(session, broker)

    async def apply(self, payload: ExtractorWebhookPayload) -> tuple[str, PipelineCard | None]:
        context = payload.callback_context
        # The HMAC signature authenticates the callback context, so its tenant can be bound.
        await set_tenant_context(self._session, context.tenant_id)
        application = await self._session.get(JobApplication, context.application_id)
        if application is None or application.candidate_id != context.candidate_id:
            raise NotFoundError("Application not found")
        if (
            application.extractor_tracking_id
            and application.extractor_tracking_id != payload.tracking_id
        ):
            raise ConflictError("Tracking id does not match this application's extraction job")
        if application.match_breakdown_json or application.extraction_error:
            return "duplicate", None  # webhook redelivery: already applied

        application.extractor_tracking_id = payload.tracking_id
        if payload.succeeded:
            assert payload.deterministic_match is not None
            match = payload.deterministic_match
            application.deterministic_match_score = Decimal(str(match.overall_score)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            application.match_breakdown_json = {
                "overall_score": match.overall_score,
                "breakdown": match.breakdown.model_dump(mode="json"),
                "experience_match": match.experience_match,
                "execution_metrics": payload.execution_metrics or {},
            }
            application.extracted_profile_json = payload.candidate_profile or {}
            candidate = await self._session.get(Candidate, application.candidate_id)
            if candidate is not None:
                self._merge_candidate(candidate, application, payload.candidate_profile or {})
            outcome = "processed"
        else:
            application.extraction_error = (
                payload.error or f"Extraction finished with status '{payload.status}'"
            )[:1000]
            outcome = "failed"

        if application.status is ApplicationStatus.PENDING_EXTRACTION:
            application.status = ApplicationStatus.APPLIED
        await self._session.commit()

        card = await self._pipeline.card(application.id)
        await self._pipeline.publish(card, "card_updated")
        return outcome, card

    @staticmethod
    def _merge_candidate(
        candidate: Candidate, application: JobApplication, profile: dict[str, Any]
    ) -> None:
        """Fill gaps only: extraction never overwrites data a person entered or verified.

        The full parsed profile replaces the candidate's profile only when this application's
        resume is the candidate's primary resume (or none was parsed yet).
        """
        if candidate.original_resume_s3_key == application.resume_s3_key or not (
            candidate.parsed_profile_json
        ):
            candidate.parsed_profile_json = profile
        contact = profile.get("contact") or {}
        first, last = _split_name(contact.get("full_name"))
        fills = {
            "first_name": first,
            "last_name": last,
            "phone": _text(contact.get("phone"), 50),
            "linkedin_url": _text(contact.get("linkedin"), 500),
            "github_url": _text(contact.get("github"), 500),
            "location": _text(contact.get("location"), 150),
        }
        for field, value in fills.items():
            if value and not getattr(candidate, field):
                setattr(candidate, field, value)
