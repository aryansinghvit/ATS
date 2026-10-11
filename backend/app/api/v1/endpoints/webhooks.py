"""Inbound webhooks. The M3 extractor authenticates with an HMAC-SHA256 body signature."""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.core.exceptions import UnprocessableError
from app.schemas.webhook import ExtractorWebhookPayload
from app.services.embedding_queue import EmbeddingQueue, get_embedding_queue
from app.services.events import EventBroker, get_event_broker
from app.services.webhook_service import ExtractorWebhookService, verify_signature

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/extractor")
async def extractor_webhook(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    broker: Annotated[EventBroker, Depends(get_event_broker)],
    embedding_queue: Annotated[EmbeddingQueue, Depends(get_embedding_queue)],
) -> dict[str, Any]:
    """Receive parsed profile + deterministic match score for one application."""
    body = await request.body()
    verify_signature(body, request.headers.get("x-webhook-signature"))
    try:
        payload = ExtractorWebhookPayload.model_validate_json(body)
    except ValidationError as exc:
        raise UnprocessableError(
            f"Invalid extractor payload: {exc.error_count()} error(s)"
        ) from None

    request.state.tenant_id = payload.callback_context.tenant_id
    outcome, card = await ExtractorWebhookService(session, broker).apply(payload)
    if outcome == "processed":  # new parsed profile -> refresh the talent-pool embedding
        try:
            await embedding_queue.enqueue_embedding(
                payload.callback_context.tenant_id, payload.callback_context.candidate_id
            )
        except Exception:  # noqa: BLE001 - search freshness must not fail the webhook
            logger.exception(
                "Could not queue embedding for %s", payload.callback_context.candidate_id
            )
    return {
        "status": outcome,
        "application_id": str(payload.callback_context.application_id),
        "deterministic_match_score": card.deterministic_match_score if card else None,
    }
