"""Public (no-login) candidate ingestion gateway: presign -> direct upload -> submit -> SSE."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import bind_tenant_by_subdomain
from app.core.config import get_settings
from app.core.database import get_session, set_tenant_context
from app.schemas.application import (
    ConfirmProfileRequest,
    ConfirmProfileResponse,
    PresignUploadRequest,
    PresignUploadResponse,
    SubmitApplicationRequest,
    SubmitApplicationResponse,
)
from app.services.application_service import (
    ApplicationService,
    load_application_event,
    verify_status_token,
)
from app.services.embedding_queue import EmbeddingQueue, get_embedding_queue
from app.services.events import EventBroker, get_event_broker
from app.services.extraction_queue import ExtractionQueue, get_extraction_queue
from app.services.pipeline_service import PipelineService
from app.services.storage import StorageService, get_storage_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/public/applications", tags=["public", "applications"])

Session = Annotated[AsyncSession, Depends(get_session)]
Storage = Annotated[StorageService, Depends(get_storage_service)]
Queue = Annotated[ExtractionQueue, Depends(get_extraction_queue)]
Broker = Annotated[EventBroker, Depends(get_event_broker)]


@router.post("/presign-upload", response_model=PresignUploadResponse)
async def presign_upload(
    payload: PresignUploadRequest, request: Request, session: Session, storage: Storage
) -> PresignUploadResponse:
    """Pre-signed URL (15-minute TTL) for uploading a PDF/DOCX resume directly to storage."""
    tenant_id = await bind_tenant_by_subdomain(request, session, payload.tenant_subdomain)
    upload = await ApplicationService(session, storage).presign_upload(payload, tenant_id=tenant_id)
    return PresignUploadResponse(
        upload_url=upload.url,
        method="PUT",
        headers=upload.headers,
        s3_key=upload.key,
        expires_in=upload.expires_in,
        max_file_size_bytes=upload.max_size_bytes,
    )


@router.post(
    "/submit", response_model=SubmitApplicationResponse, status_code=status.HTTP_202_ACCEPTED
)
async def submit_application(
    payload: SubmitApplicationRequest,
    request: Request,
    session: Session,
    storage: Storage,
    queue: Queue,
    broker: Broker,
) -> SubmitApplicationResponse:
    """Record the application as ``pending_extraction`` and queue it for the M3 extractor."""
    tenant_id = await bind_tenant_by_subdomain(request, session, payload.tenant_subdomain)
    result = await ApplicationService(session, storage).submit(payload, tenant_id=tenant_id)
    application_id = result.application.id
    try:
        await queue.enqueue_extraction(tenant_id, application_id)
    except Exception:  # noqa: BLE001 - the application is safely stored; the sweeper retries
        logger.exception("Could not enqueue extraction for application %s", application_id)
    pipeline = PipelineService(session, broker)
    await pipeline.publish(await pipeline.card(application_id), "card_updated")
    return SubmitApplicationResponse(
        application_id=application_id,
        status=result.application.status,
        status_token=result.status_token,
        events_url=str(
            request.url_for(
                "application_events", application_id=application_id
            ).include_query_params(token=result.status_token)
        ),
    )


@router.post("/{application_id}/confirm", response_model=ConfirmProfileResponse)
async def confirm_application(
    application_id: uuid.UUID,
    token: str,
    payload: ConfirmProfileRequest,
    request: Request,
    session: Session,
    storage: Storage,
    broker: Broker,
    embedding_queue: Annotated[EmbeddingQueue, Depends(get_embedding_queue)],
) -> ConfirmProfileResponse:
    """Final step: the candidate verifies the profile extracted from their resume."""
    tenant_id = verify_status_token(application_id, token)
    request.state.tenant_id = tenant_id
    await set_tenant_context(session, tenant_id)
    application = await ApplicationService(session, storage).confirm(application_id, payload)
    try:  # verified skills change the candidate's searchable profile
        await embedding_queue.enqueue_embedding(tenant_id, application.candidate_id)
    except Exception:  # noqa: BLE001 - search freshness must not fail the confirmation
        logger.exception("Could not queue embedding for application %s", application_id)
    pipeline = PipelineService(session, broker)
    await pipeline.publish(await pipeline.card(application_id), "card_updated")
    return ConfirmProfileResponse(
        application_id=application.id, status=application.status, confirmed=True
    )


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


@router.get("/{application_id}/events", name="application_events")
async def application_events(
    application_id: uuid.UUID, token: str, request: Request
) -> StreamingResponse:
    """Server-Sent Events: ``status`` events (processing -> parsed) for the applicant."""
    tenant_id = verify_status_token(application_id, token)
    settings = get_settings()

    async def stream() -> AsyncIterator[str]:
        deadline = time.monotonic() + settings.APPLICATION_EVENTS_MAX_SECONDS
        last: dict | None = None
        yield "retry: 5000\n\n"
        while True:
            event = await load_application_event(tenant_id, application_id)
            if event is None:
                yield _sse("error", {"detail": "Application not found"})
                return
            if event != last:
                yield _sse("status", event)
                last = event
            if event["status"] != "processing" or time.monotonic() >= deadline:
                return
            if await request.is_disconnected():
                return
            await asyncio.sleep(settings.APPLICATION_EVENTS_POLL_SECONDS)
            yield ": keep-alive\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
