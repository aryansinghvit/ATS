"""Test helpers: Clerk-shaped mock tokens and workspace provisioning through the public API."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.security import create_mock_token
from app.models.user import UserRole
from app.services.webhook_service import sign_payload

API = "/api/v1"
PDF_MIME = "application/pdf"

PDF_BYTES = (
    b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj << /Type /Catalog >> endobj\ntrailer << >>\n%%EOF\n"
)
DOCX_BYTES = b"PK\x03\x04\x14\x00\x06\x00" + b"\x00" * 120
EXE_BYTES = b"MZ\x90\x00\x03\x00\x00\x00\x04\x00" + b"\x00" * 120

DEFAULT_TAXONOMY = {
    "required_skills": [{"name": "Python", "min_years": 3}],
    "preferred_skills": [{"name": "FastAPI"}],
    "min_total_experience_years": 3,
}


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def mint_token(*, clerk_user_id: str, org_id: str, role: UserRole | str, **kwargs: object) -> str:
    org_role = f"org:{role.value}" if isinstance(role, UserRole) else role
    return create_mock_token(user_id=clerk_user_id, org_id=org_id, org_role=org_role, **kwargs)  # type: ignore[arg-type]


@dataclass
class Member:
    role: UserRole
    clerk_user_id: str
    email: str
    token: str
    user_id: uuid.UUID


@dataclass
class Workspace:
    tenant_id: uuid.UUID
    org_id: str
    subdomain: str
    name: str
    members: dict[UserRole, Member] = field(default_factory=dict)

    def token(self, role: UserRole = UserRole.ADMIN) -> str:
        return self.members[role].token

    def headers(self, role: UserRole = UserRole.ADMIN) -> dict[str, str]:
        return bearer(self.token(role))


async def provision_workspace(client: httpx.AsyncClient, name: str) -> Workspace:
    """Provision a tenant via its Clerk admin, then sync one user for every other role."""
    suffix = uuid.uuid4().hex[:8]
    org_id = f"org_{suffix}"
    subdomain = f"{name.lower()}-{suffix}"
    workspace: Workspace | None = None

    for role in UserRole:  # ADMIN first: only an org admin may provision the workspace
        clerk_user_id = f"user_{role.value}_{suffix}"
        email = f"{role.value}@{subdomain}.example.com"
        token = mint_token(clerk_user_id=clerk_user_id, org_id=org_id, role=role)
        body: dict[str, object] = {"email": email, "full_name": f"{name} {role.value.title()}"}
        if role is UserRole.ADMIN:
            body["organization"] = {"name": f"{name} Inc", "subdomain": subdomain}
        response = await client.post(f"{API}/auth/sync-user", json=body, headers=bearer(token))
        assert response.status_code == 201, response.text
        data = response.json()
        if workspace is None:
            workspace = Workspace(
                tenant_id=uuid.UUID(data["tenant"]["id"]),
                org_id=org_id,
                subdomain=data["tenant"]["subdomain"],
                name=data["tenant"]["name"],
            )
        workspace.members[role] = Member(
            role=role,
            clerk_user_id=clerk_user_id,
            email=email,
            token=token,
            user_id=uuid.UUID(data["user"]["id"]),
        )
    assert workspace is not None
    return workspace


class RecordingExtractionQueue:
    """Stands in for the ARQ queue: records (tenant_id, application_id) instead of using Redis."""

    def __init__(self) -> None:
        self.jobs: list[tuple[uuid.UUID, uuid.UUID]] = []
        self.fail_with: Exception | None = None

    async def enqueue_extraction(self, tenant_id: uuid.UUID, application_id: uuid.UUID) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        self.jobs.append((tenant_id, application_id))


# ------------------------------------------------------------ jobs & candidate ingestion
async def create_job(
    client: httpx.AsyncClient,
    workspace: Workspace,
    *,
    role: UserRole = UserRole.RECRUITER,
    title: str | None = None,
    status: str = "published",
    taxonomy: dict[str, Any] | None = DEFAULT_TAXONOMY,
    **fields: Any,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "title": title or f"Backend Engineer {uuid.uuid4().hex[:6]}",
        "department": "Engineering",
        "location": "Remote (US)",
        "description_markdown": "## About\n\nBuild resilient APIs.\n\n- Python\n- FastAPI\n",
        "status": status,
        **fields,
    }
    if taxonomy is not None:
        body["taxonomy"] = taxonomy
    response = await client.post(f"{API}/jobs", json=body, headers=workspace.headers(role))
    assert response.status_code == 201, response.text
    return response.json()


async def upload_resume(
    client: httpx.AsyncClient,
    workspace: Workspace,
    job_id: str,
    *,
    content: bytes = PDF_BYTES,
    file_name: str = "Jane Doe Resume.pdf",
    mime_type: str = PDF_MIME,
) -> str:
    presign = await client.post(
        f"{API}/public/applications/presign-upload",
        json={
            "tenant_subdomain": workspace.subdomain,
            "job_id": job_id,
            "file_name": file_name,
            "mime_type": mime_type,
            "file_size_bytes": len(content),
        },
    )
    assert presign.status_code == 200, presign.text
    body = presign.json()
    upload = await client.put(body["upload_url"], content=content, headers=body["headers"])
    assert upload.status_code == 204, upload.text
    return body["s3_key"]


async def submit(
    client: httpx.AsyncClient,
    workspace: Workspace,
    job_id: str,
    s3_key: str,
    *,
    email: str | None = None,
    **extra: Any,
) -> httpx.Response:
    return await client.post(
        f"{API}/public/applications/submit",
        json={
            "tenant_subdomain": workspace.subdomain,
            "job_id": job_id,
            "s3_key": s3_key,
            "file_name": "Jane Doe Resume.pdf",
            "email": email or f"jane.{uuid.uuid4().hex[:6]}@example.com",
            **extra,
        },
    )


def parse_sse(body: str) -> list[tuple[str, dict[str, Any]]]:
    events = []
    for block in body.split("\n\n"):
        fields = dict(
            line.split(": ", 1)
            for line in block.splitlines()
            if ": " in line and not line.startswith(":")
        )
        if "event" in fields:
            events.append((fields["event"], json.loads(fields["data"])))
    return events


# ------------------------------------------------------------ M3 webhook & Kanban (Phase 3+)
WEBHOOK_SECRET = "whsec_test_secret"
GO_TAXONOMY = {
    "required_skills": [{"name": "Go", "min_years": 4}, {"name": "Kubernetes", "min_years": 2}],
    "preferred_skills": [{"name": "Kafka"}, {"name": "gRPC"}],
    "min_total_experience_years": 5,
}


async def apply(
    client: httpx.AsyncClient,
    admin_engine: AsyncEngine,
    workspace: Workspace,
    job_id: str,
    *,
    email: str | None = None,
    full_name: str | None = None,
) -> dict[str, str]:
    """Candidate applies through the public dropzone; returns ids and the status token."""
    key = await upload_resume(client, workspace, job_id)
    extra = {"full_name": full_name} if full_name else {}
    response = await submit(client, workspace, job_id, key, email=email, **extra)
    assert response.status_code == 202, response.text
    body = response.json()
    async with admin_engine.connect() as conn:
        candidate_id = await conn.scalar(
            text("SELECT candidate_id FROM job_applications WHERE id = :id"),
            {"id": body["application_id"]},
        )
    return {
        "tenant_id": str(workspace.tenant_id),
        "application_id": body["application_id"],
        "candidate_id": str(candidate_id),
        "status_token": body["status_token"],
        "events_url": body["events_url"].removeprefix("http://test"),
        "s3_key": key,
    }


def m3_payload(
    app: dict[str, str],
    *,
    score: float = 94.0,
    tracking_id: str | None = None,
    status: str = "success",
    full_name: str = "Alex Mercer",
    phone: str = "+1-555-019-2834",
    **overrides: Any,
) -> dict[str, Any]:
    """An M3 webhook envelope exactly as documented in project.md §6.3."""
    payload: dict[str, Any] = {
        "tracking_id": tracking_id or f"trk_m3_{uuid.uuid4().hex[:8]}",
        "callback_context": {
            "tenant_id": app["tenant_id"],
            "application_id": app["application_id"],
            "candidate_id": app["candidate_id"],
        },
        "status": status,
        "execution_metrics": {
            "triage_route": "gpu_vlm_vision",
            "processing_time_seconds": 42.8,
            "auto_repair_triggered": False,
        },
        "candidate_profile": {
            "contact": {
                "full_name": full_name,
                "email": "alex.mercer@example.com",
                "phone": phone,
                "linkedin": "https://linkedin.com/in/alexmercer",
                "github": "https://github.com/alexmercer-dev",
                "location": "San Francisco, CA",
            },
            "work_experience": [
                {
                    "company": "Stripe",
                    "title": "Senior Software Engineer",
                    "start_date": "2021-03",
                    "end_date": "Present",
                    "highlights": ["Led a Go payments gateway processing 30k TPS"],
                    "technologies": ["Go", "Kubernetes", "PostgreSQL", "Kafka"],
                }
            ],
            "education": [
                {
                    "institution": "UC Berkeley",
                    "degree": "B.S. Computer Science",
                    "graduation_year": 2018,
                }
            ],
            "skills": ["Go", "Kubernetes", "PostgreSQL", "Kafka", "gRPC"],
        },
        "deterministic_match": {
            "overall_score": score,
            "breakdown": {
                "matched_required": [
                    {"skill": "Go", "evidence": "Used at Stripe (3+ years)"},
                    {"skill": "Kubernetes", "evidence": "Production deployments at Stripe"},
                ],
                "missing_required": [],
                "matched_preferred": [{"skill": "Kafka", "evidence": "Listed in Stripe stack"}],
                "missing_preferred": [{"skill": "gRPC"}],
            },
            "experience_match": {
                "candidate_years": 6.0,
                "required_years": 5.0,
                "meets_criteria": True,
            },
        },
    }
    payload.update(overrides)
    return payload


async def post_webhook(
    client: httpx.AsyncClient, payload: dict[str, Any], *, secret: str = WEBHOOK_SECRET
) -> httpx.Response:
    body = json.dumps(payload).encode()
    return await client.post(
        f"{API}/webhooks/extractor",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Webhook-Signature": sign_payload(body, secret),
        },
    )


async def application_row(admin_engine: AsyncEngine, application_id: str) -> Any:
    async with admin_engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    """
                    SELECT a.status::text AS status, a.deterministic_match_score AS score,
                           a.match_breakdown_json AS breakdown, a.extractor_tracking_id AS tracking,
                           a.extraction_error, a.extracted_profile_json AS extracted,
                           a.confirmed_profile_json AS confirmed, a.confirmed_at,
                           a.version, s.name AS stage,
                           c.email, c.first_name, c.last_name, c.phone, c.linkedin_url,
                           c.github_url, c.location, c.parsed_profile_json AS profile
                    FROM job_applications a
                    JOIN candidates c ON c.id = a.candidate_id
                    LEFT JOIN pipeline_stages s ON s.id = a.current_stage_id
                    WHERE a.id = :id
                    """
                ),
                {"id": application_id},
            )
        ).one()


async def board(
    client: httpx.AsyncClient,
    workspace: Workspace,
    job_id: str,
    role: UserRole = UserRole.RECRUITER,
    **params: Any,
) -> dict[str, Any]:
    response = await client.get(
        f"{API}/jobs/{job_id}/pipeline", params=params, headers=workspace.headers(role)
    )
    assert response.status_code == 200, response.text
    return response.json()


def column(pipeline: dict[str, Any], name: str) -> dict[str, Any]:
    return next(stage for stage in pipeline["stages"] if stage["name"] == name)


class RecordingEmbeddingQueue:
    """Stands in for the ARQ embedding queue."""

    def __init__(self) -> None:
        self.jobs: list[tuple[uuid.UUID, uuid.UUID]] = []
        self.resurfacing: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def enqueue_embedding(self, tenant_id: uuid.UUID, candidate_id: uuid.UUID) -> None:
        self.jobs.append((tenant_id, candidate_id))

    async def enqueue_resurfacing(self, tenant_id: uuid.UUID, job_id: uuid.UUID) -> None:
        self.resurfacing.append((tenant_id, job_id))


class RecordingCommunicationsQueue:
    """Stands in for the ARQ communications queue."""

    def __init__(self) -> None:
        self.screenings: list[tuple[uuid.UUID, uuid.UUID]] = []
        self.evaluations: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def enqueue_screening_generation(
        self, tenant_id: uuid.UUID, application_id: uuid.UUID
    ) -> None:
        self.screenings.append((tenant_id, application_id))

    async def enqueue_screening_evaluation(
        self, tenant_id: uuid.UUID, screening_id: uuid.UUID
    ) -> None:
        self.evaluations.append((tenant_id, screening_id))


# ------------------------------------------------------------ deterministic AI fakes
class FakeLLM:
    model = "fake-llama3.2:3b"

    def __init__(self, *responses: dict[str, Any] | Exception) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def generate_json(
        self, *, system: str, prompt: str, schema: dict[str, Any]
    ) -> dict[str, Any]:
        self.calls.append({"system": system, "prompt": prompt, "schema": schema})
        response = self.responses[min(len(self.calls), len(self.responses)) - 1]
        if isinstance(response, Exception):
            raise response
        return copy.deepcopy(response)


class HashingEmbedder:
    """Deterministic bag-of-words embedder (384-d, L2-normalised) for pipeline mechanics tests."""

    model_name = "test-hashing-384"
    dimensions = 384

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        for token in re.findall(r"[a-z0-9]+", text.lower()):
            bucket = int(hashlib.md5(token.encode()).hexdigest(), 16) % self.dimensions
            vector[bucket] += 1.0
        norm = math.sqrt(sum(v * v for v in vector)) or 1.0
        return [v / norm for v in vector]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


async def add_member(
    client: httpx.AsyncClient, workspace: Workspace, role: UserRole, name: str
) -> Member:
    """Sync one more Clerk identity into an existing workspace (e.g. a second interviewer)."""
    suffix = uuid.uuid4().hex[:6]
    clerk_user_id = f"user_{role.value}_{suffix}"
    email = f"{name.lower().replace(' ', '.')}.{suffix}@{workspace.subdomain}.example.com"
    token = mint_token(clerk_user_id=clerk_user_id, org_id=workspace.org_id, role=role)
    response = await client.post(
        f"{API}/auth/sync-user", json={"email": email, "full_name": name}, headers=bearer(token)
    )
    assert response.status_code == 201, response.text
    return Member(
        role=role,
        clerk_user_id=clerk_user_id,
        email=email,
        token=token,
        user_id=uuid.UUID(response.json()["user"]["id"]),
    )


class FakeTranscriber:
    """Deterministic speech-to-text stand-in: returns a fixed transcript per call."""

    engine = "fake-whisper"

    def __init__(
        self, text: str = "I would build a Kubernetes operator in Go.", *, fail: bool = False
    ) -> None:
        self.text = text
        self.fail = fail
        self.calls: list[str] = []

    def transcribe(self, path: str) -> Any:
        from app.services.screening_evaluator import Transcript, TranscriptionUnavailableError

        self.calls.append(path)
        if self.fail:
            raise TranscriptionUnavailableError("Speech-to-text model unavailable (test)")
        return Transcript(text=self.text, engine=self.engine)
