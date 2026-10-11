"""HTTP client for the external M3 Resume Extraction microservice (project.md Section 6).

The ATS shares no code with the extractor: it speaks the documented REST contract only.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from app.core.config import Settings

TRANSIENT_STATUS_CODES = frozenset({429, 502, 503, 504})


class M3TransientError(Exception):
    """Busy, unreachable or timed out: safe to retry with backoff."""


class M3PermanentError(Exception):
    """The extractor rejected the request; retrying the same payload will not help."""


class M3ExtractorClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout_seconds: float = 30.0,
        health_path: str = "/health",
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._api_key = api_key
        self._health_path = health_path
        self._http = http_client or httpx.AsyncClient(timeout=timeout_seconds)

    @classmethod
    def from_settings(cls, settings: Settings) -> M3ExtractorClient:
        return cls(
            settings.M3_EXTRACTOR_URL,
            settings.M3_EXTRACTOR_API_KEY.get_secret_value(),
            timeout_seconds=settings.M3_REQUEST_TIMEOUT_SECONDS,
            health_path=settings.M3_HEALTH_PATH,
        )

    async def submit_extraction(
        self,
        *,
        file_name: str,
        file_bytes: bytes,
        mime_type: str,
        job_description: dict[str, Any],
        webhook_url: str,
        callback_context: dict[str, str],
    ) -> str:
        """``POST /api/v1/resumes/extract`` (async webhook mode); returns the tracking id."""
        files = {
            "file": (file_name, file_bytes, mime_type),
            "job_description": (None, json.dumps(job_description), "application/json"),
            "webhook_url": (None, webhook_url),
            "callback_context": (None, json.dumps(callback_context), "application/json"),
        }
        try:
            response = await self._http.post(
                f"{self._base}/api/v1/resumes/extract",
                files=files,
                headers={"X-API-Key": self._api_key},
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise M3TransientError(f"M3 extractor unreachable: {exc.__class__.__name__}") from exc

        if response.status_code in TRANSIENT_STATUS_CODES or response.status_code >= 500:
            raise M3TransientError(f"M3 extractor returned HTTP {response.status_code}")
        if response.status_code not in (200, 202):
            raise M3PermanentError(
                f"M3 extractor rejected the resume (HTTP {response.status_code}): "
                f"{response.text[:300]}"
            )
        try:
            tracking_id = response.json()["tracking_id"]
        except (ValueError, KeyError, TypeError) as exc:
            raise M3PermanentError("M3 extractor response is missing a tracking_id") from exc
        return str(tracking_id)

    async def is_healthy(self) -> bool:
        try:
            response = await self._http.get(f"{self._base}{self._health_path}", timeout=5.0)
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    async def aclose(self) -> None:
        await self._http.aclose()
