"""Semantic talent-pool search (project.md Module 5): pgvector HNSW + full-text hybrid ranking.

Ranking uses reciprocal rank fusion (RRF, k=60) of two candidate lists, each restricted to the
caller's tenant by Row-Level Security:

* semantic: cosine distance ``embedding <=> query`` over the HNSW index;
* keyword: PostgreSQL ``ts_rank_cd`` over the generated ``search_tsv`` column (an OR of the
  query's terms, so natural-language questions still match on their key words).

RRF rewards profiles that both lists agree on without needing to calibrate the two score scales.
"""

from __future__ import annotations

import re
import uuid
from collections import defaultdict

from pgvector.sqlalchemy import Vector
from sqlalchemy import bindparam, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.audit import AuditLog
from app.models.candidate import Candidate, CandidateEmbedding, JobApplication
from app.models.job import Job
from app.models.user import User
from app.schemas.talent_pool import (
    PastApplication,
    TalentMatch,
    TalentPoolStats,
    TalentSearchRequest,
    TalentSearchResponse,
)
from app.services.embedding_service import Embedder, embed_query

RRF_K = 60
HEADLINE_OPTIONS = (
    'StartSel=«, StopSel=», MaxFragments=2, MinWords=6, MaxWords=18, FragmentDelimiter=" … "'
)
CANDIDATE_POOL = 100  # per-list depth before fusion
_TOKEN = re.compile(r"[a-z0-9]+")

_SEARCH_SQL = text(
    """
    WITH semantic AS (
        SELECT candidate_id,
               row_number() OVER (ORDER BY embedding <=> :query_vector) AS rank
        FROM candidate_embeddings
        ORDER BY embedding <=> :query_vector
        LIMIT :pool
    ),
    keyword AS (
        SELECT candidate_id,
               ts_rank_cd(search_tsv, to_tsquery('english', :ts_query)) AS score,
               row_number() OVER (
                   ORDER BY ts_rank_cd(search_tsv, to_tsquery('english', :ts_query)) DESC
               ) AS rank
        FROM candidate_embeddings
        WHERE :ts_query <> '' AND search_tsv @@ to_tsquery('english', :ts_query)
        ORDER BY score DESC
        LIMIT :pool
    ),
    fused AS (
        SELECT coalesce(s.candidate_id, k.candidate_id) AS candidate_id,
               coalesce(1.0 / (:rrf_k + s.rank), 0) + coalesce(1.0 / (:rrf_k + k.rank), 0)
                   AS hybrid_score,
               k.score AS keyword_rank
        FROM semantic s FULL OUTER JOIN keyword k USING (candidate_id)
    )
    SELECT f.candidate_id,
           f.hybrid_score,
           f.keyword_rank,
           1 - (e.embedding <=> :query_vector) AS similarity,
           CASE WHEN :ts_query <> '' THEN ts_headline(
               'english', e.content_chunk, to_tsquery('english', :ts_query),
               :headline_options
           ) END AS headline
    FROM fused f
    JOIN candidate_embeddings e ON e.candidate_id = f.candidate_id
    ORDER BY f.hybrid_score DESC, similarity DESC
    LIMIT :limit
    """
).bindparams(bindparam("query_vector", type_=Vector(384)))


async def tune_ann(session: AsyncSession) -> None:
    """Widen the HNSW search so RLS post-filtering still returns enough tenant rows."""
    ef_search = int(get_settings().TALENT_POOL_EF_SEARCH)
    await session.execute(text(f"SET LOCAL hnsw.ef_search = {ef_search}"))
    version = await session.scalar(
        text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
    )
    if version and tuple(int(p) for p in version.split(".")[:2]) >= (0, 8):
        # pgvector 0.8+: keep scanning the graph until LIMIT rows survive the filter.
        await session.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))


def build_ts_query(query: str) -> str:
    """OR together the query's alphanumeric terms (safe for to_tsquery; stop words drop out)."""
    terms = dict.fromkeys(token for token in _TOKEN.findall(query.lower()) if len(token) > 1)
    return " | ".join(terms)


