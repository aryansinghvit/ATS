"""Local LLM access (Ollama) for structured JSON generation.

The client is injected through ``get_llm_client`` so tests can substitute a deterministic fake.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any, Protocol

import httpx

from app.core.config import get_settings
from app.core.exceptions import ServiceError


class LLMUnavailableError(ServiceError):
    status_code = 503


class LLMOutputError(ServiceError):
    status_code = 502


class LLMClient(Protocol):
    model: str

    async def generate_json(
        self, *, system: str, prompt: str, schema: dict[str, Any]
    ) -> dict[str, Any]: ...


class OllamaClient:
    """Calls Ollama's /api/chat with a JSON-schema ``format`` (structured outputs)."""

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        timeout_seconds: float = 120.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.model = model
        self._url = f"{base_url.rstrip('/')}/api/chat"
        self._http = http_client or httpx.AsyncClient(timeout=timeout_seconds)

    async def generate_json(
        self, *, system: str, prompt: str, schema: dict[str, Any]
    ) -> dict[str, Any]:
        payload = {
            "model": self.model,
            "stream": False,
            "format": schema,
            "options": {"temperature": 0.2},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        }
        try:
            response = await self._http.post(self._url, json=payload)
        except httpx.HTTPError as exc:
            raise LLMUnavailableError(
                f"Local LLM (Ollama) is unreachable: {exc.__class__.__name__}"
            ) from exc
        if response.status_code != 200:
            raise LLMUnavailableError(
                f"Local LLM (Ollama) returned HTTP {response.status_code}: {response.text[:200]}"
            )
        try:
            content = response.json()["message"]["content"]
            result = json.loads(content)
        except (KeyError, TypeError, ValueError) as exc:
            raise LLMOutputError("Local LLM returned malformed JSON") from exc
        if not isinstance(result, dict):
            raise LLMOutputError("Local LLM returned JSON that is not an object")
        return result

    async def aclose(self) -> None:
        await self._http.aclose()


@lru_cache
def get_llm_client() -> LLMClient:
    settings = get_settings()
    return OllamaClient(
        settings.OLLAMA_URL, settings.OLLAMA_MODEL, timeout_seconds=settings.OLLAMA_TIMEOUT_SECONDS
    )
