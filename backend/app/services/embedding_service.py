"""Candidate profile synthesis + local FastEmbed embeddings (project.md Module 5).

Embeddings are computed on-device with FastEmbed (ONNX, ``BAAI/bge-small-en-v1.5``, 384-d,
L2-normalised), so cosine similarity is the vector dot product and no profile data leaves the box.

The synthesis deliberately excludes names, contact details and location: semantic matching should
be driven by skills and experience only (§8.2 bias mitigation).
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import threading
import uuid
from datetime import date
from functools import lru_cache
from typing import Any, Literal, Protocol

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import ServiceError
from app.models.candidate import Candidate, CandidateEmbedding, JobApplication

_YEAR_MONTH = re.compile(r"(?P<year>(19|20)\d{2})(?:-(?P<month>\d{1,2}))?")
MAX_SYNTHESIS_CHARS = 4000


class EmbeddingUnavailableError(ServiceError):
    status_code = 503


class Embedder(Protocol):
    model_name: str
    dimensions: int

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class FastEmbedEmbedder:
    """Lazily loads the ONNX model once per process (thread-safe)."""

    def __init__(
        self,
        model_name: str,
        *,
        dimensions: int,
        cache_dir: str,
        query_instruction: str = "",
        threads: int | None = None,
        model_path: str | None = None,
    ) -> None:
        self.model_name = model_name
        self.dimensions = dimensions
        self._cache_dir = cache_dir
        self._query_instruction = query_instruction
        self._threads = threads
        self._model_path = model_path
        self._model: Any = None
        self._lock = threading.Lock()

    def _load(self) -> Any:
        with self._lock:
            if self._model is None:
                from fastembed import TextEmbedding

                try:
                    extra = {"specific_model_path": self._model_path} if self._model_path else {}
                    self._model = TextEmbedding(
                        self.model_name, cache_dir=self._cache_dir, threads=self._threads, **extra
                    )
                except Exception as exc:  # download/IO failures surface as one clear error
                    raise EmbeddingUnavailableError(
                        f"Embedding model {self.model_name} is not available yet; retry shortly"
                    ) from exc
        return self._model

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        vectors = [vector.tolist() for vector in self._load().embed(texts)]
        for vector in vectors:
            if len(vector) != self.dimensions:
                raise RuntimeError(f"{self.model_name} returned {len(vector)}-d vectors")
        return vectors

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([f"{self._query_instruction}{text}"])[0]


@lru_cache
def get_embedder() -> Embedder:
    settings = get_settings()
    return FastEmbedEmbedder(
        settings.EMBEDDING_MODEL,
        dimensions=settings.EMBEDDING_DIM,
        cache_dir=str(settings.embedding_cache_dir),
        query_instruction=settings.EMBEDDING_QUERY_INSTRUCTION,
        threads=settings.EMBEDDING_THREADS,
        model_path=settings.EMBEDDING_MODEL_PATH,
    )


async def embed_query(embedder: Embedder, text: str) -> list[float]:
    """CPU-bound inference runs in a worker thread so the event loop stays responsive."""
    return await asyncio.to_thread(embedder.embed_query, text)


# ---------------------------------------------------------------------------- synthesis
def _strings(value: Any) -> list[str]:
    return (
        [item.strip() for item in value if isinstance(item, str) and item.strip()]
        if (isinstance(value, list))
        else []
    )


def _month_index(raw: Any, *, today: date) -> int | None:
    if isinstance(raw, str) and raw.strip().lower() in {"present", "current", "now"}:
        return today.year * 12 + today.month
    match = _YEAR_MONTH.search(str(raw or ""))
    if not match:
        return None
    return int(match["year"]) * 12 + int(match["month"] or 1)


def estimate_experience_years(work_experience: Any, *, today: date | None = None) -> float | None:
    """Total span of dated roles in years (overlaps merged); None when no role has dates."""
    today = today or date.today()
    spans = []
    for role in work_experience if isinstance(work_experience, list) else []:
        if not isinstance(role, dict):
            continue
        start = _month_index(role.get("start_date"), today=today)
        end = _month_index(role.get("end_date") or "present", today=today)
        if start is not None and end is not None and end >= start:
            spans.append((start, end))
    if not spans:
        return None
    spans.sort()
    total, (current_start, current_end) = 0, spans[0]
    for start, end in spans[1:]:
        if start <= current_end:
            current_end = max(current_end, end)
        else:
            total += current_end - current_start
            current_start, current_end = start, end
    total += current_end - current_start
    return round(total / 12, 1)


def synthesize_candidate_profile(
    candidate: Candidate,
    parsed_profile: dict[str, Any],
    *,
    verified_skills: list[str] | None = None,
) -> str:
    """Dense, PII-free text representation of a candidate for embedding and keyword search."""
    roles = [r for r in parsed_profile.get("work_experience") or [] if isinstance(r, dict)]
    skills: list[str] = []
    for skill in [*(verified_skills or []), *_strings(parsed_profile.get("skills"))]:
        if skill.lower() not in {s.lower() for s in skills}:
            skills.append(skill)
    technologies: list[str] = []
    for role in roles:
        for tech in _strings(role.get("technologies")):
            if tech.lower() not in {t.lower() for t in [*skills, *technologies]}:
                technologies.append(tech)

    lines = []
    if roles and roles[0].get("title"):
        lines.append(f"Title: {roles[0]['title']}")
    if isinstance(parsed_profile.get("summary"), str) and parsed_profile["summary"].strip():
        lines.append(f"Summary: {parsed_profile['summary'].strip()}")
    years = estimate_experience_years(roles)
    if years:
        lines.append(f"Experience: {years:g} years")
    if skills:
        lines.append(f"Primary Skills: {', '.join(skills)}")
    if technologies:
        lines.append(f"Technologies: {', '.join(technologies)}")
    for role in roles:
        header = " at ".join(part for part in (role.get("title"), role.get("company")) if part)
        highlights = "; ".join(_strings(role.get("highlights")))
        if header or highlights:
            lines.append(f"Role: {header}" + (f" — {highlights}" if highlights else ""))
    for project in parsed_profile.get("projects") or []:
        if not isinstance(project, dict):
            continue
        parts = [
            project.get("name"),
            project.get("description"),
            *_strings(project.get("highlights")),
        ]
        text = ": ".join(p for p in parts[:2] if p) + "".join(f"; {h}" for h in parts[2:])
        tech = ", ".join(_strings(project.get("technologies")))
        if text or tech:
            lines.append(f"Project: {text}" + (f" ({tech})" if tech else ""))
    for school in parsed_profile.get("education") or []:
        if isinstance(school, dict) and (school.get("degree") or school.get("institution")):
            lines.append(
                "Education: "
                + ", ".join(p for p in (school.get("degree"), school.get("institution")) if p)
            )
    return "\n".join(lines)[:MAX_SYNTHESIS_CHARS]


def content_hash(text: str, model: str) -> str:
    return hashlib.sha256(f"{model}\x1f{text}".encode()).hexdigest()


# ----------------------------------------------------------------------------- indexing
IndexOutcome = Literal["indexed", "unchanged", "empty", "missing"]


class EmbeddingService:
    def __init__(self, session: AsyncSession, embedder: Embedder) -> None:
        self._session = session
        self._embedder = embedder

    async def _source_profile(self, candidate: Candidate) -> dict[str, Any]:
        if candidate.parsed_profile_json.get(
            "work_experience"
        ) or candidate.parsed_profile_json.get("skills"):
            return candidate.parsed_profile_json
        # Fall back to the most recent extraction from any of the candidate's applications.
        latest = (
            await self._session.execute(
                select(JobApplication.extracted_profile_json)
                .where(
                    JobApplication.candidate_id == candidate.id,
                    JobApplication.extracted_profile_json != {},
                )
                .order_by(JobApplication.applied_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return latest or {}

    async def index_candidate(self, candidate_id: uuid.UUID) -> IndexOutcome:
        candidate = await self._session.get(Candidate, candidate_id)
        if candidate is None:
            return "missing"
        profile = await self._source_profile(candidate)
        text = synthesize_candidate_profile(
            candidate,
            profile,
            verified_skills=_strings(candidate.parsed_profile_json.get("verified_skills")),
        )
        if not text:
            return "empty"
        digest = content_hash(text, self._embedder.model_name)
        existing = (
            await self._session.execute(
                select(CandidateEmbedding.content_hash).where(
                    CandidateEmbedding.candidate_id == candidate_id
                )
            )
        ).scalar_one_or_none()
        if existing == digest:
            return "unchanged"

        vector = (await asyncio.to_thread(self._embedder.embed_documents, [text]))[0]
        statement = insert(CandidateEmbedding).values(
            tenant_id=candidate.tenant_id,
            candidate_id=candidate.id,
            content_chunk=text,
            content_hash=digest,
            model=self._embedder.model_name,
            embedding=vector,
        )
        await self._session.execute(
            statement.on_conflict_do_update(
                index_elements=[CandidateEmbedding.candidate_id],
                set_={
                    "content_chunk": statement.excluded.content_chunk,
                    "content_hash": statement.excluded.content_hash,
                    "model": statement.excluded.model,
                    "embedding": statement.excluded.embedding,
                    "updated_at": statement.excluded.updated_at,
                },
            )
        )
        await self._session.commit()
        return "indexed"