def _matched_skills(skills: list[str], query: str) -> list[str]:
    lowered = f" {query.lower()} "
    tokens = set(_TOKEN.findall(query.lower()))
    matched = []
    for skill in skills:
        name = skill.lower()
        if (
            name in tokens
            or (len(name) > 2 and f" {name} " in lowered)
            or (" " in name and name in lowered)
        ):
            matched.append(skill)
    return matched


class TalentPoolService:
    def __init__(self, session: AsyncSession, embedder: Embedder) -> None:
        self._session = session
        self._embedder = embedder

    async def stats(self) -> TalentPoolStats:
        candidates = await self._session.scalar(select(func.count()).select_from(Candidate))
        indexed = await self._session.scalar(select(func.count()).select_from(CandidateEmbedding))
        return TalentPoolStats(
            model=self._embedder.model_name,
            dimensions=self._embedder.dimensions,
            candidates=candidates or 0,
            indexed=indexed or 0,
        )

    async def _tune_ann(self) -> None:
        await tune_ann(self._session)

    async def search(self, request: TalentSearchRequest, *, actor: User) -> TalentSearchResponse:
        vector = await embed_query(self._embedder, request.query)
        ts_query = build_ts_query(request.query)
        await self._tune_ann()
        rows = (
            await self._session.execute(
                _SEARCH_SQL,
                {
                    "query_vector": vector,
                    "ts_query": ts_query,
                    "pool": CANDIDATE_POOL,
                    "rrf_k": RRF_K,
                    "headline_options": HEADLINE_OPTIONS,
                    "limit": request.limit,
                },
            )
        ).all()
        if request.min_similarity is not None:
            rows = [row for row in rows if row.similarity >= request.min_similarity]

        candidate_ids = [row.candidate_id for row in rows]
        candidates = {
            c.id: c
            for c in (
                await self._session.execute(
                    select(Candidate).where(Candidate.id.in_(candidate_ids))
                )
            ).scalars()
        }
        applications: dict[uuid.UUID, list[PastApplication]] = defaultdict(list)
        for application, job_title in (
            await self._session.execute(
                select(JobApplication, Job.title)
                .join(Job, Job.id == JobApplication.job_id)
                .where(JobApplication.candidate_id.in_(candidate_ids))
                .order_by(JobApplication.applied_at.desc())
            )
        ).all():
            score = application.deterministic_match_score
            applications[application.candidate_id].append(
                PastApplication(
                    application_id=application.id,
                    job_id=application.job_id,
                    job_title=job_title,
                    status=application.status,
                    deterministic_match_score=float(score) if score is not None else None,
                    applied_at=application.applied_at,
                )
            )

        results = []
        for row in rows:
            candidate = candidates.get(row.candidate_id)
            if candidate is None:
                continue
            profile = candidate.parsed_profile_json
            skills = [
                s
                for s in [*profile.get("verified_skills", []), *profile.get("skills", [])]
                if isinstance(s, str)
            ]
            skills = list(dict.fromkeys(skills))
            full_name = " ".join(p for p in (candidate.first_name, candidate.last_name) if p)
            results.append(
                TalentMatch(
                    candidate_id=candidate.id,
                    full_name=full_name or None,
                    email=candidate.email,
                    location=candidate.location,
                    similarity=round(float(row.similarity), 4),
                    keyword_rank=round(float(row.keyword_rank), 4) if row.keyword_rank else None,
                    hybrid_score=round(float(row.hybrid_score), 6),
                    matched_skills=_matched_skills(skills, request.query),
                    skills=skills,
                    highlights=[h for h in (row.headline or "").split(" … ") if "«" in h],
                    applications=applications.get(candidate.id, []),
                )
            )

        self._session.add(
            AuditLog(
                tenant_id=actor.tenant_id,
                actor_id=actor.id,
                action="talent_pool_searched",
                entity_type="talent_pool",
                entity_id=actor.tenant_id,
                metadata_json={"query": request.query[:500], "results": len(results)},
            )
        )
        stats = await self.stats()
        await self._session.commit()
        return TalentSearchResponse(
            query=request.query,
            model=self._embedder.model_name,
            total_indexed=stats.indexed,
            results=results,
        )
