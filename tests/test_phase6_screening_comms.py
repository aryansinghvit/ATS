"""Phase 6 — L5 HR Tester: AI pre-screening questions, stage email workflows, resurfacing.

Strict testing mandate: headless Python script tests only (pytest + httpx.AsyncClient).
No web/browser testing. The local LLM is replaced by a deterministic fake (plus an outage
simulation for the template fallback); email delivery uses the in-memory log transport.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import get_settings
from app.core.database import AsyncSessionLocal, set_tenant_context
from app.models import UserRole
from app.services.communications import LogEmailSender, dispatch_due_emails, polite_send_time
from app.services.embedding_service import EmbeddingService, get_embedder
from app.services.llm_client import LLMUnavailableError, get_llm_client
from app.workers import communications_worker, embedding_worker
from tests.helpers import (
    API,
    GO_TAXONOMY,
    FakeLLM,
    HashingEmbedder,
    Workspace,
    apply,
    board,
    column,
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
AI_QUESTIONS = {
    "questions": [
        {
            "question": "You have deep Go experience; how would you approach Kubernetes operators?",
            "targets": "Kubernetes",
            "rationale": "Missing required skill",
        },
        {
            "question": "Describe how you would evaluate gRPC versus REST for internal services.",
            "targets": "gRPC",
            "rationale": "Missing preferred skill",
        },
        {
            "question": "Walk us through the hardest concurrency bug you fixed in Go.",
            "targets": "Go",
            "rationale": "Depth check",
        },
    ]
}


@pytest.fixture(scope="module")
async def workspaces(api_app: object) -> AsyncIterator[tuple[Workspace, Workspace]]:
    transport = httpx.ASGITransport(app=api_app)  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield await provision_workspace(http, "Cobalt"), await provision_workspace(http, "Dune")


@pytest.fixture
def alpha(workspaces: tuple) -> Workspace:
    return workspaces[0]


@pytest.fixture
def beta(workspaces: tuple) -> Workspace:
    return workspaces[1]


@pytest.fixture
def use_llm(api_app: Any) -> Iterator[Callable[..., FakeLLM]]:
    def install(*responses: Any) -> FakeLLM:
        fake = FakeLLM(*responses)
        api_app.dependency_overrides[get_llm_client] = lambda: fake
        return fake

    yield install
    api_app.dependency_overrides.pop(get_llm_client, None)


@pytest.fixture
async def scored_app(
    client: httpx.AsyncClient, admin_engine: AsyncEngine, alpha: Workspace
) -> dict:
    """An application scored by M3: Go matched; Kubernetes missing; gRPC preferred missing."""
    job = await create_job(client, alpha, title="Platform Engineer", taxonomy=GO_TAXONOMY)
    app = await apply(client, admin_engine, alpha, job["id"], full_name="Sam Rivera")
    payload = m3_payload(app, full_name="Sam Rivera")
    payload["deterministic_match"]["breakdown"] = {
        "matched_required": [{"skill": "Go", "evidence": "5 years"}],
        "missing_required": [{"skill": "Kubernetes"}],
        "matched_preferred": [{"skill": "Kafka"}],
        "missing_preferred": [{"skill": "gRPC"}],
    }
    await post_webhook(client, payload)
    return {**app, "job_id": job["id"]}


async def drain(sender: Any) -> dict[str, int]:
    """Dispatch every due email (other suites leave many queued in the shared test database)."""
    totals: dict[str, int] = {}
    while counts := await dispatch_due_emails(sender, limit=500):
        for key, value in counts.items():
            totals[key] = totals.get(key, 0) + value
        if set(counts) <= {"skipped"}:
            break
    return totals


async def outbox(admin_engine: AsyncEngine, application_id: str) -> list[Any]:
    async with admin_engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT template_key, status, scheduled_for, subject, body, to_email, "
                    "attempts, "
                    "last_error FROM email_outbox WHERE application_id = :id ORDER BY created_at"
                ),
                {"id": application_id},
            )
        ).all()


async def move(
    client: httpx.AsyncClient, workspace: Workspace, app_id: str, job_id: str, stage: str
) -> dict:
    pipeline = await board(client, workspace, job_id)
    card = next(c for s in pipeline["stages"] for c in s["cards"] if c["application_id"] == app_id)
    response = await client.patch(
        f"{API}/applications/{app_id}/move-stage",
        json={"to_stage_id": column(pipeline, stage)["id"], "expected_version": card["version"]},
        headers=workspace.headers(RECRUITER),
    )
    assert response.status_code == 200, response.text
    return response.json()


# =============================================================================================
# AI pre-screening question generator
# =============================================================================================
class TestScreeningQuestions:
    async def test_ai_generates_gap_targeted_questions(
        self,
        client: httpx.AsyncClient,
        alpha: Workspace,
        scored_app: dict,
        use_llm: Any,
        admin_engine: AsyncEngine,
    ) -> None:
        fake = use_llm(AI_QUESTIONS)
        response = await client.post(
            f"{API}/applications/{scored_app['application_id']}/screening-questions",
            json={"count": 3},
            headers=alpha.headers(RECRUITER),
        )
        assert response.status_code == 200, response.text
        screening = response.json()
        assert screening["source"] == "ai" and screening["model"] == FakeLLM.model
        assert screening["skill_gaps"] == ["Kubernetes", "gRPC"]
        assert [q["targets"] for q in screening["questions"]] == ["Kubernetes", "gRPC", "Go"]
        assert screening["status"] == "pending"
        expires = datetime.fromisoformat(screening["expires_at"])
        assert timedelta(hours=71) < expires - datetime.now(UTC) <= timedelta(hours=72)

        prompt = fake.calls[0]["prompt"]
        assert (
            "Missing required skills: Kubernetes" in prompt and "Sam Rivera" not in prompt
        )  # no PII
        assert "legally protected" in fake.calls[0]["system"]

        async with admin_engine.connect() as conn:
            stored = (
                await conn.execute(
                    text(
                        "SELECT access_token, generation_source FROM screenings "
                        "WHERE application_id = :id"
                    ),
                    {"id": scored_app["application_id"]},
                )
            ).one()
        # A random nonce only: portal links are HMAC-signed over it, never stored themselves.
        assert len(stored.access_token) >= 40 and stored.generation_source == "ai"

    async def test_fallback_when_ollama_is_unavailable(
        self,
        client: httpx.AsyncClient,
        alpha: Workspace,
        scored_app: dict,
        use_llm: Any,
        admin_engine: AsyncEngine,
    ) -> None:
        use_llm(LLMUnavailableError("Local LLM (Ollama) returned HTTP 404: model not found"))
        url = f"{API}/applications/{scored_app['application_id']}/screening-questions"
        response = await client.post(url, json={"count": 5}, headers=alpha.headers(ADMIN))
        assert response.status_code == 200
        screening = response.json()
        assert screening["source"] == "fallback" and "model not found" in screening["source_detail"]
        questions = screening["questions"]
        assert len(questions) == 5
        assert questions[0]["targets"] == "Kubernetes" and "Go" in questions[0]["question"]
        assert questions[1]["targets"] == "gRPC"
        assert len({q["question"] for q in questions}) == 5

        regenerated = await client.post(url, json={"count": 3}, headers=alpha.headers(ADMIN))
        assert len(regenerated.json()["questions"]) == 3
        async with admin_engine.connect() as conn:
            count = await conn.scalar(
                text("SELECT count(*) FROM screenings WHERE application_id = :id"),
                {"id": scored_app["application_id"]},
            )
        assert count == 1  # a pending screening is replaced, not duplicated

        latest = await client.get(
            f"{API}/applications/{scored_app['application_id']}/screening",
            headers=alpha.headers(HIRING_MANAGER),
        )
        assert latest.status_code == 200 and len(latest.json()["questions"]) == 3

    async def test_screening_rbac_and_validation(
        self,
        client: httpx.AsyncClient,
        alpha: Workspace,
        beta: Workspace,
        scored_app: dict,
        use_llm: Any,
    ) -> None:
        use_llm(AI_QUESTIONS)
        url = f"{API}/applications/{scored_app['application_id']}/screening-questions"
        assert (
            await client.post(url, json={"count": 3}, headers=alpha.headers(HIRING_MANAGER))
        ).status_code == 403
        assert (
            await client.post(url, json={"count": 3}, headers=alpha.headers(INTERVIEWER))
        ).status_code == 403
        assert (
            await client.post(url, json={"count": 6}, headers=alpha.headers())
        ).status_code == 422
        assert (
            await client.post(url, json={"count": 3}, headers=beta.headers())
        ).status_code == 404
        get_url = f"{API}/applications/{scored_app['application_id']}/screening"
        assert (await client.get(get_url, headers=alpha.headers(INTERVIEWER))).status_code == 403

    async def test_entering_pre_screen_queues_generation_and_worker_runs_it(
        self,
        client: httpx.AsyncClient,
        alpha: Workspace,
        scored_app: dict,
        communications_queue: Any,
    ) -> None:
        await move(client, alpha, scored_app["application_id"], scored_app["job_id"], "Pre-Screen")
        assert communications_queue.screenings[-1] == (
            alpha.tenant_id,
            uuid.UUID(scored_app["application_id"]),
        )

        source = await communications_worker.generate_screening(
            {"llm": FakeLLM(AI_QUESTIONS)}, str(alpha.tenant_id), scored_app["application_id"]
        )
        assert source == "ai"
        latest = (
            await client.get(
                f"{API}/applications/{scored_app['application_id']}/screening",
                headers=alpha.headers(),
            )
        ).json()
        assert latest["questions"][0]["targets"] == "Kubernetes"


# =============================================================================================
# Stage-triggered emails, templates, polite delayed rejections
# =============================================================================================
class TestPoliteDelay:
    @pytest.mark.parametrize(
        ("now", "delay", "tz", "expected_local"),
        [
            # Friday 16:00 + 48h = Sunday 16:00 -> Monday 09:00
            (datetime(2026, 10, 9, 16, 0, tzinfo=UTC), 48, "UTC", datetime(2026, 10, 12, 9, 0)),
            # Tuesday 03:00 + 24h = Wednesday 03:00 -> Wednesday 09:00
            (datetime(2026, 10, 6, 3, 0, tzinfo=UTC), 24, "UTC", datetime(2026, 10, 7, 9, 0)),
            # Monday 10:00 + 48h = Wednesday 10:00 (already business hours)
            (datetime(2026, 10, 5, 10, 0, tzinfo=UTC), 48, "UTC", datetime(2026, 10, 7, 10, 0)),
            # Wednesday 20:00 UTC + 36h = Friday 08:00 UTC = 04:00 New York -> Friday 09:00 New York
            (
                datetime(2026, 10, 7, 20, 0, tzinfo=UTC),
                36,
                "America/New_York",
                datetime(2026, 10, 9, 9, 0),
            ),
        ],
    )
    def test_rejections_land_in_business_hours(
        self, now: datetime, delay: int, tz: str, expected_local: datetime
    ) -> None:
        sent = polite_send_time(now, delay, tz)
        local = sent.astimezone(ZoneInfo(tz))
        assert local.replace(tzinfo=None) == expected_local
        assert sent - now >= timedelta(hours=delay)
        assert local.weekday() < 5 and 9 <= local.hour < 17


class TestStageEmails:
    async def test_application_received_email_is_sent_with_variables(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, alpha: Workspace
    ) -> None:
        job = await create_job(client, alpha, title="Data Engineer", taxonomy=GO_TAXONOMY)
        app = await apply(
            client, admin_engine, alpha, job["id"], email="jo@example.com", full_name="Jo Park"
        )
        [queued] = await outbox(admin_engine, app["application_id"])
        assert (queued.template_key, queued.status, queued.to_email) == (
            "application_received",
            "queued",
            "jo@example.com",
        )

        sender = LogEmailSender()
        await drain(sender)
        [sent_row] = await outbox(admin_engine, app["application_id"])
        assert sent_row.status == "sent"
        message = next(m for m in sender.sent if m["to"] == "jo@example.com")
        assert message["subject"] == "We received your application for Data Engineer"
        assert message["body"].startswith("Hi Jo,")
        assert alpha.name in message["body"] and "{{" not in message["body"]
        assert sent_row.subject == message["subject"]  # rendered snapshot kept for the audit trail

    async def test_interview_invite_injects_scheduling_link(
        self,
        client: httpx.AsyncClient,
        admin_engine: AsyncEngine,
        alpha: Workspace,
        scored_app: dict,
    ) -> None:
        settings = await client.put(
            f"{API}/communications/settings",
            json={
                "scheduling_url": "https://cal.com/cobalt/interview",
                "rejection_delay_hours": 48,
                "timezone": "UTC",
            },
            headers=alpha.headers(ADMIN),
        )
        assert settings.status_code == 200, settings.text
        await move(client, alpha, scored_app["application_id"], scored_app["job_id"], "Interview")
        sender = LogEmailSender()
        await drain(sender)
        invite = next(m for m in sender.sent if m["subject"].startswith("Interview invitation"))
        assert "https://cal.com/cobalt/interview" in invite["body"]

        # A job-level link overrides the workspace link.
        await client.patch(
            f"{API}/jobs/{scored_app['job_id']}",
            json={"scheduling_url": "https://calendly.com/cobalt/platform"},
            headers=alpha.headers(RECRUITER),
        )
        preview = await client.post(
            f"{API}/communications/templates/interview_invitation/preview",
            json={"application_id": scored_app["application_id"]},
            headers=alpha.headers(RECRUITER),
        )
        assert "https://calendly.com/cobalt/platform" in preview.json()["body"]

    async def test_rejection_is_delayed_cancelled_on_reversal_and_sent_when_due(
        self,
        client: httpx.AsyncClient,
        admin_engine: AsyncEngine,
        alpha: Workspace,
        scored_app: dict,
    ) -> None:
        app_id, job_id = scored_app["application_id"], scored_app["job_id"]
        await move(client, alpha, app_id, job_id, "Rejected")
        rejection = next(
            r for r in await outbox(admin_engine, app_id) if r.template_key == "rejection"
        )
        assert rejection.status == "queued"
        delay = rejection.scheduled_for - datetime.now(UTC)
        assert delay >= timedelta(hours=24)  # never instantaneous
        assert rejection.scheduled_for.weekday() < 5 and 9 <= rejection.scheduled_for.hour < 17

        sender = LogEmailSender()
        await drain(sender)
        assert not any("Your application for" in m["subject"] for m in sender.sent)  # not due yet

        await move(client, alpha, app_id, job_id, "Interview")  # decision reversed
        rows = [r for r in await outbox(admin_engine, app_id) if r.template_key == "rejection"]
        assert [r.status for r in rows] == ["cancelled"]

        await move(client, alpha, app_id, job_id, "Rejected")
        async with admin_engine.begin() as conn:  # fast-forward past the polite delay
            await conn.execute(
                text(
                    "UPDATE email_outbox SET scheduled_for = now() - interval '1 minute' "
                    "WHERE application_id = :id AND template_key = 'rejection' "
                    "AND status = 'queued'"
                ),
                {"id": app_id},
            )
        await drain(sender)
        statuses = [
            r.status for r in await outbox(admin_engine, app_id) if r.template_key == "rejection"
        ]
        assert statuses == ["cancelled", "sent"]
        rejection_mail = next(
            m for m in sender.sent if m["subject"].startswith("Your application for")
        )
        assert "move forward with other candidates" in rejection_mail["body"]

    async def test_send_time_guard_cancels_rejection_if_candidate_moved(
        self,
        client: httpx.AsyncClient,
        admin_engine: AsyncEngine,
        alpha: Workspace,
        scored_app: dict,
    ) -> None:
        app_id, job_id = scored_app["application_id"], scored_app["job_id"]
        await move(client, alpha, app_id, job_id, "Rejected")
        applied = column(await board(client, alpha, job_id), "Applied")["id"]
        async with admin_engine.begin() as conn:  # stage changed outside the move endpoint
            await conn.execute(
                text("UPDATE job_applications SET current_stage_id = :s WHERE id = :id"),
                {"s": applied, "id": app_id},
            )
            await conn.execute(
                text(
                    "UPDATE email_outbox SET scheduled_for = now() "
                    "WHERE application_id = :id AND template_key = 'rejection'"
                ),
                {"id": app_id},
            )
        await drain(LogEmailSender())
        row = next(r for r in await outbox(admin_engine, app_id) if r.template_key == "rejection")
        assert row.status == "cancelled" and "no longer in a Rejected stage" in row.last_error

    async def test_delivery_failures_retry_then_fail(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, alpha: Workspace
    ) -> None:
        job = await create_job(client, alpha, taxonomy=GO_TAXONOMY)
        app = await apply(client, admin_engine, alpha, job["id"])

        class BrokenSender:
            async def send(self, **_: Any) -> None:
                raise ConnectionError("SMTP down")

        for attempt in range(1, get_settings().EMAIL_MAX_ATTEMPTS + 1):
            async with admin_engine.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE email_outbox SET scheduled_for = now() WHERE application_id = :id"
                    ),
                    {"id": app["application_id"]},
                )
            await drain(BrokenSender())
            [row] = await outbox(admin_engine, app["application_id"])
            assert row.attempts == attempt and "SMTP down" in row.last_error
        assert row.status == "failed"

    async def test_email_log_endpoint_and_isolation(
        self, client: httpx.AsyncClient, alpha: Workspace, beta: Workspace, scored_app: dict
    ) -> None:
        url = f"{API}/applications/{scored_app['application_id']}/emails"
        emails = (await client.get(url, headers=alpha.headers(HIRING_MANAGER))).json()
        assert [e["template_key"] for e in emails][:1] == ["application_received"]
        assert (await client.get(url, headers=alpha.headers(INTERVIEWER))).status_code == 403
        assert (await client.get(url, headers=beta.headers())).status_code == 404


class TestTemplatesAndSettings:
    async def test_template_editor_validation_preview_and_reset(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, alpha: Workspace
    ) -> None:
        templates = (
            await client.get(f"{API}/communications/templates", headers=alpha.headers(RECRUITER))
        ).json()
        assert [t["key"] for t in templates] == [
            "application_received", "interview_invitation", "offer_extended",
            "rejection", "screening_invitation", "talent_invitation",
        ]  # fmt: skip
        assert {"candidate_first_name", "job_title", "company_name"} <= set(
            templates[0]["variables"]
        )

        url = f"{API}/communications/templates/application_received"
        bad = await client.put(
            url, json={"subject": "Hi {{salary}}", "body": "x"}, headers=alpha.headers(RECRUITER)
        )
        assert bad.status_code == 422 and "salary" in bad.json()["detail"]

        custom = await client.put(
            url,
            json={
                "subject": "Thanks, {{candidate_name}}!",
                "body": "Welcome to {{company_name}}'s process for {{job_title}}.",
            },
            headers=alpha.headers(RECRUITER),
        )
        assert custom.status_code == 200 and custom.json()["customized"] is True
        preview = await client.post(f"{url}/preview", json={}, headers=alpha.headers(RECRUITER))
        assert preview.json()["subject"] == "Thanks, Alex Mercer!"
        assert (
            preview.json()["body"]
            == f"Welcome to {alpha.name}'s process for Senior Backend Engineer."
        )

        job = await create_job(client, alpha, title="SRE", taxonomy=GO_TAXONOMY)
        app = await apply(client, admin_engine, alpha, job["id"], full_name="Lee Chan")
        sender = LogEmailSender()
        await drain(sender)
        assert any(m["subject"] == "Thanks, Lee Chan!" for m in sender.sent)

        disabled = await client.put(
            url, json={"subject": "x", "body": "y", "enabled": False}, headers=alpha.headers(ADMIN)
        )
        assert disabled.json()["enabled"] is False
        second = await apply(client, admin_engine, alpha, job["id"])
        assert await outbox(admin_engine, second["application_id"]) == []  # disabled -> not queued

        reset = await client.delete(url, headers=alpha.headers(ADMIN))
        assert reset.json()["customized"] is False and reset.json()["enabled"] is True
        assert app  # first application's email was sent with the custom template

    async def test_template_and_settings_rbac(
        self, client: httpx.AsyncClient, alpha: Workspace
    ) -> None:
        for role in (HIRING_MANAGER, INTERVIEWER):
            assert (
                await client.get(f"{API}/communications/templates", headers=alpha.headers(role))
            ).status_code == 403
            assert (
                await client.get(f"{API}/communications/settings", headers=alpha.headers(role))
            ).status_code == 403
        assert (
            await client.get(
                f"{API}/communications/templates/nope/preview", headers=alpha.headers()
            )
        ).status_code == 405
        missing = await client.post(
            f"{API}/communications/templates/nope/preview", json={}, headers=alpha.headers()
        )
        assert missing.status_code == 404

    @pytest.mark.parametrize(
        "payload",
        [
            {"scheduling_url": "http://cal.com/x"},
            {"rejection_delay_hours": 12},
            {"rejection_delay_hours": 100},
            {"timezone": "Mars/Olympus"},
        ],
    )
    async def test_settings_validation(
        self, client: httpx.AsyncClient, alpha: Workspace, payload: dict
    ) -> None:
        response = await client.put(
            f"{API}/communications/settings", json=payload, headers=alpha.headers(ADMIN)
        )
        assert response.status_code == 422


# =============================================================================================
# Applicant resurfacing on job publish + 1-click invite
# =============================================================================================
class TestResurfacing:
    @pytest.fixture
    async def talent(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, alpha: Workspace, api_app: Any
    ) -> AsyncIterator[dict[str, str]]:
        old_job = await create_job(client, alpha, title="Old Role", taxonomy=GO_TAXONOMY)
        people = {
            "Gopher Raft": ["Go", "Raft", "distributed", "systems", "Kubernetes"],
            "Rita Pixel": ["React", "TypeScript", "CSS"],
        }
        seeded = {}
        for name, skills in people.items():
            app = await apply(client, admin_engine, alpha, old_job["id"], full_name=name)
            payload = m3_payload(app, full_name=name)
            payload["candidate_profile"] = {
                "skills": skills,
                "work_experience": [{"title": f"{skills[0]} Engineer"}],
            }
            await post_webhook(client, payload)
            seeded[name] = app
        embedder = HashingEmbedder()
        async with AsyncSessionLocal() as session:
            await set_tenant_context(session, alpha.tenant_id)
            for app in seeded.values():
                await EmbeddingService(session, embedder).index_candidate(
                    uuid.UUID(app["candidate_id"])
                )
        api_app.dependency_overrides[get_embedder] = lambda: embedder
        yield seeded
        api_app.dependency_overrides.pop(get_embedder, None)

    async def test_publishing_queues_scan_that_alerts_the_recruiter(
        self,
        client: httpx.AsyncClient,
        alpha: Workspace,
        talent: dict,
        embedding_queue: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(get_settings(), "RESURFACING_THRESHOLD", 0.3)
        job = await create_job(
            client,
            alpha,
            title="Go Distributed Systems Engineer",
            taxonomy={
                "required_skills": [{"name": "Go"}, {"name": "Raft"}],
                "preferred_skills": [],
            },
            description_markdown="Go, Raft, distributed systems, Kubernetes",
        )
        assert embedding_queue.resurfacing[-1] == (alpha.tenant_id, uuid.UUID(job["id"]))

        result = await embedding_worker.resurface_for_job(
            {"embedder": HashingEmbedder()}, str(alpha.tenant_id), job["id"]
        )
        assert result["new_matches"] >= 1 and result["notified"] == 1

        suggestions = (
            await client.get(
                f"{API}/talent-pool/jobs/{job['id']}/suggestions", headers=alpha.headers()
            )
        ).json()
        assert (
            suggestions[0]["full_name"] == "Gopher Raft" and suggestions[0]["status"] == "suggested"
        )
        assert suggestions[0]["applications"][0]["job_title"] == "Old Role"

        inbox = (await client.get(f"{API}/notifications", headers=alpha.headers(RECRUITER))).json()
        alert = next(
            n for n in inbox["items"] if n["type"] == "resurfacing" and n["entity_id"] == job["id"]
        )
        assert (
            alert["title"].startswith("Found ")
            and "≥30% match for Go Distributed Systems Engineer" in alert["title"]
        )

        rerun = await client.post(
            f"{API}/talent-pool/jobs/{job['id']}/resurface", headers=alpha.headers(RECRUITER)
        )
        assert (
            rerun.json()["new_matches"] == 0 and rerun.json()["notified"] == 0
        )  # no duplicate alerts

    async def test_one_click_invite_creates_application_and_email(
        self,
        client: httpx.AsyncClient,
        admin_engine: AsyncEngine,
        alpha: Workspace,
        talent: dict,
        extraction_queue: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(get_settings(), "RESURFACING_THRESHOLD", 0.3)
        job = await create_job(
            client,
            alpha,
            title="Raft Engineer",
            taxonomy={"required_skills": [{"name": "Raft"}, {"name": "Go"}]},
            description_markdown="Go and Raft for distributed systems on Kubernetes",
        )
        await client.post(
            f"{API}/talent-pool/jobs/{job['id']}/resurface", headers=alpha.headers(RECRUITER)
        )
        candidate_id = talent["Gopher Raft"]["candidate_id"]
        url = f"{API}/talent-pool/candidates/{candidate_id}/invite-to-job"

        invited = await client.post(
            url, json={"job_id": job["id"]}, headers=alpha.headers(RECRUITER)
        )

        assert invited.status_code == 200, invited.text
        body = invited.json()
        assert body["status"] == "pending_extraction" and body["email_queued"] is True
        assert (alpha.tenant_id, uuid.UUID(body["application_id"])) in extraction_queue.jobs
        applied = column(await board(client, alpha, job["id"]), "Applied")["cards"]
        assert body["application_id"] in {c["application_id"] for c in applied}
        [email] = await outbox(admin_engine, body["application_id"])
        assert email.template_key == "talent_invitation"
        suggestions = (
            await client.get(
                f"{API}/talent-pool/jobs/{job['id']}/suggestions", headers=alpha.headers()
            )
        ).json()
        assert (
            next(s for s in suggestions if s["candidate_id"] == candidate_id)["status"] == "invited"
        )

        again = await client.post(url, json={"job_id": job["id"]}, headers=alpha.headers(RECRUITER))
        assert again.status_code == 409
        draft = await create_job(client, alpha, status="draft", taxonomy=GO_TAXONOMY)
        assert (
            await client.post(url, json={"job_id": draft["id"]}, headers=alpha.headers())
        ).status_code == 422
        hm = await client.post(
            url, json={"job_id": job["id"]}, headers=alpha.headers(HIRING_MANAGER)
        )
        assert hm.status_code == 403

    async def test_dismiss_and_exclusion_of_existing_applicants(
        self,
        client: httpx.AsyncClient,
        alpha: Workspace,
        talent: dict,
        embedding_queue: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(get_settings(), "RESURFACING_THRESHOLD", 0.0)
        draft = await create_job(
            client, alpha, status="draft", taxonomy={"required_skills": [{"name": "React"}]}
        )
        before = len(embedding_queue.resurfacing)
        published = await client.patch(
            f"{API}/jobs/{draft['id']}", json={"status": "published"}, headers=alpha.headers()
        )
        assert published.status_code == 200
        assert embedding_queue.resurfacing[before:] == [(alpha.tenant_id, uuid.UUID(draft["id"]))]

        await client.post(
            f"{API}/talent-pool/jobs/{draft['id']}/resurface", headers=alpha.headers(RECRUITER)
        )
        suggestions = (
            await client.get(
                f"{API}/talent-pool/jobs/{draft['id']}/suggestions", headers=alpha.headers()
            )
        ).json()
        rita = next(s for s in suggestions if s["full_name"] == "Rita Pixel")
        dismissed = await client.post(
            f"{API}/talent-pool/jobs/{draft['id']}/suggestions/{rita['candidate_id']}/dismiss",
            headers=alpha.headers(RECRUITER),
        )
        assert dismissed.status_code == 204
        remaining = (
            await client.get(
                f"{API}/talent-pool/jobs/{draft['id']}/suggestions", headers=alpha.headers()
            )
        ).json()
        assert rita["candidate_id"] not in {s["candidate_id"] for s in remaining}

        # Candidates already in this job's pipeline are never resurfaced for it.
        gopher = talent["Gopher Raft"]["candidate_id"]
        invited = await client.post(
            f"{API}/talent-pool/candidates/{gopher}/invite-to-job",
            json={"job_id": draft["id"]},
            headers=alpha.headers(RECRUITER),
        )
        assert invited.status_code == 200
        async with AsyncSessionLocal() as session:
            await set_tenant_context(session, alpha.tenant_id)
            await session.execute(
                text("DELETE FROM job_talent_matches WHERE job_id = :job"), {"job": draft["id"]}
            )
            await session.commit()
        await client.post(
            f"{API}/talent-pool/jobs/{draft['id']}/resurface", headers=alpha.headers(RECRUITER)
        )
        rescanned = (
            await client.get(
                f"{API}/talent-pool/jobs/{draft['id']}/suggestions", headers=alpha.headers()
            )
        ).json()
        assert gopher not in {s["candidate_id"] for s in rescanned}
