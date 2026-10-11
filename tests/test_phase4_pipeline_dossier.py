"""Phase 4 — L5 HR Tester: Kanban triage controls, candidate dossier, stage side effects.

Strict testing mandate: headless Python script tests only (pytest + httpx.AsyncClient).
No web/browser testing.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.models import UserRole
from app.services.storage import DOCX_MIME
from tests.helpers import (
    API,
    DOCX_BYTES,
    GO_TAXONOMY,
    PDF_BYTES,
    Workspace,
    apply,
    board,
    column,
    create_job,
    m3_payload,
    post_webhook,
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

PROJECTS = [
    {
        "name": "Ledger",
        "description": "Multi-region Raft-backed payment ledger handling 50k TPS",
        "technologies": ["Go", "Raft"],
        "url": "https://github.com/alexmercer-dev/ledger",
    }
]


@pytest.fixture(scope="module")
async def workspaces(api_app: object) -> AsyncIterator[tuple[Workspace, Workspace]]:
    transport = httpx.ASGITransport(app=api_app)  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield await provision_workspace(http, "Vertex"), await provision_workspace(http, "Lumen")


@pytest.fixture
def alpha(workspaces: tuple[Workspace, Workspace]) -> Workspace:
    return workspaces[0]


@pytest.fixture
def beta(workspaces: tuple[Workspace, Workspace]) -> Workspace:
    return workspaces[1]


@pytest.fixture
async def job(client: httpx.AsyncClient, alpha: Workspace) -> dict[str, Any]:
    return await create_job(
        client, alpha, title="Distributed Systems Engineer", taxonomy=GO_TAXONOMY
    )


def scored(
    app: dict[str, str],
    score: float,
    *,
    missing: list[str] | None = None,
    full_name: str = "Alex Mercer",
) -> dict[str, Any]:
    payload = m3_payload(app, score=score, full_name=full_name)
    payload["candidate_profile"]["projects"] = PROJECTS
    payload["candidate_profile"]["summary"] = "Backend engineer focused on payments."
    if missing is not None:
        payload["deterministic_match"]["breakdown"]["missing_required"] = [
            {"skill": skill} for skill in missing
        ]
        payload["deterministic_match"]["breakdown"]["matched_required"] = [
            {"skill": s, "evidence": "resume"} for s in ("Go", "Kubernetes") if s not in missing
        ]
    return payload


async def dossier(
    client: httpx.AsyncClient, workspace: Workspace, application_id: str, role=RECRUITER
) -> httpx.Response:
    return await client.get(
        f"{API}/applications/{application_id}/dossier", headers=workspace.headers(role)
    )


# =============================================================================================
# Candidate dossier (split view): parsed profile, match breakdown, resume viewer, history
# =============================================================================================
class TestCandidateDossier:
    async def test_recruiter_sees_full_dossier_with_resume_and_history(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, alpha: Workspace, job: dict
    ) -> None:
        app = await apply(client, admin_engine, alpha, job["id"], email="alex@example.com")
        await post_webhook(client, scored(app, 94.0, missing=[]))
        pipeline = await board(client, alpha, job["id"])
        move = await client.patch(
            f"{API}/applications/{app['application_id']}/move-stage",
            json={"to_stage_id": column(pipeline, "Tech Screen")["id"], "expected_version": 1},
            headers=alpha.headers(RECRUITER),
        )
        assert move.status_code == 200

        response = await dossier(client, alpha, app["application_id"])

        assert response.status_code == 200, response.text
        data = response.json()
        assert data["redacted"] is False
        assert data["job"] == {"id": job["id"], "title": "Distributed Systems Engineer"}
        assert data["stage"]["name"] == "Tech Screen" and data["status"] == "applied"
        assert data["candidate"]["full_name"] == "Alex Mercer"
        assert data["candidate"]["email"] == "alex@example.com"
        assert data["candidate"]["phone"] == "+1-555-019-2834"
        assert data["candidate"]["github_url"] == "https://github.com/alexmercer-dev"

        profile = data["profile"]
        assert profile["summary"] == "Backend engineer focused on payments."
        assert profile["skills"] == ["Go", "Kubernetes", "PostgreSQL", "Kafka", "gRPC"]
        assert profile["work_experience"][0]["company"] == "Stripe"
        assert profile["work_experience"][0]["technologies"] == [
            "Go",
            "Kubernetes",
            "PostgreSQL",
            "Kafka",
        ]
        assert profile["education"][0] == {
            "institution": "UC Berkeley",
            "degree": "B.S. Computer Science",
            "graduation_year": 2018,
        }
        assert profile["projects"][0]["name"] == "Ledger"

        match = data["match"]
        assert (
            match["deterministic_match_score"] == 94.0 and match["extraction_status"] == "complete"
        )
        assert match["matched_required"] == [
            {"skill": "Go", "evidence": "resume"},
            {"skill": "Kubernetes", "evidence": "resume"},
        ]
        assert match["missing_required"] == []
        assert match["matched_preferred"] == [
            {"skill": "Kafka", "evidence": "Listed in Stripe stack"}
        ]
        assert match["missing_preferred"] == [{"skill": "gRPC", "evidence": None}]
        assert match["experience_match"]["meets_criteria"] is True

        resume = data["resume"]
        assert resume["kind"] == "pdf" and resume["mime_type"] == "application/pdf"
        assert resume["file_name"] == "Jane Doe Resume.pdf" and resume["url_expires_in"] == 900
        download = await client.get(resume["url"])
        assert download.status_code == 200 and download.content == PDF_BYTES
        assert download.headers["content-type"] == "application/pdf"

        assert [(e["from_stage"], e["to_stage"]) for e in data["stage_history"]] == [
            ("Applied", "Tech Screen")
        ]
        assert data["stage_history"][0]["actor_name"] == f"{alpha.name.split()[0]} Recruiter"

        async with admin_engine.connect() as conn:
            audit = (
                await conn.execute(
                    text(
                        "SELECT actor_id, metadata_json FROM audit_logs "
                        "WHERE entity_id = :id AND action = 'dossier_viewed'"
                    ),
                    {"id": app["application_id"]},
                )
            ).one()
        assert audit.actor_id == alpha.members[RECRUITER].user_id
        assert audit.metadata_json == {"redacted": False, "resume_url_issued": True}

    async def test_blind_interviewer_gets_redacted_dossier(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, alpha: Workspace, job: dict
    ) -> None:
        app = await apply(client, admin_engine, alpha, job["id"], email="secret.person@example.com")
        await post_webhook(
            client, scored(app, 88.0, missing=["Kubernetes"], full_name="Secret Person")
        )

        response = await dossier(client, alpha, app["application_id"], role=INTERVIEWER)

        assert response.status_code == 200
        data = response.json()
        assert data["redacted"] is True
        assert data["candidate"]["display_name"].startswith("Candidate ")
        for field in ("full_name", "email", "phone", "linkedin_url", "github_url", "location"):
            assert data["candidate"][field] is None, field
        assert data["match"] is None  # deterministic score is redacted for interviewers
        assert data["resume"]["url"] is None and data["resume"]["file_name"] is None
        assert data["profile"]["summary"] is None
        assert "graduation_year" not in data["profile"]["education"][0]
        assert data["profile"]["work_experience"][0]["company"] == "Stripe"  # skills context kept
        body = response.text
        for secret in ("Secret Person", "secret.person@example.com", "+1-555-019-2834", "2018"):
            assert secret not in body, secret

        hm = await dossier(client, alpha, app["application_id"], role=HIRING_MANAGER)
        assert (
            hm.json()["redacted"] is False
            and hm.json()["match"]["deterministic_match_score"] == 88.0
        )

    async def test_pending_and_docx_dossiers(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, alpha: Workspace, job: dict
    ) -> None:
        key = await upload_resume(
            client, alpha, job["id"], content=DOCX_BYTES, file_name="cv.docx", mime_type=DOCX_MIME
        )
        submitted = await submit(client, alpha, job["id"], key, full_name="Docx Person")
        data = (await dossier(client, alpha, submitted.json()["application_id"])).json()
        assert data["resume"]["kind"] == "docx" and data["resume"]["mime_type"] == DOCX_MIME
        assert data["match"]["extraction_status"] == "pending"
        assert data["match"]["deterministic_match_score"] is None
        assert data["profile"]["work_experience"] == [] and data["stage_history"] == []
        assert data["candidate"]["full_name"] == "Docx Person"

    async def test_confirmed_skills_appear_as_verified(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, alpha: Workspace, job: dict
    ) -> None:
        app = await apply(client, admin_engine, alpha, job["id"])
        await post_webhook(client, scored(app, 70.0))
        confirm = await client.post(
            f"{API}/public/applications/{app['application_id']}/confirm",
            params={"token": app["status_token"]},
            json={
                "full_name": "Alex Mercer",
                "email": "alex.v@example.com",
                "skills": ["Go", "Rust"],
            },
        )
        assert confirm.status_code == 200
        data = (await dossier(client, alpha, app["application_id"])).json()
        assert data["profile"]["verified_skills"] == ["Go", "Rust"]
        assert data["confirmed_at"] is not None

    async def test_dossier_is_tenant_isolated_and_authenticated(
        self,
        client: httpx.AsyncClient,
        admin_engine: AsyncEngine,
        alpha: Workspace,
        beta: Workspace,
        job: dict,
    ) -> None:
        app = await apply(client, admin_engine, alpha, job["id"])
        assert (await dossier(client, beta, app["application_id"], role=ADMIN)).status_code == 404
        anonymous = await client.get(f"{API}/applications/{app['application_id']}/dossier")
        assert anonymous.status_code == 401
        missing = await dossier(client, alpha, str(uuid.uuid4()))
        assert missing.status_code == 404


# =============================================================================================
# Deterministic triage: sort and filter controls
# =============================================================================================
class TestPipelineTriageControls:
    @pytest.fixture
    async def populated(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, alpha: Workspace
    ) -> dict[str, Any]:
        job = await create_job(client, alpha, title="Triage Engineer", taxonomy=GO_TAXONOMY)
        candidates = {}
        for name, score, missing in (
            ("Ana Silva", 91.0, []),
            ("Ben Okafor", 64.0, ["Go", "Kubernetes"]),
            ("Chen Wei", 78.0, ["Kubernetes"]),
            ("Dana Levi", 85.0, []),
        ):
            app = await apply(
                client,
                admin_engine,
                alpha,
                job["id"],
                email=f"{name.split()[0].lower()}@example.com",
            )
            await post_webhook(client, scored(app, score, missing=missing, full_name=name))
            candidates[name] = app
        candidates["Pending Pat"] = await apply(
            client, admin_engine, alpha, job["id"], email="pat@example.com", full_name="Pending Pat"
        )
        return {"job": job, "apps": candidates}

    @staticmethod
    def names(pipeline: dict[str, Any], stage: str = "Applied") -> list[str | None]:
        return [card["candidate"]["full_name"] for card in column(pipeline, stage)["cards"]]

    async def test_sort_modes(
        self, client: httpx.AsyncClient, alpha: Workspace, populated: dict[str, Any]
    ) -> None:
        job_id = populated["job"]["id"]
        default = await board(client, alpha, job_id)
        assert self.names(default) == [
            "Ana Silva",
            "Dana Levi",
            "Chen Wei",
            "Ben Okafor",
            "Pending Pat",
        ]
        assert default["filters"]["sort"] == "default"

        assert self.names(await board(client, alpha, job_id, sort="applied_asc")) == [
            "Ana Silva", "Ben Okafor", "Chen Wei", "Dana Levi", "Pending Pat"
        ]  # fmt: skip
        assert self.names(await board(client, alpha, job_id, sort="applied_desc")) == [
            "Pending Pat", "Dana Levi", "Chen Wei", "Ben Okafor", "Ana Silva"
        ]  # fmt: skip
        # Fewest missing required skills first (ties by score), unscored candidates last.
        assert self.names(await board(client, alpha, job_id, sort="missing_skills")) == [
            "Ana Silva", "Dana Levi", "Chen Wei", "Ben Okafor", "Pending Pat"
        ]  # fmt: skip

        # "score" applies to every column, not only Applied.
        pipeline = await board(client, alpha, job_id)
        interview = column(pipeline, "Interview")["id"]
        for name in ("Ben Okafor", "Ana Silva"):  # Ben enters first (FIFO would list him first)
            card = next(
                c
                for c in column(pipeline, "Applied")["cards"]
                if c["candidate"]["full_name"] == name
            )
            moved = await client.patch(
                f"{API}/applications/{card['application_id']}/move-stage",
                json={"to_stage_id": interview, "expected_version": card["version"]},
                headers=alpha.headers(RECRUITER),
            )
            assert moved.status_code == 200
        assert self.names(await board(client, alpha, job_id), "Interview") == [
            "Ben Okafor",
            "Ana Silva",
        ]
        assert self.names(await board(client, alpha, job_id, sort="score"), "Interview") == [
            "Ana Silva", "Ben Okafor"
        ]  # fmt: skip

    async def test_filters_by_score_missing_skill_and_text(
        self, client: httpx.AsyncClient, alpha: Workspace, populated: dict[str, Any]
    ) -> None:
        job_id = populated["job"]["id"]
        strong = await board(client, alpha, job_id, min_score=80)
        assert self.names(strong) == ["Ana Silva", "Dana Levi"]
        assert column(strong, "Applied")["total_count"] == 5  # unfiltered count for "2 of 5"
        assert strong["job"]["required_skills"] == ["Go", "Kubernetes"]

        lacking_k8s = await board(client, alpha, job_id, missing_skill="kubernetes")
        assert self.names(lacking_k8s) == ["Chen Wei", "Ben Okafor"]
        assert self.names(await board(client, alpha, job_id, missing_skill="Go")) == ["Ben Okafor"]

        assert self.names(await board(client, alpha, job_id, q="dana")) == ["Dana Levi"]
        assert self.names(await board(client, alpha, job_id, q="PAT@EXAMPLE")) == ["Pending Pat"]
        combined = await board(
            client, alpha, job_id, min_score=70, missing_skill="Kubernetes", sort="score"
        )
        assert self.names(combined) == ["Chen Wei"]

        for params in ({"sort": "random"}, {"min_score": 120}, {"min_score": -1}):
            response = await client.get(
                f"{API}/jobs/{job_id}/pipeline", params=params, headers=alpha.headers(RECRUITER)
            )
            assert response.status_code == 422, params


# =============================================================================================
# Stage transition side effects and validation rules
# =============================================================================================
class TestStageTransitionRules:
    async def test_moves_drive_application_status_and_history(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, alpha: Workspace, job: dict
    ) -> None:
        app = await apply(client, admin_engine, alpha, job["id"])
        await post_webhook(client, scored(app, 90.0))
        pipeline = await board(client, alpha, job["id"])
        version = 1
        for stage, status in (
            ("Interview", "applied"),
            ("Offer", "offered"),
            ("Hired", "hired"),
            ("Interview", "applied"),  # a reversed decision resets the status
            ("Rejected", "rejected"),
        ):
            response = await client.patch(
                f"{API}/applications/{app['application_id']}/move-stage",
                json={"to_stage_id": column(pipeline, stage)["id"], "expected_version": version},
                headers=alpha.headers(ADMIN),
            )
            assert response.status_code == 200, (stage, response.text)
            assert response.json()["status"] == status, stage
            version = response.json()["version"]

        data = (await dossier(client, alpha, app["application_id"])).json()
        assert data["status"] == "rejected" and data["stage"]["name"] == "Rejected"
        assert [e["to_stage"] for e in data["stage_history"]] == [
            "Interview", "Offer", "Hired", "Interview", "Rejected"
        ]  # fmt: skip

    async def test_archived_job_pipeline_is_read_only(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, alpha: Workspace
    ) -> None:
        job = await create_job(client, alpha, taxonomy=GO_TAXONOMY)
        app = await apply(client, admin_engine, alpha, job["id"])
        pipeline = await board(client, alpha, job["id"])
        for status in ("closed", "archived"):
            changed = await client.patch(
                f"{API}/jobs/{job['id']}", json={"status": status}, headers=alpha.headers(ADMIN)
            )
            assert changed.status_code == 200, changed.text

        response = await client.patch(
            f"{API}/applications/{app['application_id']}/move-stage",
            json={"to_stage_id": column(pipeline, "Interview")["id"], "expected_version": 1},
            headers=alpha.headers(ADMIN),
        )
        assert response.status_code == 422 and "archived" in response.json()["detail"]
        assert (await board(client, alpha, job["id"]))["job"][
            "status"
        ] == "archived"  # still viewable

    async def test_closed_job_still_allows_offer_decisions(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, alpha: Workspace
    ) -> None:
        job = await create_job(client, alpha, taxonomy=GO_TAXONOMY)
        app = await apply(client, admin_engine, alpha, job["id"])
        pipeline = await board(client, alpha, job["id"])
        await client.patch(
            f"{API}/jobs/{job['id']}", json={"status": "closed"}, headers=alpha.headers()
        )
        response = await client.patch(
            f"{API}/applications/{app['application_id']}/move-stage",
            json={"to_stage_id": column(pipeline, "Offer")["id"], "expected_version": 1},
            headers=alpha.headers(RECRUITER),
        )
        assert response.status_code == 200 and response.json()["status"] == "offered"


def test_payload_helper_shapes_breakdown() -> None:
    app = {
        "tenant_id": str(uuid.uuid4()),
        "application_id": str(uuid.uuid4()),
        "candidate_id": str(uuid.uuid4()),
    }
    payload = scored(app, 50.0, missing=["Go"])
    assert json.loads(json.dumps(payload))["deterministic_match"]["breakdown"][
        "missing_required"
    ] == [{"skill": "Go"}]
