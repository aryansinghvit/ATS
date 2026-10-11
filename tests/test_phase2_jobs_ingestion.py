"""Phase 2.4 — L5 HR Tester: AI job requisitions, public careers JSON-LD, candidate dropzone.

Strict testing mandate: headless Python script tests only (pytest + httpx.AsyncClient).
No web/browser testing. The local LLM is replaced by a deterministic fake (an opt-in live
Ollama test runs only when the model is available).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import Settings, get_settings
from app.core.database import AsyncSessionLocal, set_tenant_context
from app.models import Job, JobApplication, UserRole
from app.services.job_generator import build_m3_job_description
from app.services.llm_client import (
    LLMOutputError,
    LLMUnavailableError,
    OllamaClient,
    get_llm_client,
)
from app.services.storage import DOCX_MIME, PDF_MIME, S3StorageBackend, StorageService
from tests.helpers import (
    API,
    DEFAULT_TAXONOMY,
    DOCX_BYTES,
    EXE_BYTES,
    PDF_BYTES,
    FakeLLM,
    Workspace,
    create_job,
    parse_sse,
    provision_workspace,
    submit,
    upload_resume,
)

ADMIN, RECRUITER, HIRING_MANAGER, INTERVIEWER = (
    UserRole.ADMIN,
    UserRole.RECRUITER,
    UserRole.HIRING_MANAGER,
    UserRole.INTERVIEWER,
)

# What the local LLM returns for the recruiter prompt (aliases + duplicates on purpose).
GENERATED_JOB: dict[str, Any] = {
    "title": "Staff Backend Engineer",
    "department": "Engineering",
    "seniority_level": "Staff",
    "employment_type": "Full-time",
    "location": "United States",
    "remote_policy": "Remote",
    "compensation": {"min": None, "max": None, "currency": None, "equity": True},
    "about_team": "The Payments Platform team builds the high-concurrency systems that move "
    "money for millions of customers.",
    "responsibilities": [
        "Design and operate high-throughput payment services in Go",
        "Lead Kubernetes-based deployment practices",
    ],
    "required_qualifications": [
        "6+ years building backend systems",
        "Deep experience with Go and Kubernetes",
    ],
    "preferred_qualifications": ["Experience with Kafka or gRPC"],
    "required_skills": [
        {"name": "golang", "min_years": 6},
        {"name": "k8s", "min_years": 3},
        {"name": "Postgres", "min_years": 2},
        {"name": "Go", "min_years": 4},
    ],
    "preferred_skills": [{"name": "apache kafka"}, {"name": "GRPC"}, {"name": "golang"}],
    "min_total_experience_years": 6,
    "education_level": "Bachelor's degree or equivalent practical experience",
}

EXTRACTED_TAXONOMY: dict[str, Any] = {
    "required_skills": [
        {"name": "TypeScript", "min_years": 4},
        {"name": "reactjs", "min_years": 3},
    ],
    "preferred_skills": [{"name": "nextjs"}, {"name": "GraphQL"}],
    "min_total_experience_years": 5,
    "education_level": None,
}


@pytest.fixture
def use_llm(api_app: Any) -> Iterator[Callable[..., FakeLLM]]:
    def install(*responses: dict[str, Any] | Exception) -> FakeLLM:
        fake = FakeLLM(*responses)
        api_app.dependency_overrides[get_llm_client] = lambda: fake
        return fake

    yield install
    api_app.dependency_overrides.pop(get_llm_client, None)


@pytest.fixture(scope="module")
async def workspaces(api_app: object) -> AsyncIterator[tuple[Workspace, Workspace]]:
    transport = httpx.ASGITransport(app=api_app)  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield await provision_workspace(http, "Orbit"), await provision_workspace(http, "Nova")


@pytest.fixture
def alpha(workspaces: tuple[Workspace, Workspace]) -> Workspace:
    return workspaces[0]


@pytest.fixture
def beta(workspaces: tuple[Workspace, Workspace]) -> Workspace:
    return workspaces[1]


# =============================================================================================
# 1. Recruiter creates a job with an AI-assisted description; taxonomy is extracted & saved
# =============================================================================================
class TestAIJobRequisitions:
    async def test_ai_generates_inclusive_description_with_m3_taxonomy(
        self, client: httpx.AsyncClient, alpha: Workspace, use_llm: Callable[..., FakeLLM]
    ) -> None:
        fake = use_llm(GENERATED_JOB)
        response = await client.post(
            f"{API}/jobs/generate-description",
            headers=alpha.headers(RECRUITER),
            json={
                "prompt": "Staff Backend Engineer, 6+ years Go, Kubernetes, high-concurrency "
                "payment processing, remote US",
                "department": "Platform Engineering",
                "salary_min": 180000,
                "salary_max": 240000,
                "currency": "usd",
            },
        )

        assert response.status_code == 200, response.text
        draft = response.json()
        assert draft["title"] == "Staff Backend Engineer"
        assert draft["department"] == "Platform Engineering"  # recruiter facts override the model
        assert (draft["employment_type"], draft["remote_policy"]) == ("full_time", "remote")
        assert (draft["salary_min"], draft["salary_max"], draft["currency"]) == (
            180000,
            240000,
            "USD",
        )
        assert draft["model"] == FakeLLM.model

        taxonomy = draft["taxonomy"]
        assert taxonomy["required_skills"] == [
            {"name": "Go", "min_years": 6.0, "weight": 1.0},
            {"name": "Kubernetes", "min_years": 3.0, "weight": 1.0},
            {"name": "PostgreSQL", "min_years": 2.0, "weight": 1.0},
        ]
        assert taxonomy["preferred_skills"] == [
            {"name": "Kafka", "weight": 0.5},
            {"name": "gRPC", "weight": 0.5},
        ]
        assert taxonomy["min_total_experience_years"] == 6

        markdown = draft["description_markdown"]
        for section in ("## About the Team", "## What You'll Do", "## What You'll Bring"):
            assert section in markdown
        assert "USD 180,000 – 240,000 per year, plus equity" in markdown
        assert "## Equal Opportunity" in markdown and alpha.name in markdown
        assert "reasonable accommodation" in markdown

        call = fake.calls[0]
        assert "inclusive" in call["system"] and "Platform Engineering" in call["prompt"]
        assert call["schema"]["type"] == "object"

    async def test_recruiter_creates_job_and_taxonomy_is_saved(
        self,
        client: httpx.AsyncClient,
        alpha: Workspace,
        admin_engine: AsyncEngine,
        use_llm: Callable[..., FakeLLM],
    ) -> None:
        use_llm(GENERATED_JOB)
        draft = (
            await client.post(
                f"{API}/jobs/generate-description",
                headers=alpha.headers(RECRUITER),
                json={"prompt": "Staff Backend Engineer, Go, Kubernetes, payments, remote US"},
            )
        ).json()
        payload = {key: draft[key] for key in draft if key != "model"}
        payload["status"] = "published"

        response = await client.post(f"{API}/jobs", json=payload, headers=alpha.headers(RECRUITER))

        assert response.status_code == 201, response.text
        job = response.json()
        assert job["status"] == "published" and job["published_at"] is not None
        assert job["slug"].startswith("staff-backend-engineer")
        assert job["creator_id"] == str(alpha.members[RECRUITER].user_id)
        assert [s["name"] for s in job["taxonomy"]["required_skills"]] == [
            "Go",
            "Kubernetes",
            "PostgreSQL",
        ]
        assert [s["name"] for s in job["taxonomy"]["preferred_skills"]] == ["Kafka", "gRPC"]
        assert "<h2>Equal Opportunity</h2>" in job["description_html"]

        async with admin_engine.connect() as conn:  # ground truth in PostgreSQL
            stored = (
                await conn.execute(
                    text("SELECT tenant_id, taxonomy_json FROM jobs WHERE id = :id"),
                    {"id": job["id"]},
                )
            ).one()
            stages = (
                (
                    await conn.execute(
                        text(
                            "SELECT name FROM pipeline_stages WHERE job_id = :id "
                            "ORDER BY position_index"
                        ),
                        {"id": job["id"]},
                    )
                )
                .scalars()
                .all()
            )
        assert stored.tenant_id == alpha.tenant_id
        assert {"required_skills", "preferred_skills"} <= stored.taxonomy_json.keys()
        assert stored.taxonomy_json["required_skills"][0] == {
            "name": "Go",
            "min_years": 6.0,
            "weight": 1.0,
        }
        assert stages == [
            "Applied", "Pre-Screen", "Tech Screen", "Interview", "Offer", "Hired", "Rejected"
        ]  # fmt: skip

    async def test_taxonomy_is_ai_extracted_when_omitted(
        self, client: httpx.AsyncClient, alpha: Workspace, use_llm: Callable[..., FakeLLM]
    ) -> None:
        fake = use_llm(EXTRACTED_TAXONOMY)
        job = await create_job(client, alpha, title="Senior Frontend Engineer", taxonomy=None)
        assert job["taxonomy"]["required_skills"] == [
            {"name": "TypeScript", "min_years": 4.0, "weight": 1.0},
            {"name": "React", "min_years": 3.0, "weight": 1.0},
        ]
        assert [s["name"] for s in job["taxonomy"]["preferred_skills"]] == ["Next.js", "GraphQL"]
        assert "Senior Frontend Engineer" in fake.calls[0]["prompt"]

    async def test_m3_payload_matches_extractor_contract(
        self, client: httpx.AsyncClient, alpha: Workspace
    ) -> None:
        created = await create_job(
            client,
            alpha,
            taxonomy={
                "required_skills": [{"name": "golang", "min_years": 4}, {"name": "K8s"}],
                "preferred_skills": [{"name": "Kafka", "weight": 0.7}],
                "min_total_experience_years": 5,
            },
        )
        async with AsyncSessionLocal() as session:
            await set_tenant_context(session, alpha.tenant_id)
            job = await session.get(Job, uuid.UUID(created["id"]))
            assert job is not None
            payload = build_m3_job_description(job)
        assert payload == {
            "job_id": created["id"],
            "title": created["title"],
            "required_skills": ["Go", "Kubernetes"],
            "preferred_skills": ["Kafka"],
            "min_years_experience": 5.0,
        }

    async def test_invalid_llm_output_is_auto_repaired_once(
        self, client: httpx.AsyncClient, alpha: Workspace, use_llm: Callable[..., FakeLLM]
    ) -> None:
        broken = {**GENERATED_JOB, "required_skills": [], "responsibilities": []}
        fake = use_llm(broken, GENERATED_JOB)
        response = await client.post(
            f"{API}/jobs/generate-description",
            headers=alpha.headers(RECRUITER),
            json={"prompt": "Backend engineer with Go and Kubernetes"},
        )
        assert response.status_code == 200
        assert len(fake.calls) == 2
        assert "previous answer was invalid" in fake.calls[1]["prompt"]

        use_llm(broken, broken)
        failed = await client.post(
            f"{API}/jobs/generate-description",
            headers=alpha.headers(RECRUITER),
            json={"prompt": "Backend engineer with Go and Kubernetes"},
        )
        assert failed.status_code == 502

    async def test_llm_outage_returns_503_and_manual_creation_still_works(
        self, client: httpx.AsyncClient, alpha: Workspace, use_llm: Callable[..., FakeLLM]
    ) -> None:
        use_llm(LLMUnavailableError("Local LLM (Ollama) is unreachable: ConnectError"))
        generated = await client.post(
            f"{API}/jobs/generate-description",
            headers=alpha.headers(RECRUITER),
            json={"prompt": "Backend engineer with Go and Kubernetes"},
        )
        extracted = await client.post(
            f"{API}/jobs",
            headers=alpha.headers(RECRUITER),
            json={
                "title": "Data Engineer",
                "department": "Data",
                "location": "Berlin",
                "description_markdown": "Build pipelines.",
            },
        )
        assert generated.status_code == 503 and extracted.status_code == 503
        manual = await create_job(client, alpha, title="Data Engineer")  # explicit taxonomy
        assert manual["status"] == "published"

    async def test_ollama_client_uses_structured_outputs(self) -> None:
        seen: dict[str, Any] = {}

        def ollama(request: httpx.Request) -> httpx.Response:
            seen.update(json.loads(request.content))
            content = json.dumps({"ok": True})
            return httpx.Response(200, json={"message": {"role": "assistant", "content": content}})

        client = OllamaClient(
            "http://ollama.test",
            "llama3.2:3b",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(ollama)),
        )
        result = await client.generate_json(system="sys", prompt="notes", schema={"type": "object"})
        assert result == {"ok": True}
        assert seen["model"] == "llama3.2:3b" and seen["stream"] is False
        assert seen["format"] == {"type": "object"}
        assert [m["role"] for m in seen["messages"]] == ["system", "user"]

        for handler, error in (
            (lambda r: (_ for _ in ()).throw(httpx.ConnectError("refused")), LLMUnavailableError),
            (lambda r: httpx.Response(404, json={"error": "model not found"}), LLMUnavailableError),
            (
                lambda r: httpx.Response(200, json={"message": {"content": "not json"}}),
                LLMOutputError,
            ),
        ):
            failing = OllamaClient(
                "http://ollama.test",
                "llama3.2:3b",
                http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            )
            with pytest.raises(error):
                await failing.generate_json(system="s", prompt="p", schema={})

    async def test_publishing_requires_required_skills(
        self, client: httpx.AsyncClient, alpha: Workspace
    ) -> None:
        response = await client.post(
            f"{API}/jobs",
            headers=alpha.headers(RECRUITER),
            json={
                "title": "Mystery Role",
                "department": "Ops",
                "location": "Remote",
                "description_markdown": "TBD",
                "taxonomy": {"required_skills": [], "preferred_skills": []},
                "status": "published",
            },
        )
        assert response.status_code == 422
        draft = await create_job(
            client, alpha, status="draft", taxonomy={"required_skills": [], "preferred_skills": []}
        )
        publish = await client.patch(
            f"{API}/jobs/{draft['id']}", json={"status": "published"}, headers=alpha.headers()
        )
        assert publish.status_code == 422

    async def test_status_lifecycle_and_filters(
        self, client: httpx.AsyncClient, alpha: Workspace
    ) -> None:
        draft = await create_job(client, alpha, status="draft")
        assert draft["published_at"] is None
        published = await client.patch(
            f"{API}/jobs/{draft['id']}", json={"status": "published"}, headers=alpha.headers()
        )
        assert published.status_code == 200 and published.json()["published_at"] is not None
        invalid = await client.patch(
            f"{API}/jobs/{draft['id']}", json={"status": "archived"}, headers=alpha.headers()
        )
        assert invalid.status_code == 422  # published -> archived must go through closed

        listed = await client.get(
            f"{API}/jobs?status=published", headers=alpha.headers(INTERVIEWER)
        )
        assert listed.status_code == 200
        assert draft["id"] in {job["id"] for job in listed.json()}
        assert {job["status"] for job in listed.json()} == {"published"}

    async def test_slugs_are_unique_per_tenant_and_markdown_is_sanitised(
        self, client: httpx.AsyncClient, alpha: Workspace, beta: Workspace
    ) -> None:
        title = f"Site Reliability Engineer {uuid.uuid4().hex[:4]}"
        first = await create_job(client, alpha, title=title)
        second = await create_job(
            client,
            alpha,
            title=title,
            description_markdown="<script>alert(1)</script>\n\n[click](javascript:alert(1))",
        )
        other_tenant = await create_job(client, beta, title=title)
        assert second["slug"] == f"{first['slug']}-2"
        assert other_tenant["slug"] == first["slug"]  # slugs are scoped per tenant
        assert "<script>" not in second["description_html"]
        assert "&lt;script&gt;" in second["description_html"]
        assert 'href="javascript:' not in second["description_html"]

    async def test_job_rbac_matrix(self, client: httpx.AsyncClient, alpha: Workspace) -> None:
        hm_draft = await create_job(client, alpha, role=HIRING_MANAGER, status="draft")
        assert hm_draft["status"] == "draft"

        hm_publish = await client.patch(
            f"{API}/jobs/{hm_draft['id']}",
            json={"status": "published"},
            headers=alpha.headers(HIRING_MANAGER),
        )
        hm_create_published = await client.post(
            f"{API}/jobs",
            headers=alpha.headers(HIRING_MANAGER),
            json={
                "title": "HM Role",
                "department": "Eng",
                "location": "Remote",
                "description_markdown": "x",
                "taxonomy": DEFAULT_TAXONOMY,
                "status": "published",
            },
        )
        published = await create_job(client, alpha)
        hm_edit_published = await client.patch(
            f"{API}/jobs/{published['id']}",
            json={"title": "Renamed"},
            headers=alpha.headers(HIRING_MANAGER),
        )
        assert hm_publish.status_code == 403
        assert hm_create_published.status_code == 403
        assert hm_edit_published.status_code == 403

        interviewer = alpha.headers(INTERVIEWER)
        assert (await client.post(f"{API}/jobs", json={}, headers=interviewer)).status_code == 403
        assert (
            await client.patch(f"{API}/jobs/{published['id']}", json={}, headers=interviewer)
        ).status_code == 403
        assert (
            await client.post(f"{API}/jobs/generate-description", json={}, headers=interviewer)
        ).status_code == 403
        assert (
            await client.get(f"{API}/jobs/{published['id']}", headers=interviewer)
        ).status_code == 200

    async def test_jobs_are_tenant_isolated(
        self, client: httpx.AsyncClient, alpha: Workspace, beta: Workspace
    ) -> None:
        job = await create_job(client, alpha)
        assert (
            await client.get(f"{API}/jobs/{job['id']}", headers=beta.headers())
        ).status_code == 404
        assert (
            await client.patch(
                f"{API}/jobs/{job['id']}", json={"title": "pwned"}, headers=beta.headers()
            )
        ).status_code == 404
        beta_ids = {
            j["id"] for j in (await client.get(f"{API}/jobs", headers=beta.headers())).json()
        }
        assert job["id"] not in beta_ids


# =============================================================================================
# 2. Public careers endpoint returns job details with Schema.org JSON-LD
# =============================================================================================
class TestPublicCareersJsonLd:
    async def test_public_job_detail_includes_job_posting_json_ld(
        self, client: httpx.AsyncClient, alpha: Workspace
    ) -> None:
        job = await create_job(
            client,
            alpha,
            title="Staff Backend Engineer",
            salary_min=180000,
            salary_max=240000,
            currency="USD",
            seniority_level="Staff",
            taxonomy={**DEFAULT_TAXONOMY, "min_total_experience_years": 6},
        )

        response = await client.get(f"{API}/public/jobs/{alpha.subdomain}/{job['slug']}")

        assert response.status_code == 200, response.text
        detail = response.json()
        assert detail["title"] == "Staff Backend Engineer"
        assert detail["organization"]["subdomain"] == alpha.subdomain
        assert detail["description_html"] == job["description_html"]
        assert (
            "taxonomy" not in detail and "creator_id" not in detail
        )  # internal data stays private

        ld = detail["json_ld"]
        assert ld["@context"] == "https://schema.org/" and ld["@type"] == "JobPosting"
        assert ld["title"] == "Staff Backend Engineer"
        assert ld["description"] == job["description_html"]
        assert ld["datePosted"] == job["published_at"][:10]
        assert ld["employmentType"] == "FULL_TIME"
        assert ld["hiringOrganization"] == {"@type": "Organization", "name": alpha.name}
        assert ld["identifier"]["value"] == job["id"]
        assert ld["jobLocationType"] == "TELECOMMUTE"
        assert ld["applicantLocationRequirements"]["name"] == "Remote (US)"
        assert ld["baseSalary"] == {
            "@type": "MonetaryAmount",
            "currency": "USD",
            "value": {
                "@type": "QuantitativeValue",
                "unitText": "YEAR",
                "minValue": 180000.0,
                "maxValue": 240000.0,
            },
        }
        assert ld["experienceRequirements"]["monthsOfExperience"] == 72
        assert ld["url"].endswith(f"/careers/{alpha.subdomain}/{job['slug']}")
        assert ld["directApply"] is True

    async def test_onsite_job_uses_job_location_place(
        self, client: httpx.AsyncClient, alpha: Workspace
    ) -> None:
        job = await create_job(
            client, alpha, location="Austin, TX", remote_policy="hybrid", employment_type="contract"
        )
        ld = (await client.get(f"{API}/public/jobs/{alpha.subdomain}/{job['slug']}")).json()[
            "json_ld"
        ]
        assert ld["jobLocation"]["address"]["addressLocality"] == "Austin, TX"
        assert "jobLocationType" not in ld and "baseSalary" not in ld
        assert ld["employmentType"] == "CONTRACTOR"

    async def test_only_published_jobs_are_public(
        self, client: httpx.AsyncClient, alpha: Workspace, beta: Workspace
    ) -> None:
        draft = await create_job(client, alpha, status="draft")
        paused = await create_job(client, alpha)
        await client.patch(
            f"{API}/jobs/{paused['id']}", json={"status": "paused"}, headers=alpha.headers()
        )
        live = await create_job(client, alpha)

        for slug in (draft["slug"], paused["slug"]):
            assert (
                await client.get(f"{API}/public/jobs/{alpha.subdomain}/{slug}")
            ).status_code == 404
        board = await client.get(f"{API}/public/jobs/{alpha.subdomain}")
        assert board.status_code == 200
        slugs = {job["slug"] for job in board.json()["jobs"]}
        assert live["slug"] in slugs and not {draft["slug"], paused["slug"]} & slugs
        assert board.json()["organization"]["name"] == alpha.name

        # Another tenant's subdomain never exposes this tenant's jobs; unknown sites are 404.
        assert (
            await client.get(f"{API}/public/jobs/{beta.subdomain}/{live['slug']}")
        ).status_code == 404
        assert (await client.get(f"{API}/public/jobs/no-such-company")).status_code == 404


# =============================================================================================
# 3. Candidate dropzone: presign -> direct upload -> submit -> pending_extraction in PostgreSQL
# =============================================================================================
class TestCandidateDropzone:
    async def test_dropzone_flow_stores_pending_extraction_application(
        self, client: httpx.AsyncClient, alpha: Workspace, admin_engine: AsyncEngine
    ) -> None:
        job = await create_job(client, alpha)
        presign = await client.post(
            f"{API}/public/applications/presign-upload",
            json={
                "tenant_subdomain": alpha.subdomain,
                "job_id": job["id"],
                "file_name": "Jane Doe Resume.pdf",
                "mime_type": PDF_MIME,
                "file_size_bytes": len(PDF_BYTES),
            },
        )
        assert presign.status_code == 200, presign.text
        upload = presign.json()
        assert upload["method"] == "PUT" and upload["headers"] == {"Content-Type": PDF_MIME}
        assert upload["expires_in"] == 900 and upload["max_file_size_bytes"] == 15 * 1024 * 1024
        assert upload["s3_key"].startswith(f"tenants/{alpha.tenant_id}/resumes/")
        query = parse_qs(urlsplit(upload["upload_url"]).query)
        assert {"expires", "content_type", "signature"} <= query.keys()

        put = await client.put(upload["upload_url"], content=PDF_BYTES, headers=upload["headers"])
        assert put.status_code == 204
        stored_file = get_settings().local_storage_dir / upload["s3_key"]
        assert stored_file.read_bytes() == PDF_BYTES

        response = await submit(
            client,
            alpha,
            job["id"],
            upload["s3_key"],
            email="Jane.Doe@Example.com",
            full_name="Jane Q Doe",
            phone="+1-555-0100",
        )

        assert response.status_code == 202, response.text
        body = response.json()
        assert body["status"] == "pending_extraction"
        assert (
            body["status_token"]
            and f"/public/applications/{body['application_id']}/events" in body["events_url"]
        )

        async with admin_engine.connect() as conn:  # ground truth in PostgreSQL
            row = (
                await conn.execute(
                    text(
                        """
                        SELECT a.status::text AS status, a.tenant_id, a.resume_s3_key,
                               s.name AS stage, c.email, c.first_name, c.last_name,
                               c.resume_file_name, c.resume_mime_type, c.resume_file_size_bytes,
                               c.original_resume_s3_key
                        FROM job_applications a
                        JOIN candidates c ON c.id = a.candidate_id
                        JOIN pipeline_stages s ON s.id = a.current_stage_id
                        WHERE a.id = :id
                        """
                    ),
                    {"id": body["application_id"]},
                )
            ).one()
        assert row.status == "pending_extraction"
        assert row.tenant_id == alpha.tenant_id
        assert row.stage == "Applied"
        assert row.resume_s3_key == row.original_resume_s3_key == upload["s3_key"]
        assert (row.email, row.first_name, row.last_name) == (
            "jane.doe@example.com",
            "Jane Q",
            "Doe",
        )
        assert (row.resume_file_name, row.resume_mime_type) == ("Jane Doe Resume.pdf", PDF_MIME)
        assert row.resume_file_size_bytes == len(PDF_BYTES)

    async def test_docx_resumes_are_accepted(
        self, client: httpx.AsyncClient, alpha: Workspace
    ) -> None:
        job = await create_job(client, alpha)
        key = await upload_resume(
            client, alpha, job["id"], content=DOCX_BYTES, file_name="cv.docx", mime_type=DOCX_MIME
        )
        response = await submit(client, alpha, job["id"], key)
        assert response.status_code == 202, response.text

    async def test_presign_rejects_invalid_requests(
        self, client: httpx.AsyncClient, alpha: Workspace
    ) -> None:
        job = await create_job(client, alpha)
        draft = await create_job(client, alpha, status="draft")
        base = {
            "tenant_subdomain": alpha.subdomain,
            "job_id": job["id"],
            "file_name": "resume.pdf",
            "mime_type": PDF_MIME,
            "file_size_bytes": 1000,
        }
        cases = {
            "image_upload": ({"mime_type": "image/png", "file_name": "me.png"}, 422),
            "disguised_extension": ({"file_name": "resume.exe"}, 422),
            "too_large": ({"file_size_bytes": 15 * 1024 * 1024 + 1}, 422),
            "draft_job": ({"job_id": draft["id"]}, 404),
            "unknown_job": ({"job_id": str(uuid.uuid4())}, 404),
            "unknown_company": ({"tenant_subdomain": "no-such-company"}, 404),
        }
        for name, (override, expected) in cases.items():
            response = await client.post(
                f"{API}/public/applications/presign-upload", json={**base, **override}
            )
            assert response.status_code == expected, f"{name}: {response.text}"

    async def test_upload_urls_are_tamper_proof_and_size_limited(
        self, client: httpx.AsyncClient, alpha: Workspace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        job = await create_job(client, alpha)
        presign = (
            await client.post(
                f"{API}/public/applications/presign-upload",
                json={
                    "tenant_subdomain": alpha.subdomain,
                    "job_id": job["id"],
                    "file_name": "resume.pdf",
                    "mime_type": PDF_MIME,
                    "file_size_bytes": 100,
                },
            )
        ).json()
        url, headers = presign["upload_url"], presign["headers"]

        tampered_key = url.replace("/resumes/", "/resumes/0")
        forged_sig = url[:-4] + ("0000" if not url.endswith("0000") else "1111")
        assert (
            await client.put(tampered_key, content=PDF_BYTES, headers=headers)
        ).status_code == 403
        assert (await client.put(forged_sig, content=PDF_BYTES, headers=headers)).status_code == 403
        wrong_type = await client.put(url, content=PDF_BYTES, headers={"Content-Type": "text/html"})
        assert wrong_type.status_code == 415

        monkeypatch.setattr(get_settings(), "MAX_RESUME_SIZE_BYTES", 64)
        too_big = await client.put(url, content=PDF_BYTES * 10, headers=headers)
        assert too_big.status_code == 413  # rejected from the declared Content-Length

        async def chunked_body() -> AsyncIterator[bytes]:
            for _ in range(10):
                yield PDF_BYTES

        streamed = await client.put(url, content=chunked_body(), headers=headers)
        assert "content-length" not in streamed.request.headers
        assert streamed.status_code == 413  # rejected while streaming, no partial file kept
        target = get_settings().local_storage_dir / presign["s3_key"]
        assert not list(target.parent.glob(f"*{target.name}*"))  # neither file nor .part remains

    async def test_submit_rejects_disguised_executables(
        self, client: httpx.AsyncClient, alpha: Workspace, admin_engine: AsyncEngine
    ) -> None:
        job = await create_job(client, alpha)
        key = await upload_resume(client, alpha, job["id"], content=EXE_BYTES, file_name="cv.pdf")
        response = await submit(client, alpha, job["id"], key)
        assert response.status_code == 422
        assert "signature" in response.json()["detail"]
        assert not (get_settings().local_storage_dir / key).exists()  # rejected upload is purged
        async with admin_engine.connect() as conn:
            count = await conn.scalar(
                text("SELECT count(*) FROM job_applications WHERE resume_s3_key = :k"), {"k": key}
            )
        assert count == 0

    async def test_submit_rejects_foreign_missing_or_reused_uploads(
        self, client: httpx.AsyncClient, alpha: Workspace, beta: Workspace
    ) -> None:
        alpha_job = await create_job(client, alpha)
        beta_job = await create_job(client, beta)
        beta_key = await upload_resume(client, beta, beta_job["id"])
        missing_key = f"tenants/{alpha.tenant_id}/resumes/{uuid.uuid4().hex}.pdf"
        traversal_key = f"tenants/{alpha.tenant_id}/resumes/../../{beta.tenant_id}/x.pdf"

        for key in (beta_key, missing_key, traversal_key):
            response = await submit(client, alpha, alpha_job["id"], key)
            assert response.status_code == 422, (key, response.text)

        own_key = await upload_resume(client, alpha, alpha_job["id"])
        assert (await submit(client, alpha, alpha_job["id"], own_key)).status_code == 202
        reused = await submit(client, alpha, alpha_job["id"], own_key)
        assert reused.status_code == 409

    async def test_duplicate_applications_and_candidate_reuse(
        self, client: httpx.AsyncClient, alpha: Workspace, admin_engine: AsyncEngine
    ) -> None:
        email = f"repeat.{uuid.uuid4().hex[:6]}@example.com"
        first_job, second_job = await create_job(client, alpha), await create_job(client, alpha)

        first_key = await upload_resume(client, alpha, first_job["id"])
        assert (
            await submit(client, alpha, first_job["id"], first_key, email=email)
        ).status_code == 202

        dup_key = await upload_resume(client, alpha, first_job["id"])
        duplicate = await submit(client, alpha, first_job["id"], dup_key, email=email.upper())
        assert duplicate.status_code == 409
        assert not (get_settings().local_storage_dir / dup_key).exists()

        second_key = await upload_resume(client, alpha, second_job["id"])
        other_role = await submit(
            client, alpha, second_job["id"], second_key, email=email, full_name="X"
        )
        assert other_role.status_code == 202

        async with admin_engine.connect() as conn:
            candidates = (
                await conn.execute(
                    text(
                        "SELECT id, original_resume_s3_key, first_name FROM candidates "
                        "WHERE email = :e"
                    ),
                    {"e": email},
                )
            ).all()
            app_keys = set(
                (
                    await conn.execute(
                        text("SELECT resume_s3_key FROM job_applications WHERE candidate_id = :c"),
                        {"c": candidates[0].id},
                    )
                ).scalars()
            )
        assert len(candidates) == 1
        # The anonymous second submission did not overwrite the existing candidate profile...
        assert (
            candidates[0].original_resume_s3_key == first_key and candidates[0].first_name is None
        )
        # ...but its own resume is attached to its own application.
        assert app_keys == {first_key, second_key}

    async def test_applications_are_tenant_isolated_under_rls(
        self, client: httpx.AsyncClient, alpha: Workspace, beta: Workspace
    ) -> None:
        job = await create_job(client, alpha)
        key = await upload_resume(client, alpha, job["id"])
        application_id = uuid.UUID(
            (await submit(client, alpha, job["id"], key)).json()["application_id"]
        )

        async with AsyncSessionLocal() as session:
            await set_tenant_context(session, beta.tenant_id)
            assert await session.get(JobApplication, application_id) is None
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(JobApplication)
                    .where(JobApplication.job_id == uuid.UUID(job["id"]))
                )
                == 0
            )
        async with AsyncSessionLocal() as session:
            await set_tenant_context(session, alpha.tenant_id)
            assert await session.get(JobApplication, application_id) is not None

    async def test_application_status_stream(
        self,
        client: httpx.AsyncClient,
        alpha: Workspace,
        admin_engine: AsyncEngine,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        settings = get_settings()
        monkeypatch.setattr(settings, "APPLICATION_EVENTS_MAX_SECONDS", 0.3)
        monkeypatch.setattr(settings, "APPLICATION_EVENTS_POLL_SECONDS", 0.05)

        job = await create_job(client, alpha)
        key = await upload_resume(client, alpha, job["id"])
        submitted = (await submit(client, alpha, job["id"], key, full_name="Ada Lovelace")).json()
        events_path = submitted["events_url"].removeprefix("http://test")

        pending = await client.get(events_path)
        assert pending.status_code == 200
        assert pending.headers["content-type"].startswith("text/event-stream")
        assert parse_sse(pending.text) == [
            ("status", {"application_id": submitted["application_id"], "status": "processing"})
        ]

        forged = events_path.replace("token=", "token=0")
        assert (await client.get(forged)).status_code == 403
        other_app = events_path.replace(submitted["application_id"], str(uuid.uuid4()))
        assert (await client.get(other_app)).status_code == 403  # token is bound to one application

        async with admin_engine.begin() as conn:  # simulate what the extraction webhook stores
            await conn.execute(
                text(
                    "UPDATE job_applications SET status = 'applied', "
                    "match_breakdown_json = :m, extracted_profile_json = :p WHERE id = :id"
                ),
                {
                    "m": json.dumps({"overall_score": 81.0, "breakdown": {}}),
                    "p": json.dumps({"contact": {}, "skills": ["Python", "FastAPI"]}),
                    "id": submitted["application_id"],
                },
            )
        parsed = parse_sse((await client.get(events_path)).text)
        assert parsed[-1][0] == "status"
        assert parsed[-1][1]["status"] == "parsed"
        assert parsed[-1][1]["candidate"]["full_name"] == "Ada Lovelace"
        assert parsed[-1][1]["candidate"]["skills"] == ["Python", "FastAPI"]


# =============================================================================================
# Production storage path (S3 / Cloudflare R2 via aioboto3) — pre-signing is computed offline
# =============================================================================================
async def test_r2_presigned_put_url_via_aioboto3() -> None:
    settings = Settings(
        _env_file=None,
        ENVIRONMENT="test",
        STORAGE_BACKEND="r2",
        S3_BUCKET="ats-resumes",
        S3_ENDPOINT_URL="https://account123.r2.cloudflarestorage.com",
        S3_ACCESS_KEY_ID="test-access-key",
        S3_SECRET_ACCESS_KEY="test-secret-key",
    )
    service = StorageService(S3StorageBackend(settings), settings)
    tenant_id = uuid.uuid4()

    upload = await service.generate_presigned_upload_url("resume.pdf", PDF_MIME, tenant_id)
    download = await service.get_presigned_download_url(upload.key)

    parts = urlsplit(upload.url)
    query = parse_qs(parts.query)
    assert parts.netloc.startswith("account123.r2.cloudflarestorage.com")
    assert parts.path == f"/ats-resumes/{upload.key}"
    assert upload.key.startswith(f"tenants/{tenant_id}/resumes/") and upload.key.endswith(".pdf")
    assert query["X-Amz-Expires"] == ["900"] and "X-Amz-Signature" in query
    assert "content-type" in query["X-Amz-SignedHeaders"][0]
    assert upload.headers == {"Content-Type": PDF_MIME}
    assert "X-Amz-Signature" in parse_qs(urlsplit(download).query)


# =============================================================================================
# Opt-in: live generation against the local Ollama model (skipped when it is not available)
# =============================================================================================
def _ollama_model_available() -> bool:
    settings = get_settings()
    try:
        tags = httpx.get(f"{settings.OLLAMA_URL}/api/tags", timeout=1.0).json()
    except (httpx.HTTPError, ValueError):
        return False
    names = {model.get("name") for model in tags.get("models", [])}
    return settings.OLLAMA_MODEL in names


@pytest.mark.skipif(
    not _ollama_model_available(),
    reason=f"Ollama model {get_settings().OLLAMA_MODEL} is not available locally",
)
async def test_live_ollama_job_generation(client: httpx.AsyncClient, alpha: Workspace) -> None:
    response = await client.post(
        f"{API}/jobs/generate-description",
        headers=alpha.headers(RECRUITER),
        json={
            "prompt": "Senior backend engineer, 5+ years Python and FastAPI, PostgreSQL, remote EU"
        },
        timeout=300,
    )
    assert response.status_code == 200, response.text
    assert response.json()["taxonomy"]["required_skills"]


def test_storage_root_is_isolated_for_tests() -> None:
    assert Path(get_settings().LOCAL_STORAGE_PATH).name.startswith("ats-test-uploads-")
