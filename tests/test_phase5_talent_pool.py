"""Phase 5 — L5 HR Tester: semantic talent pool (FastEmbed + pgvector HNSW + hybrid search).

Strict testing mandate: headless Python script tests only (pytest + httpx.AsyncClient).
No web/browser testing.

Pipeline mechanics run with a deterministic hashing embedder (no model download needed). The
ranking-quality tests use the real on-device BAAI/bge-small-en-v1.5 model and are skipped only
when its ONNX files are not present locally.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import REPO_ROOT, get_settings
from app.core.database import AsyncSessionLocal, set_tenant_context
from app.models import Candidate, CandidateEmbedding, UserRole
from app.services.embedding_service import (
    EmbeddingService,
    FastEmbedEmbedder,
    estimate_experience_years,
    get_embedder,
    synthesize_candidate_profile,
)
from app.services.talent_pool_service import build_ts_query
from app.workers import embedding_worker
from tests.helpers import (
    API,
    GO_TAXONOMY,
    HashingEmbedder,
    Workspace,
    apply,
    create_job,
    m3_payload,
    post_webhook,
    provision_workspace,
)

ADMIN, RECRUITER, HIRING_MANAGER, INTERVIEWER = (
    UserRole.ADMIN,
    UserRole.RECRUITER,
    UserRole.HIRING_MANAGER,
    UserRole.INTERVIEWER,
)
GO_QUERY = "Find me engineers skilled in distributed systems and Go"
MODEL_DIR = REPO_ROOT / ".cache" / "fastembed" / "bge-small-en-v1.5-onnx-Q"


def _role(title: str, company: str, start: str, end: str, highlights: list[str], tech: list[str]):
    return {
        "title": title,
        "company": company,
        "start_date": start,
        "end_date": end,
        "highlights": highlights,
        "technologies": tech,
    }


# Ten distinct technology stacks (task.md Prompt 5.4).
TALENT: list[dict[str, Any]] = [
    {
        "name": "Gopher Raft",
        "stack": "go",
        "skills": ["Go", "Raft", "etcd", "gRPC", "Kubernetes"],
        "roles": [
            _role(
                "Staff Backend Engineer",
                "Ledgerly",
                "2018-01",
                "Present",
                [
                    "Built a multi-region Raft consensus cluster for a payments ledger",
                    "Designed fault-tolerant distributed storage in Go",
                ],
                ["Go", "Raft", "gRPC"],
            )
        ],
        "projects": [
            {
                "name": "raftkv",
                "description": "Distributed key-value store implementing Raft consensus in Go",
            }
        ],
    },
    {
        "name": "Gina Stream",
        "stack": "go",
        "skills": ["Go", "Kafka", "Distributed Systems", "PostgreSQL"],
        "roles": [
            _role(
                "Senior Software Engineer",
                "StreamCo",
                "2017-03",
                "Present",
                [
                    "Scaled a distributed event-streaming platform in Go to 2M msgs/sec",
                    "Owned partitioning and replication for Kafka consumers",
                ],
                ["Go", "Kafka"],
            )
        ],
        "projects": [],
    },
    {
        "name": "Gary Mesh",
        "stack": "go",
        "skills": ["Go", "Microservices", "Consul", "Distributed Tracing"],
        "roles": [
            _role(
                "Backend Engineer",
                "MeshWorks",
                "2019-06",
                "Present",
                ["Wrote a Go service mesh control plane for distributed microservices"],
                ["Go", "Envoy"],
            )
        ],
        "projects": [],
    },
    {
        "name": "Rita Pixel",
        "stack": "frontend",
        "skills": ["React", "TypeScript", "CSS", "Figma"],
        "roles": [
            _role(
                "Senior Frontend Engineer",
                "Pixelate",
                "2016-01",
                "Present",
                ["Built a design system and accessible React component library"],
                ["React", "TypeScript"],
            )
        ],
        "projects": [],
    },
    {
        "name": "Remy Vue",
        "stack": "frontend",
        "skills": ["Vue.js", "JavaScript", "Tailwind CSS"],
        "roles": [
            _role(
                "Frontend Developer",
                "Shoply",
                "2020-02",
                "Present",
                ["Implemented responsive storefront UI and animations"],
                ["Vue.js"],
            )
        ],
        "projects": [],
    },
    {
        "name": "Dev Terra",
        "stack": "devops",
        "skills": ["Terraform", "AWS", "Kubernetes", "CI/CD"],
        "roles": [
            _role(
                "DevOps Engineer",
                "Cloudly",
                "2017-05",
                "Present",
                ["Automated infrastructure as code with Terraform across 40 AWS accounts"],
                ["Terraform", "AWS"],
            )
        ],
        "projects": [],
    },
    {
        "name": "Ops Helm",
        "stack": "devops",
        "skills": ["Kubernetes", "Helm", "Prometheus", "Linux"],
        "roles": [
            _role(
                "Site Reliability Engineer",
                "Uptime Inc",
                "2018-09",
                "Present",
                ["Ran Kubernetes clusters, on-call, Prometheus alerting and incident response"],
                ["Kubernetes", "Helm"],
            )
        ],
        "projects": [],
    },
    {
        "name": "Mia Tensor",
        "stack": "ml",
        "skills": ["Python", "PyTorch", "Machine Learning", "NLP"],
        "roles": [
            _role(
                "Machine Learning Engineer",
                "DeepLabs",
                "2019-01",
                "Present",
                ["Trained transformer NLP models and deployed PyTorch inference"],
                ["Python", "PyTorch"],
            )
        ],
        "projects": [],
    },
    {
        "name": "Leo Pandas",
        "stack": "ml",
        "skills": ["Python", "Pandas", "scikit-learn", "Data Science"],
        "roles": [
            _role(
                "Data Scientist",
                "Insightful",
                "2018-04",
                "Present",
                ["Built churn prediction models with scikit-learn and pandas"],
                ["Python", "scikit-learn"],
            )
        ],
        "projects": [],
    },
    {
        "name": "Fran Stack",
        "stack": "fullstack",
        "skills": ["Node.js", "React", "REST APIs", "PostgreSQL", "GraphQL"],
        "roles": [
            _role(
                "Full-Stack Engineer",
                "Apiary",
                "2017-07",
                "Present",
                [
                    "Designed and built robust REST and GraphQL APIs in Node.js",
                    "Shipped React dashboards on top of those APIs",
                ],
                ["Node.js", "React"],
            )
        ],
        "projects": [],
    },
]


def _real_model_available() -> bool:
    required = ("model_optimized.onnx", "tokenizer.json", "config.json", "special_tokens_map.json")
    return all(
        (MODEL_DIR / name).is_file() and (MODEL_DIR / name).stat().st_size > 0 for name in required
    )


@pytest.fixture(scope="module")
async def workspaces(api_app: object) -> AsyncIterator[tuple[Workspace, Workspace]]:
    transport = httpx.ASGITransport(app=api_app)  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield await provision_workspace(http, "Orion"), await provision_workspace(http, "Nebula")


@pytest.fixture
def alpha(workspaces: tuple[Workspace, Workspace]) -> Workspace:
    return workspaces[0]


@pytest.fixture
def beta(workspaces: tuple[Workspace, Workspace]) -> Workspace:
    return workspaces[1]


@pytest.fixture
def use_embedder(api_app: Any) -> Iterator[Any]:
    def install(embedder: Any) -> Any:
        api_app.dependency_overrides[get_embedder] = lambda: embedder
        return embedder

    yield install
    api_app.dependency_overrides.pop(get_embedder, None)


async def seed_talent(
    client: httpx.AsyncClient, admin_engine: AsyncEngine, workspace: Workspace, profiles=TALENT
) -> dict[str, dict[str, str]]:
    """Each profile applies through the dropzone and is parsed by a signed M3 webhook."""
    job = await create_job(client, workspace, title="General Application", taxonomy=GO_TAXONOMY)
    seeded = {}
    for profile in profiles:
        slug = profile["name"].lower().replace(" ", ".")
        app = await apply(
            client,
            admin_engine,
            workspace,
            job["id"],
            email=f"{slug}.{uuid.uuid4().hex[:4]}@example.com",
        )
        payload = m3_payload(app, full_name=profile["name"])
        payload["candidate_profile"] = {
            "contact": {
                "full_name": profile["name"],
                "location": "Lisbon, PT",
                "phone": "+351 555",
            },
            "skills": profile["skills"],
            "work_experience": profile["roles"],
            "projects": profile["projects"],
            "education": [
                {"degree": "B.Sc. Computer Science", "institution": "IST", "graduation_year": 2015}
            ],
        }
        assert (await post_webhook(client, payload)).status_code == 200
        seeded[profile["name"]] = {**app, "stack": profile["stack"]}
    return seeded


async def index_all(workspace: Workspace, seeded: dict[str, dict[str, str]], embedder: Any) -> None:
    async with AsyncSessionLocal() as session:
        await set_tenant_context(session, workspace.tenant_id)
        service = EmbeddingService(session, embedder)
        for app in seeded.values():
            assert await service.index_candidate(uuid.UUID(app["candidate_id"])) == "indexed"


async def search(
    client: httpx.AsyncClient, workspace: Workspace, query: str, role=RECRUITER, **extra: Any
) -> httpx.Response:
    return await client.post(
        f"{API}/talent-pool/search", json={"query": query, **extra}, headers=workspace.headers(role)
    )


# =============================================================================================
# Profile synthesis
# =============================================================================================
class TestProfileSynthesis:
    def test_synthesis_is_dense_and_pii_free(self) -> None:
        candidate = Candidate(
            first_name="Ada", last_name="Lovelace", email="ada@example.com",
            phone="+44 20 0000", location="London", parsed_profile_json={},
        )  # fmt: skip
        profile = {
            "contact": {"full_name": "Ada Lovelace", "email": "ada@example.com"},
            "summary": "Engineer who loves compilers.",
            "skills": ["Go", "Rust", "go"],
            "work_experience": TALENT[0]["roles"],
            "projects": TALENT[0]["projects"],
            "education": [{"degree": "B.Sc.", "institution": "Cambridge", "graduation_year": 1840}],
        }
        text_ = synthesize_candidate_profile(
            candidate, profile, verified_skills=["Distributed Systems"]
        )
        assert text_.splitlines()[0] == "Title: Staff Backend Engineer"
        assert "Primary Skills: Distributed Systems, Go, Rust" in text_  # verified first, deduped
        assert "Role: Staff Backend Engineer at Ledgerly — Built a multi-region Raft" in text_
        assert "Project: raftkv: Distributed key-value store" in text_
        assert "Education: B.Sc., Cambridge" in text_
        for pii in ("Ada", "Lovelace", "ada@example.com", "London", "+44", "1840"):
            assert pii not in text_, pii

    def test_experience_years_merge_overlapping_roles(self) -> None:
        roles = [
            {"start_date": "2015-01", "end_date": "2018-01"},
            {"start_date": "2017-01", "end_date": "2019-01"},  # overlaps the first
            {"start_date": "2020-01", "end_date": "Present"},
        ]
        assert estimate_experience_years(roles, today=date(2026, 1, 1)) == 10.0
        assert estimate_experience_years([{"title": "No dates"}]) is None

    def test_ts_query_is_sanitised_or_query(self) -> None:
        assert (
            build_ts_query("Go & C++ engineers; DROP TABLE x--") == "go | engineers | drop | table"
        )
        assert build_ts_query("!!") == ""


# =============================================================================================
# Embedding pipeline + pgvector storage (deterministic embedder)
# =============================================================================================
class TestEmbeddingPipeline:
    async def test_embeddings_are_stored_in_pgvector_with_hnsw_index(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, alpha: Workspace
    ) -> None:
        seeded = await seed_talent(client, admin_engine, alpha)
        await index_all(alpha, seeded, HashingEmbedder())

        ids = [app["candidate_id"] for app in seeded.values()]
        async with admin_engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT tenant_id, vector_dims(embedding) AS dims, model, content_chunk, "
                        "search_tsv IS NOT NULL AS has_tsv FROM candidate_embeddings "
                        "WHERE candidate_id = ANY(CAST(:ids AS uuid[]))"
                    ),
                    {"ids": ids},
                )
            ).all()
            await conn.execute(text("SET enable_seqscan = off"))
            plan = "\n".join(
                (
                    await conn.execute(
                        text(
                            "EXPLAIN SELECT candidate_id FROM candidate_embeddings "
                            "ORDER BY embedding <=> "
                            "(SELECT embedding FROM candidate_embeddings LIMIT 1) "
                            "LIMIT 5"
                        )
                    )
                ).scalars()
            )
        assert len(rows) == 10
        assert {row.dims for row in rows} == {384}
        assert {row.tenant_id for row in rows} == {alpha.tenant_id}
        assert {row.model for row in rows} == {"test-hashing-384"}
        assert all(row.has_tsv for row in rows)
        assert all("Lisbon" not in row.content_chunk for row in rows)  # location excluded
        assert "idx_candidate_embeddings_hnsw" in plan

    async def test_reindexing_is_idempotent_and_tracks_profile_changes(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, alpha: Workspace
    ) -> None:
        seeded = await seed_talent(client, admin_engine, alpha, profiles=TALENT[:1])
        candidate_id = uuid.UUID(next(iter(seeded.values()))["candidate_id"])
        embedder = HashingEmbedder()
        async with AsyncSessionLocal() as session:
            await set_tenant_context(session, alpha.tenant_id)
            service = EmbeddingService(session, embedder)
            assert await service.index_candidate(candidate_id) == "indexed"
            assert await service.index_candidate(candidate_id) == "unchanged"
            first = await session.scalar(
                select(CandidateEmbedding.content_hash).where(
                    CandidateEmbedding.candidate_id == candidate_id
                )
            )
            candidate = await session.get(Candidate, candidate_id)
            candidate.parsed_profile_json = {
                **candidate.parsed_profile_json,
                "verified_skills": ["Rust"],
            }
            await session.commit()
            assert await service.index_candidate(candidate_id) == "indexed"
            second = await session.scalar(
                select(CandidateEmbedding.content_hash).where(
                    CandidateEmbedding.candidate_id == candidate_id
                )
            )
            count = await session.scalar(
                select(func.count())
                .select_from(CandidateEmbedding)
                .where(CandidateEmbedding.candidate_id == candidate_id)
            )
            assert await service.index_candidate(uuid.uuid4()) == "missing"
        assert first != second and count == 1

    async def test_profile_events_queue_embeddings_and_worker_indexes(
        self,
        client: httpx.AsyncClient,
        admin_engine: AsyncEngine,
        alpha: Workspace,
        embedding_queue: Any,
    ) -> None:
        job = await create_job(client, alpha, taxonomy=GO_TAXONOMY)
        app = await apply(client, admin_engine, alpha, job["id"])
        before = len(embedding_queue.jobs)
        await post_webhook(client, m3_payload(app))
        assert (alpha.tenant_id, uuid.UUID(app["candidate_id"])) in embedding_queue.jobs[before:]

        confirmed = await client.post(
            f"{API}/public/applications/{app['application_id']}/confirm",
            params={"token": app["status_token"]},
            json={"full_name": "Alex Mercer", "email": "alex.p5@example.com", "skills": ["Go"]},
        )
        assert confirmed.status_code == 200
        assert embedding_queue.jobs[-1] == (alpha.tenant_id, uuid.UUID(app["candidate_id"]))

        result = await embedding_worker.embed_candidate(
            {"embedder": HashingEmbedder()}, str(alpha.tenant_id), app["candidate_id"]
        )
        assert result == "indexed"

    async def test_embeddings_are_tenant_isolated(
        self,
        client: httpx.AsyncClient,
        admin_engine: AsyncEngine,
        alpha: Workspace,
        beta: Workspace,
        use_embedder: Any,
    ) -> None:
        embedder = use_embedder(HashingEmbedder())
        beta_seed = await seed_talent(client, admin_engine, beta, profiles=TALENT[:2])
        await index_all(beta, beta_seed, embedder)
        beta_ids = {app["candidate_id"] for app in beta_seed.values()}

        async with AsyncSessionLocal() as session:
            await set_tenant_context(session, alpha.tenant_id)
            visible = set(
                (await session.execute(select(CandidateEmbedding.candidate_id))).scalars()
            )
        assert not {str(v) for v in visible} & beta_ids

        results = (await search(client, alpha, "Go Raft distributed consensus", limit=50)).json()[
            "results"
        ]
        assert not {r["candidate_id"] for r in results} & beta_ids
        assert {
            r["candidate_id"]
            for r in (await search(client, beta, "Go Raft", limit=50)).json()["results"]
        } <= beta_ids

    async def test_search_endpoint_contract_rbac_and_audit(
        self,
        client: httpx.AsyncClient,
        admin_engine: AsyncEngine,
        alpha: Workspace,
        use_embedder: Any,
    ) -> None:
        use_embedder(HashingEmbedder())
        response = await search(client, alpha, "raft consensus go", limit=5)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["model"] == "test-hashing-384" and body["total_indexed"] >= 10
        top = body["results"][0]
        assert top["full_name"] == "Gopher Raft"
        assert {"Go", "Raft"} <= set(top["matched_skills"])
        assert any("«" in h for h in top["highlights"])
        assert top["applications"][0]["job_title"] == "General Application"
        assert 0 < top["similarity"] <= 1 and top["hybrid_score"] > 0

        for role, expected in ((HIRING_MANAGER, 200), (INTERVIEWER, 403)):
            assert (await search(client, alpha, "golang", role=role)).status_code == expected
        assert (
            await client.post(f"{API}/talent-pool/search", json={"query": "go engineers"})
        ).status_code == 401
        assert (await search(client, alpha, "go")).status_code == 422  # min 3 chars
        assert (await search(client, alpha, "golang", limit=500)).status_code == 422

        async with admin_engine.connect() as conn:
            logged = await conn.scalar(
                text(
                    "SELECT metadata_json->>'query' FROM audit_logs WHERE tenant_id = :t "
                    "AND action = 'talent_pool_searched' ORDER BY created_at DESC LIMIT 1"
                ),
                {"t": alpha.tenant_id},
            )
        assert logged == "golang" or logged == "raft consensus go"

    async def test_stats_and_admin_reindex(
        self, client: httpx.AsyncClient, alpha: Workspace, embedding_queue: Any, use_embedder: Any
    ) -> None:
        use_embedder(HashingEmbedder())
        stats = (await client.get(f"{API}/talent-pool/stats", headers=alpha.headers())).json()
        assert stats["dimensions"] == 384 and stats["indexed"] <= stats["candidates"]

        before = len(embedding_queue.jobs)
        response = await client.post(f"{API}/talent-pool/reindex", headers=alpha.headers(ADMIN))
        assert response.status_code == 200
        assert response.json()["queued"] == stats["candidates"]
        assert len(embedding_queue.jobs) - before == stats["candidates"]
        forbidden = await client.post(
            f"{API}/talent-pool/reindex", headers=alpha.headers(RECRUITER)
        )
        assert forbidden.status_code == 403

    async def test_missing_model_returns_503(
        self, client: httpx.AsyncClient, alpha: Workspace, use_embedder: Any
    ) -> None:
        use_embedder(FastEmbedEmbedder("BAAI/bge-small-en-v1.5", dimensions=384,
                                       cache_dir=str(REPO_ROOT / ".cache" / "nonexistent"),
                                       model_path="/nonexistent/model"))  # fmt: skip
        response = await search(client, alpha, "distributed go engineers")
        assert response.status_code == 503


# =============================================================================================
# Ranking quality with the real on-device model
# =============================================================================================
@pytest.mark.skipif(
    not _real_model_available(), reason=f"bge-small ONNX files not found in {MODEL_DIR}"
)
class TestSemanticRankingWithRealModel:
    @pytest.fixture(scope="class")
    @classmethod
    def real_embedder(cls) -> FastEmbedEmbedder:
        settings = get_settings()
        return FastEmbedEmbedder(
            settings.EMBEDDING_MODEL,
            dimensions=settings.EMBEDDING_DIM,
            cache_dir=str(settings.embedding_cache_dir),
            query_instruction=settings.EMBEDDING_QUERY_INSTRUCTION,
            model_path=str(MODEL_DIR),
        )

    @pytest.fixture(scope="class")
    async def ranked_workspace(
        self, api_app: object, admin_engine: AsyncEngine, real_embedder: Any
    ):
        transport = httpx.ASGITransport(app=api_app)  # type: ignore[arg-type]
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            workspace = await provision_workspace(http, "Semantic")
            seeded = await seed_talent(http, admin_engine, workspace)
        await index_all(workspace, seeded, real_embedder)
        return workspace, seeded

    async def test_go_distributed_systems_query_ranks_go_backends_first(
        self,
        client: httpx.AsyncClient,
        ranked_workspace: Any,
        real_embedder: Any,
        use_embedder: Any,
    ) -> None:
        use_embedder(real_embedder)
        workspace, seeded = ranked_workspace
        stack_of = {app["candidate_id"]: app["stack"] for app in seeded.values()}

        body = (await search(client, workspace, GO_QUERY, limit=10)).json()

        results = body["results"]
        print(
            "\n"
            + "\n".join(
                f"{r['full_name']:<12} sim={r['similarity']:.3f} rrf={r['hybrid_score']:.4f}"
                for r in results
            )
        )
        assert body["model"] == "BAAI/bge-small-en-v1.5"
        assert [stack_of[r["candidate_id"]] for r in results[:3]] == ["go", "go", "go"]
        top_go = min(r["similarity"] for r in results[:3])
        best_other = max(r["similarity"] for r in results if stack_of[r["candidate_id"]] != "go")
        assert top_go > best_other  # semantic similarity alone also separates the Go backends
        assert all("Go" in r["matched_skills"] for r in results[:3])

    @pytest.mark.parametrize(
        ("query", "expected_stack"),
        [
            ("Full-stack engineer who has built robust APIs", "fullstack"),
            ("Frontend developer experienced with React component libraries", "frontend"),
            ("Infrastructure as code and Kubernetes reliability engineer", "devops"),
            ("Machine learning engineer who trains NLP models in Python", "ml"),
        ],
    )
    async def test_natural_language_queries_find_the_right_specialists(
        self,
        client: httpx.AsyncClient,
        ranked_workspace: Any,
        real_embedder: Any,
        use_embedder: Any,
        query: str,
        expected_stack: str,
    ) -> None:
        use_embedder(real_embedder)
        workspace, seeded = ranked_workspace
        stack_of = {app["candidate_id"]: app["stack"] for app in seeded.values()}
        results = (await search(client, workspace, query, limit=10)).json()["results"]
        assert stack_of[results[0]["candidate_id"]] == expected_stack, [
            r["full_name"] for r in results[:3]
        ]


def test_model_directory_constant_points_inside_repo() -> None:
    assert Path(MODEL_DIR).is_relative_to(REPO_ROOT)
