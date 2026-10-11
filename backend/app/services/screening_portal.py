"""Candidate-facing asynchronous screening portal (project.md Module 6, task.md Prompt 6.3).

Access tokens are ``{tenant}.{screening}.{expiry}.{hmac}``; the HMAC also covers a random
per-screening nonce stored in ``screenings.access_token``. No raw token is ever stored, emails
can rebuild the link at send time, and re-inviting rotates the nonce (old links stop working).
Links are single-use: once answers are submitted — or the 72 h window passes — they return 410.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import signing
from app.core.config import get_settings
from app.core.database import set_tenant_context
from app.core.exceptions import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ServiceError,
    UnprocessableError,
)
from app.models.audit import AuditLog
from app.models.candidate import ApplicationStatus, Candidate, JobApplication
from app.models.collaboration import Screening, ScreeningStatus
from app.models.job import Job
from app.models.tenant import Tenant
from app.schemas.screening import (
    PortalAnswer,
    PortalLimits,
    PortalQuestion,
    PortalView,
)
from app.services.storage import StorageService

TOKEN_PURPOSE = "screening-access"
AUDIO_MIME_TYPES = {
    "audio/webm": "webm",
    "audio/ogg": "ogg",
    "audio/mp4": "m4a",
    "audio/mpeg": "mp3",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
}


class ScreeningGoneError(ServiceError):
    status_code = 410


def new_nonce() -> str:
    return secrets.token_urlsafe(32)


def build_token(screening: Screening) -> str:
    expires = int(screening.expires_at.timestamp())
    signature = signing.sign(
        TOKEN_PURPOSE, screening.tenant_id, screening.id, expires, screening.access_token
    )
    return f"{screening.tenant_id}.{screening.id}.{expires}.{signature}"


def portal_link(screening: Screening) -> str:
    return f"{get_settings().FRONTEND_URL.rstrip('/')}/screen/{build_token(screening)}"


def _parse(token: str) -> tuple[uuid.UUID, uuid.UUID, int, str]:
    try:
        tenant_raw, screening_raw, expires_raw, signature = token.split(".")
        return uuid.UUID(tenant_raw), uuid.UUID(screening_raw), int(expires_raw), signature
    except ValueError:
        raise PermissionDeniedError("This screening link is invalid") from None


def _answers(screening: Screening) -> dict[int, dict[str, Any]]:
    return {int(r["index"]): r for r in screening.responses_json if "index" in r}


class ScreeningPortalService:
    def __init__(self, session: AsyncSession, storage: StorageService) -> None:
        self._session = session
        self._storage = storage

    async def resolve(self, token: str, *, for_update: bool = False) -> Screening:
        tenant_id, screening_id, expires, signature = _parse(token)
        await set_tenant_context(self._session, tenant_id)  # unverified until the HMAC passes
        screening = await self._session.get(Screening, screening_id, with_for_update=for_update)
        if screening is None or not signing.verify(
            signature, TOKEN_PURPOSE, tenant_id, screening_id, expires, screening.access_token
        ):
            raise PermissionDeniedError("This screening link is invalid")
        if expires != int(screening.expires_at.timestamp()):
            raise PermissionDeniedError("This screening link has been replaced by a newer one")
        if screening.status is ScreeningStatus.COMPLETED:
            raise ScreeningGoneError("These answers were already submitted. Thank you!")
        if screening.status is ScreeningStatus.EXPIRED or screening.expires_at <= datetime.now(UTC):
            if screening.status is not ScreeningStatus.EXPIRED:
                screening.status = ScreeningStatus.EXPIRED
                await self._session.commit()
            raise ScreeningGoneError("This screening link has expired")
        return screening

    async def view(self, token: str) -> PortalView:
        screening = await self.resolve(token)
        if screening.status is ScreeningStatus.PENDING:
            screening.status = ScreeningStatus.OPENED
            screening.opened_at = datetime.now(UTC)
            await self._session.commit()
        application = await self._session.get(JobApplication, screening.application_id)
        if application is None:  # pragma: no cover - foreign key
            raise NotFoundError("Screening not found")
        job = await self._session.get(Job, application.job_id)
        tenant = await self._session.get(Tenant, screening.tenant_id)
        candidate = await self._session.get(Candidate, application.candidate_id)
        settings = get_settings()
        return PortalView(
            status=screening.status,
            company_name=tenant.name if tenant else "",
            logo_url=tenant.logo_url if tenant else None,
            primary_color=tenant.primary_color if tenant else "#2563EB",
            job_title=job.title if job else "",
            candidate_first_name=candidate.first_name if candidate else None,
            expires_at=screening.expires_at,
            # Targets/rationale are recruiter-only: candidates see just the questions.
            questions=[
                PortalQuestion(index=i, question=q["question"])
                for i, q in enumerate(screening.questions_json)
            ],
            answers=[
                self._answer(a)
                for a in sorted(_answers(screening).values(), key=lambda a: a["index"])
            ],
            limits=PortalLimits(
                max_audio_seconds=settings.SCREENING_MAX_AUDIO_SECONDS,
                max_audio_bytes=settings.SCREENING_MAX_AUDIO_BYTES,
                max_text_chars=settings.SCREENING_MAX_TEXT_CHARS,
            ),
        )

    @staticmethod
    def _answer(entry: dict[str, Any]) -> PortalAnswer:
        return PortalAnswer(
            index=entry["index"],
            type=entry["type"],
            text=entry.get("text"),
            has_audio=bool(entry.get("audio_key")),
            audio_bytes=entry.get("audio_bytes"),
            updated_at=datetime.fromisoformat(entry["updated_at"]),
        )

    def _check_index(self, screening: Screening, index: int) -> None:
        if not 0 <= index < len(screening.questions_json):
            raise NotFoundError("Question not found")

    async def _store(self, screening: Screening, entry: dict[str, Any]) -> PortalAnswer:
        answers = _answers(screening)
        previous = answers.get(entry["index"])
        if (
            previous
            and previous.get("audio_key")
            and previous["audio_key"] != entry.get("audio_key")
        ):
            await self._storage.delete(previous["audio_key"])  # replaced or switched to text
        answers[entry["index"]] = entry
        screening.responses_json = sorted(answers.values(), key=lambda a: a["index"])
        if screening.status is ScreeningStatus.PENDING:
            screening.status = ScreeningStatus.OPENED
            screening.opened_at = datetime.now(UTC)
        await self._session.commit()
        return self._answer(entry)

    async def save_text(self, token: str, index: int, text: str) -> PortalAnswer:
        screening = await self.resolve(token, for_update=True)
        self._check_index(screening, index)
        return await self._store(
            screening,
            {
                "index": index,
                "type": "text",
                "text": text,
                "updated_at": datetime.now(UTC).isoformat(),
            },
        )

    async def save_audio(self, token: str, index: int, data: bytes, mime_type: str) -> PortalAnswer:
        screening = await self.resolve(token, for_update=True)
        self._check_index(screening, index)
        base_mime = mime_type.split(";")[0].strip().lower()
        extension = AUDIO_MIME_TYPES.get(base_mime)
        if extension is None:
            raise UnprocessableError("Unsupported audio format; record again in a modern browser")
        limit = get_settings().SCREENING_MAX_AUDIO_BYTES
        if not data or len(data) > limit:
            raise UnprocessableError(
                f"Recordings must be between 1 byte and {limit // (1024 * 1024)} MB"
            )
        key = (
            f"tenants/{screening.tenant_id}/screenings/{screening.id}/"
            f"{index}-{uuid.uuid4().hex}.{extension}"
        )
        await self._storage.backend.put_bytes(key, data, content_type=base_mime)
        return await self._store(
            screening,
            {
                "index": index,
                "type": "audio",
                "text": None,
                "audio_key": key,
                "audio_mime": base_mime,
                "audio_bytes": len(data),
                "updated_at": datetime.now(UTC).isoformat(),
            },
        )

    async def submit(self, token: str) -> Screening:
        screening = await self.resolve(token, for_update=True)
        answers = _answers(screening)
        missing = [
            i + 1
            for i in range(len(screening.questions_json))
            if not (
                (
                    answers.get(i, {}).get("type") == "text"
                    and (answers[i].get("text") or "").strip()
                )
                or (answers.get(i, {}).get("type") == "audio" and answers[i].get("audio_key"))
            )
        ]
        if missing:
            raise UnprocessableError(
                f"Please answer question(s) {', '.join(map(str, missing))} before submitting"
            )
        screening.status = ScreeningStatus.COMPLETED
        screening.submitted_at = datetime.now(UTC)
        screening.ai_evaluation_json = {
            **screening.ai_evaluation_json,
            "evaluation_status": "pending",
        }
        application = await self._session.get(JobApplication, screening.application_id)
        if application is not None and application.status in (
            ApplicationStatus.APPLIED,
            ApplicationStatus.SCREENING_INVITED,
        ):
            application.status = (
                ApplicationStatus.SCREENING_COMPLETED
            )  # tracking only, never a decision
        self._session.add(
            AuditLog(
                tenant_id=screening.tenant_id,
                actor_id=None,
                action="screening_submitted",
                entity_type="job_application",
                entity_id=screening.application_id,
                metadata_json={
                    "screening_id": str(screening.id),
                    "answers": {
                        t: sum(1 for a in answers.values() if a["type"] == t)
                        for t in ("text", "audio")
                    },
                },
            )
        )
        await self._session.commit()
        return screening


async def issue_invitation(session: AsyncSession, screening: Screening) -> None:
    """Rotate the link (72 h, fresh nonce) and mark the candidate as invited (no commit)."""
    screening.access_token = new_nonce()
    screening.expires_at = datetime.now(UTC) + timedelta(
        hours=get_settings().SCREENING_TOKEN_TTL_HOURS
    )
    screening.invited_at = datetime.now(UTC)
    if screening.status is ScreeningStatus.EXPIRED:
        screening.status = ScreeningStatus.PENDING
    application = await session.get(JobApplication, screening.application_id)
    if application is not None and application.status is ApplicationStatus.APPLIED:
        application.status = ApplicationStatus.SCREENING_INVITED


def ensure_open(screening: Screening) -> None:
    if screening.status is ScreeningStatus.COMPLETED:
        raise ConflictError("The candidate already submitted this screening")
