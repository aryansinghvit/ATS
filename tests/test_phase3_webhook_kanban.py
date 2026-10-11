"""Phase 3.4 — L5 HR Tester: M3 extraction queue, HMAC webhook, score-ranked Kanban pipeline.

Strict testing mandate: headless Python script tests only (pytest + httpx.AsyncClient).
No web/browser testing. The M3 microservice is mocked at the HTTP layer (httpx.MockTransport)
and its webhook callbacks are generated and HMAC-signed by the tests.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from email.parser import BytesParser
from email.policy import default as email_policy
from typing import Any

import httpx
import pytest
from arq import Retry
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import get_settings
from app.core.database import AsyncSessionLocal, set_tenant_context
from app.models import Job, UserRole
from app.services.events import InMemoryEventBroker, get_event_broker, pipeline_channel
from app.services.extraction_queue import ArqExtractionQueue
from app.services.job_generator import build_m3_job_description
from app.services.m3_client import M3ExtractorClient
from app.services.storage import get_storage_service
from app.services.webhook_service import sign_payload
from app.workers import extraction_worker
from tests.helpers import (
    API,
    GO_TAXONOMY,
    WEBHOOK_SECRET,
    Workspace,
    application_row,
    apply,
    board,
    column,
    create_job,
    m3_payload,
    parse_sse,
    post_webhook,
    provision_workspace,
)

ADMIN, RECRUITER, HIRING_MANAGER, INTERVIEWER = (
    UserRole.ADMIN,
    UserRole.RECRUITER,
    UserRole.HIRING_MANAGER,
    UserRole.INTERVIEWER,
)


@pytest.fixture(scope="module")
async def workspaces(api_app: object) -> AsyncIterator[tuple[Workspace, Workspace]]:
    transport = httpx.ASGITransport(app=api_app)  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield await provision_workspace(http, "Helix"), await provision_workspace(http, "Quasar")


@pytest.fixture
def alpha(workspaces: tuple[Workspace, Workspace]) -> Workspace:
    return workspaces[0]


@pytest.fixture
def beta(workspaces: tuple[Workspace, Workspace]) -> Workspace:
    return workspaces[1]


@pytest.fixture
async def job(client: httpx.AsyncClient, alpha: Workspace) -> dict[str, Any]:
    return await create_job(client, alpha, title="Senior Backend Engineer", taxonomy=GO_TAXONOMY)


class M3Mock:
    """In-process stand-in for the M3 extractor's HTTP API."""

    def __init__(self, *responses: httpx.Response | Exception, healthy: bool = True) -> None:
        self.responses = list(responses) or [
            httpx.Response(202, json={"status": "queued", "tracking_id": "trk_m3_88301fa0"})
        ]
        self.requests: list[httpx.Request] = []
        self.healthy = healthy

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200 if self.healthy else 503)
        self.requests.append(request)
        response = self.responses[min(len(self.requests), len(self.responses)) - 1]
        if isinstance(response, Exception):
            raise response
        return response

    def client(self) -> M3ExtractorClient:
        return M3ExtractorClient(
            get_settings().M3_EXTRACTOR_URL,
            get_settings().M3_EXTRACTOR_API_KEY.get_secret_value(),
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(self.handler)),
        )


def worker_ctx(m3: M3Mock, *, job_try: int = 1, **extra: Any) -> dict[str, Any]:
    return {
        "m3": m3.client(),
        "storage": get_storage_service(),
        "broker": get_event_broker(),
        "job_try": job_try,
        **extra,
    }


def multipart_fields(request: httpx.Request) -> dict[str, tuple[str | None, bytes, str]]:
    raw = f"Content-Type: {request.headers['content-type']}\r\n\r\n".encode() + request.content
    message = BytesParser(policy=email_policy).parsebytes(raw)
    return {
        part.get_param("name", header="content-disposition"): (
            part.get_filename(),
            part.get_payload(decode=True),
            part.get_content_type(),
        )
        for part in message.iter_parts()
    }


# =============================================================================================
# 1–2. HMAC-signed M3 webhook updates candidate profile and application score
# =============================================================================================
class TestExtractorWebhook:
    async def test_signed_webhook_updates_profile_score_and_breakdown(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, alpha: Workspace, job: dict
    ) -> None:
        app = await apply(client, admin_engine, alpha, job["id"], email="alex@example.com")
        broker = get_event_broker()
        payload = m3_payload(app, score=94.0)

        async with broker.subscribe(pipeline_channel(alpha.tenant_id, uuid.UUID(job["id"]))) as sub:
            response = await post_webhook(client, payload)
            event = await sub.get(timeout=1)

        assert response.status_code == 200, response.text
        assert response.json() == {
            "status": "processed",
            "application_id": app["application_id"],
            "deterministic_match_score": 94.0,
        }
        row = await application_row(admin_engine, app["application_id"])
        assert row.status == "applied" and str(row.score) == "94.00"
        assert row.tracking == payload["tracking_id"]
        assert row.breakdown["breakdown"]["matched_required"][0]["skill"] == "Go"
        assert row.breakdown["experience_match"]["meets_criteria"] is True
        assert row.extracted["work_experience"][0]["company"] == "Stripe"
        # Candidate profile: parsed JSON stored; empty contact fields filled; email untouched.
        assert row.profile["skills"] == ["Go", "Kubernetes", "PostgreSQL", "Kafka", "gRPC"]
        assert row.profile["education"][0]["institution"] == "UC Berkeley"
        assert (row.first_name, row.last_name) == ("Alex", "Mercer")
        assert row.phone == "+1-555-019-2834" and row.location == "San Francisco, CA"
        assert row.github_url == "https://github.com/alexmercer-dev"
        assert row.email == "alex@example.com"
        # Recruiters connected to the board are notified with the new score.
        assert event is not None and event["type"] == "card_updated"
        assert event["card"]["deterministic_match_score"] == 94.0
        assert event["card"]["match"] == {
            "matched_required": ["Go", "Kubernetes"],
            "matched_preferred": ["Kafka"],
            "missing_required": [],
        }

    async def test_signature_is_required_and_verified(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, alpha: Workspace, job: dict
    ) -> None:
        app = await apply(client, admin_engine, alpha, job["id"])
        payload = m3_payload(app)
        body = json.dumps(payload).encode()
        tampered = json.dumps({**payload, "deterministic_match": {"overall_score": 100}}).encode()
        cases = {
            "missing": ({}, body),
            "wrong_secret": ({"X-Webhook-Signature": sign_payload(body, "attacker")}, body),
            "tampered_body": (
                {"X-Webhook-Signature": sign_payload(body, WEBHOOK_SECRET)},
                tampered,
            ),
            "garbage": ({"X-Webhook-Signature": "sha256=deadbeef"}, body),
        }
        for name, (headers, content) in cases.items():
            response = await client.post(
                f"{API}/webhooks/extractor",
                content=content,
                headers={"Content-Type": "application/json", **headers},
            )
            assert response.status_code == 401, name
        assert (await application_row(admin_engine, app["application_id"])).score is None

        bare_hex = sign_payload(body, WEBHOOK_SECRET).removeprefix("sha256=")
        accepted = await client.post(
            f"{API}/webhooks/extractor",
            content=body,
            headers={"Content-Type": "application/json", "X-Webhook-Signature": bare_hex},
        )
        assert accepted.status_code == 200

    async def test_webhook_fails_closed_without_a_secret(
        self, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            get_settings(), "M3_WEBHOOK_SECRET", type(get_settings().M3_WEBHOOK_SECRET)("")
        )
        response = await client.post(
            f"{API}/webhooks/extractor",
            content=b"{}",
            headers={"X-Webhook-Signature": sign_payload(b"{}", "")},
        )
        assert response.status_code == 503

    async def test_context_and_payload_are_validated(
        self,
        client: httpx.AsyncClient,
        admin_engine: AsyncEngine,
        alpha: Workspace,
        beta: Workspace,
        job: dict,
    ) -> None:
        app = await apply(client, admin_engine, alpha, job["id"])
        cross_tenant = m3_payload({**app, "tenant_id": str(beta.tenant_id)})
        wrong_candidate = m3_payload({**app, "candidate_id": str(uuid.uuid4())})
        unknown_app = m3_payload({**app, "application_id": str(uuid.uuid4())})
        out_of_range = m3_payload(app, score=120.0)
        for payload, expected in (
            (cross_tenant, 404),  # RLS: the application is invisible under another tenant
            (wrong_candidate, 404),
            (unknown_app, 404),
            (out_of_range, 422),
            ({"tracking_id": "trk"}, 422),
        ):
            assert (await post_webhook(client, payload)).status_code == expected

        async with admin_engine.begin() as conn:  # the worker recorded M3's tracking id
            await conn.execute(
                text(
                    "UPDATE job_applications SET extractor_tracking_id = 'trk_real' WHERE id = :id"
                ),
                {"id": app["application_id"]},
            )
        mismatched = await post_webhook(client, m3_payload(app, tracking_id="trk_forged"))
        assert mismatched.status_code == 409
        matched = await post_webhook(client, m3_payload(app, tracking_id="trk_real"))
        assert matched.status_code == 200

    async def test_redelivered_webhook_is_idempotent(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, alpha: Workspace, job: dict
    ) -> None:
        app = await apply(client, admin_engine, alpha, job["id"])
        payload = m3_payload(app, score=81.5)
        assert (await post_webhook(client, payload)).json()["status"] == "processed"
        again = await post_webhook(
            client, {**payload, "deterministic_match": {"overall_score": 12}}
        )
        assert again.status_code == 200 and again.json()["status"] == "duplicate"
        assert str((await application_row(admin_engine, app["application_id"])).score) == "81.50"

    async def test_failed_extraction_is_recorded_and_candidate_notified(
        self,
        client: httpx.AsyncClient,
        admin_engine: AsyncEngine,
        alpha: Workspace,
        job: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(get_settings(), "APPLICATION_EVENTS_MAX_SECONDS", 0.3)
        app = await apply(client, admin_engine, alpha, job["id"])
        payload = m3_payload(app, status="failed", error="Scanned image unreadable")
        payload.pop("deterministic_match")
        response = await post_webhook(client, payload)
        assert response.json()["status"] == "failed"

        row = await application_row(admin_engine, app["application_id"])
        assert row.extraction_error == "Scanned image unreadable"
        assert row.status == "applied" and row.score is None
        events = parse_sse((await client.get(app["events_url"])).text)
        assert events[-1][1]["status"] == "failed"
        card = column(await board(client, alpha, job["id"]), "Applied")["cards"]
        assert (
            next(c for c in card if c["application_id"] == app["application_id"])[
                "extraction_status"
            ]
            == "failed"
        )

    async def test_existing_candidate_profile_is_not_overwritten(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, alpha: Workspace, job: dict
    ) -> None:
        email = f"returning.{uuid.uuid4().hex[:6]}@example.com"
        first = await apply(client, admin_engine, alpha, job["id"], email=email)
        await post_webhook(client, m3_payload(first, phone="+1-555-0001", full_name="Real Person"))

        other_job = await create_job(client, alpha, taxonomy=GO_TAXONOMY)
        second = await apply(client, admin_engine, alpha, other_job["id"], email=email)
        await post_webhook(
            client, m3_payload(second, phone="+1-555-9999", full_name="Someone Else")
        )

        row = await application_row(admin_engine, second["application_id"])
        assert (row.first_name, row.last_name, row.phone) == ("Real", "Person", "+1-555-0001")
        assert row.extracted["contact"]["phone"] == "+1-555-9999"  # kept on the application


# =============================================================================================
# Extraction worker: ARQ task -> M3 REST contract, rate limit, backoff, sweeper
# =============================================================================================
class TestExtractionWorker:
    async def test_submission_is_queued_for_extraction(
        self,
        client: httpx.AsyncClient,
        admin_engine: AsyncEngine,
        alpha: Workspace,
        job: dict,
        extraction_queue: Any,
    ) -> None:
        app = await apply(client, admin_engine, alpha, job["id"])
        assert (alpha.tenant_id, uuid.UUID(app["application_id"])) in extraction_queue.jobs

        extraction_queue.fail_with = ConnectionError("redis down")
        try:  # a Redis outage never loses the application; the sweeper picks it up later
            outage = await apply(client, admin_engine, alpha, job["id"])
        finally:
            extraction_queue.fail_with = None
        assert (await application_row(admin_engine, outage["application_id"])).status == (
            "pending_extraction"
        )

    async def test_worker_dispatches_per_contract_and_records_tracking_id(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, alpha: Workspace, job: dict
    ) -> None:
        app = await apply(client, admin_engine, alpha, job["id"])
        m3 = M3Mock()

        result = await extraction_worker.extract_resume(
            worker_ctx(m3), app["tenant_id"], app["application_id"]
        )

        assert result == "dispatched"
        assert (
            await application_row(admin_engine, app["application_id"])
        ).tracking == "trk_m3_88301fa0"
        request = m3.requests[0]
        assert str(request.url) == "http://m3.test:8000/api/v1/resumes/extract"
        assert request.headers["x-api-key"] == "sec_test_m3_key"
        fields = multipart_fields(request)
        file_name, file_bytes, mime = fields["file"]
        assert file_name == "resume.pdf" and mime == "application/pdf"
        assert file_bytes == (get_settings().local_storage_dir / app["s3_key"]).read_bytes()
        async with AsyncSessionLocal() as session:
            await set_tenant_context(session, alpha.tenant_id)
            expected_jd = build_m3_job_description(await session.get(Job, uuid.UUID(job["id"])))
        assert json.loads(fields["job_description"][1]) == expected_jd
        assert expected_jd["required_skills"] == ["Go", "Kubernetes"]
        assert fields["webhook_url"][1].decode() == "http://test/api/v1/webhooks/extractor"
        assert json.loads(fields["callback_context"][1]) == {
            "tenant_id": app["tenant_id"],
            "application_id": app["application_id"],
            "candidate_id": app["candidate_id"],
        }

        again = await extraction_worker.extract_resume(
            worker_ctx(m3), app["tenant_id"], app["application_id"]
        )
        assert again == "already_dispatched" and len(m3.requests) == 1

    async def test_worker_backs_off_5_15_45_on_busy_or_timeouts(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, alpha: Workspace, job: dict
    ) -> None:
        app = await apply(client, admin_engine, alpha, job["id"])
        busy = M3Mock(httpx.Response(503), httpx.ReadTimeout("slow"), httpx.Response(503))
        ctx = worker_ctx(busy)
        for attempt, delay in enumerate((5, 15, 45), start=1):
            ctx["job_try"] = attempt
            with pytest.raises(Retry) as retry:
                await extraction_worker.extract_resume(ctx, app["tenant_id"], app["application_id"])
            assert retry.value.defer_score == delay * 1000
        ctx["job_try"] = 4
        assert (
            await extraction_worker.extract_resume(ctx, app["tenant_id"], app["application_id"])
            == "deferred_to_sweeper"
        )
        row = await application_row(admin_engine, app["application_id"])
        assert row.status == "pending_extraction" and row.extraction_error is None
        assert extraction_worker.WorkerSettings.max_jobs == 1  # matches M3's Semaphore(1)

    async def test_worker_records_permanent_rejections(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, alpha: Workspace, job: dict
    ) -> None:
        rejected = await apply(client, admin_engine, alpha, job["id"])
        m3 = M3Mock(httpx.Response(400, json={"detail": "unsupported file"}))
        result = await extraction_worker.extract_resume(
            worker_ctx(m3), rejected["tenant_id"], rejected["application_id"]
        )
        row = await application_row(admin_engine, rejected["application_id"])
        assert result == "failed" and "HTTP 400" in row.extraction_error and row.status == "applied"

        missing = await apply(client, admin_engine, alpha, job["id"])
        (get_settings().local_storage_dir / missing["s3_key"]).unlink()
        result = await extraction_worker.extract_resume(
            worker_ctx(M3Mock()), missing["tenant_id"], missing["application_id"]
        )
        assert result == "failed"

    async def test_sweeper_requeues_stalled_applications_when_m3_is_healthy(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, alpha: Workspace, job: dict
    ) -> None:
        stalled = await apply(client, admin_engine, alpha, job["id"])
        async with admin_engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE job_applications SET applied_at = now() - interval '1 hour' "
                    "WHERE id = :id"
                ),
                {"id": stalled["application_id"]},
            )

        class FakeArqRedis:
            def __init__(self) -> None:
                self.jobs: list[tuple] = []

            async def enqueue_job(self, name: str, *args: str, **kwargs: Any) -> None:
                self.jobs.append((name, *args))

        down = FakeArqRedis()
        assert (
            await extraction_worker.sweep_stalled_extractions(
                worker_ctx(M3Mock(healthy=False), redis=down)
            )
            == 0
        )
        up = FakeArqRedis()
        count = await extraction_worker.sweep_stalled_extractions(
            worker_ctx(M3Mock(healthy=True), redis=up)
        )
        assert count >= 1
        assert ("extract_resume", stalled["tenant_id"], stalled["application_id"]) in up.jobs

    # arq's Worker.close() calls redis' deprecated close(); not something this project controls.
    @pytest.mark.filterwarnings("ignore:Call to deprecated close:DeprecationWarning")
    async def test_real_arq_worker_round_trip_over_redis(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, alpha: Workspace, job: dict
    ) -> None:
        from arq.worker import Worker

        queue = ArqExtractionQueue(get_settings().REDIS_URL)
        try:
            pool = await queue._redis()
            await pool.ping()
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"Redis not reachable at {get_settings().REDIS_URL}: {exc}")

        app = await apply(client, admin_engine, alpha, job["id"])
        await queue.enqueue_extraction(alpha.tenant_id, uuid.UUID(app["application_id"]))
        m3 = M3Mock()

        async def startup(ctx: dict[str, Any]) -> None:
            ctx.update(worker_ctx(m3))

        worker = Worker(
            functions=extraction_worker.WorkerSettings.functions,
            queue_name=extraction_worker.WorkerSettings.queue_name,
            redis_pool=pool,
            on_startup=startup,
            burst=True,
            max_jobs=extraction_worker.WorkerSettings.max_jobs,
            poll_delay=0.05,
            handle_signals=False,
        )
        try:
            await worker.main()
        finally:
            await worker.close()
            await pool.delete(f"arq:result:extract:{app['application_id']}")
            await queue.aclose()
        assert worker.jobs_complete == 1
        assert (
            await application_row(admin_engine, app["application_id"])
        ).tracking == "trk_m3_88301fa0"


# =============================================================================================
# 3–4. Score-ranked Kanban, stage moves, audit log, RBAC and live SSE
# =============================================================================================
class TestKanbanPipeline:
    async def test_applied_column_is_ranked_by_deterministic_score(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, alpha: Workspace
    ) -> None:
        job = await create_job(client, alpha, title="Platform Engineer", taxonomy=GO_TAXONOMY)
        scores = [72.0, 45.0, 95.0, 60.0, 88.0]  # deliberately submitted out of order
        for score in scores:
            app = await apply(client, admin_engine, alpha, job["id"])
            assert (await post_webhook(client, m3_payload(app, score=score))).status_code == 200
        pending = await apply(client, admin_engine, alpha, job["id"])  # still extracting

        pipeline = await board(client, alpha, job["id"])

        assert [stage["name"] for stage in pipeline["stages"]] == [
            "Applied", "Pre-Screen", "Tech Screen", "Interview", "Offer", "Hired", "Rejected"
        ]  # fmt: skip
        applied = column(pipeline, "Applied")["cards"]
        assert [card["deterministic_match_score"] for card in applied] == [
            95.0, 88.0, 72.0, 60.0, 45.0, None
        ]  # fmt: skip
        assert applied[-1]["application_id"] == pending["application_id"]
        assert applied[-1]["extraction_status"] == "pending"
        assert applied[0]["match"]["matched_required"] == ["Go", "Kubernetes"]
        assert applied[0]["candidate"]["full_name"] == "Alex Mercer"
        assert pipeline["job"]["title"] == "Platform Engineer"

    async def test_move_applied_to_tech_screen_with_audit_log(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, alpha: Workspace, job: dict
    ) -> None:
        app = await apply(client, admin_engine, alpha, job["id"])
        await post_webhook(client, m3_payload(app, score=91.0))
        pipeline = await board(client, alpha, job["id"])
        tech_screen = column(pipeline, "Tech Screen")

        response = await client.patch(
            f"{API}/applications/{app['application_id']}/move-stage",
            json={"to_stage_id": tech_screen["id"], "expected_version": 1},
            headers={**alpha.headers(RECRUITER), "User-Agent": "pytest-hr-tester"},
        )

        assert response.status_code == 200, response.text
        card = response.json()
        assert card["stage_id"] == tech_screen["id"] and card["version"] == 2
        refreshed = await board(client, alpha, job["id"])
        assert app["application_id"] in {
            c["application_id"] for c in column(refreshed, "Tech Screen")["cards"]
        }
        assert app["application_id"] not in {
            c["application_id"] for c in column(refreshed, "Applied")["cards"]
        }

        async with admin_engine.connect() as conn:
            audit = (
                await conn.execute(
                    text(
                        "SELECT tenant_id, actor_id, action, entity_type, metadata_json, "
                        "user_agent "
                        "FROM audit_logs WHERE entity_id = :id"
                    ),
                    {"id": app["application_id"]},
                )
            ).one()
        assert audit.tenant_id == alpha.tenant_id
        assert audit.actor_id == alpha.members[RECRUITER].user_id
        assert (audit.action, audit.entity_type) == ("stage_changed", "job_application")
        assert audit.metadata_json["from_stage"] == "Applied"
        assert audit.metadata_json["to_stage"] == "Tech Screen"
        assert audit.metadata_json["version"] == 2
        assert audit.user_agent == "pytest-hr-tester"

        stale = await client.patch(
            f"{API}/applications/{app['application_id']}/move-stage",
            json={"to_stage_id": column(pipeline, "Interview")["id"], "expected_version": 1},
            headers=alpha.headers(RECRUITER),
        )
        assert stale.status_code == 409  # optimistic concurrency

    async def test_concurrent_moves_cannot_both_win(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, alpha: Workspace, job: dict
    ) -> None:
        app = await apply(client, admin_engine, alpha, job["id"])
        pipeline = await board(client, alpha, job["id"])
        targets = [column(pipeline, "Tech Screen")["id"], column(pipeline, "Interview")["id"]]
        responses = await asyncio.gather(
            *(
                client.patch(
                    f"{API}/applications/{app['application_id']}/move-stage",
                    json={"to_stage_id": target, "expected_version": 1},
                    headers=alpha.headers(RECRUITER),
                )
                for target in targets
            )
        )
        assert sorted(r.status_code for r in responses) == [200, 409]
        assert (await application_row(admin_engine, app["application_id"])).version == 2

    async def test_stage_actions_and_status_follow_the_card(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, alpha: Workspace, job: dict
    ) -> None:
        app = await apply(client, admin_engine, alpha, job["id"])
        await post_webhook(client, m3_payload(app))
        pipeline = await board(client, alpha, job["id"])

        version = 1
        for stage_name, expected_status in (
            ("Pre-Screen", "applied"),
            ("Rejected", "rejected"),
            ("Applied", "applied"),
            ("Offer", "offered"),
        ):
            response = await client.patch(
                f"{API}/applications/{app['application_id']}/move-stage",
                json={
                    "to_stage_id": column(pipeline, stage_name)["id"],
                    "expected_version": version,
                },
                headers=alpha.headers(ADMIN),
            )
            assert response.status_code == 200, (stage_name, response.text)
            assert response.json()["status"] == expected_status, stage_name
            version = response.json()["version"]

        async with admin_engine.connect() as conn:
            actions = (
                await conn.execute(
                    text(
                        "SELECT metadata_json->>'to_stage' AS stage, "
                        "metadata_json->'stage_actions' AS a "
                        "FROM audit_logs WHERE entity_id = :id ORDER BY created_at"
                    ),
                    {"id": app["application_id"]},
                )
            ).all()
        by_stage = {row.stage: row.a for row in actions}
        # Phase 6 implements these actions: AI pre-screen queued, rejection email scheduled
        # behind the polite delay, offer email queued.
        assert by_stage["Pre-Screen"] == [{"action": "ai_pre_screen", "status": "queued"}]
        assert by_stage["Rejected"] == [{"action": "polite_rejection_email", "status": "scheduled"}]
        assert by_stage["Offer"] == [{"action": "offer_email", "status": "queued"}]

    async def test_late_webhook_does_not_undo_a_recruiter_decision(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, alpha: Workspace, job: dict
    ) -> None:
        app = await apply(client, admin_engine, alpha, job["id"])
        pipeline = await board(client, alpha, job["id"])
        await client.patch(
            f"{API}/applications/{app['application_id']}/move-stage",
            json={"to_stage_id": column(pipeline, "Rejected")["id"], "expected_version": 1},
            headers=alpha.headers(RECRUITER),
        )
        await post_webhook(client, m3_payload(app, score=55.0))
        row = await application_row(admin_engine, app["application_id"])
        assert row.status == "rejected" and str(row.score) == "55.00"

    async def test_validation_rbac_and_tenant_isolation(
        self,
        client: httpx.AsyncClient,
        admin_engine: AsyncEngine,
        alpha: Workspace,
        beta: Workspace,
        job: dict,
    ) -> None:
        app = await apply(client, admin_engine, alpha, job["id"])
        other_job = await create_job(client, alpha, taxonomy=GO_TAXONOMY)
        pipeline = await board(client, alpha, job["id"])
        foreign_stage = column(await board(client, alpha, other_job["id"]), "Interview")["id"]
        move_url = f"{API}/applications/{app['application_id']}/move-stage"

        def move(stage_id: str, version: int = 1) -> dict[str, Any]:
            return {"to_stage_id": stage_id, "expected_version": version}

        assert (
            await client.patch(move_url, json=move(foreign_stage), headers=alpha.headers())
        ).status_code == 422
        hm_tech = await client.patch(
            move_url,
            json=move(column(pipeline, "Tech Screen")["id"]),
            headers=alpha.headers(HIRING_MANAGER),
        )
        hm_interview = await client.patch(
            move_url,
            json=move(column(pipeline, "Interview")["id"]),
            headers=alpha.headers(HIRING_MANAGER),
        )
        assert (hm_tech.status_code, hm_interview.status_code) == (403, 200)

        interviewer = alpha.headers(INTERVIEWER)
        assert (
            await client.get(f"{API}/jobs/{job['id']}/pipeline", headers=interviewer)
        ).status_code == 403
        assert (
            await client.patch(
                move_url, json=move(column(pipeline, "Offer")["id"], 2), headers=interviewer
            )
        ).status_code == 403
        assert (
            await client.get(f"{API}/jobs/{job['id']}/pipeline/stream", headers=interviewer)
        ).status_code == 403

        assert (
            await client.get(f"{API}/jobs/{job['id']}/pipeline", headers=beta.headers())
        ).status_code == 404
        assert (
            await client.patch(
                move_url, json=move(column(pipeline, "Offer")["id"], 2), headers=beta.headers()
            )
        ).status_code == 404

    async def test_pipeline_stream_pushes_extraction_and_move_events(
        self,
        client: httpx.AsyncClient,
        admin_engine: AsyncEngine,
        alpha: Workspace,
        job: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(get_settings(), "PIPELINE_STREAM_MAX_SECONDS", 1.0)
        monkeypatch.setattr(get_settings(), "PIPELINE_STREAM_KEEPALIVE_SECONDS", 0.2)
        app = await apply(client, admin_engine, alpha, job["id"])
        tech_screen = column(await board(client, alpha, job["id"]), "Tech Screen")["id"]

        async def recruiter_activity() -> None:
            await asyncio.sleep(0.2)
            await post_webhook(client, m3_payload(app, score=77.0))
            await client.patch(
                f"{API}/applications/{app['application_id']}/move-stage",
                json={"to_stage_id": tech_screen, "expected_version": 1},
                headers=alpha.headers(RECRUITER),
            )

        stream, _ = await asyncio.gather(
            client.get(f"{API}/jobs/{job['id']}/pipeline/stream", headers=alpha.headers(RECRUITER)),
            recruiter_activity(),
        )

        assert stream.status_code == 200
        assert stream.headers["content-type"].startswith("text/event-stream")
        events = [
            (name, data)
            for name, data in parse_sse(stream.text)
            if data["card"]["application_id"] == app["application_id"]
        ]
        assert [name for name, _ in events] == ["card_updated", "card_moved"]
        assert events[0][1]["card"]["deterministic_match_score"] == 77.0
        assert events[1][1]["card"]["stage_id"] == tech_screen

    async def test_stream_requires_bearer_auth_and_own_job(
        self, client: httpx.AsyncClient, alpha: Workspace, beta: Workspace, job: dict
    ) -> None:
        url = f"{API}/jobs/{job['id']}/pipeline/stream"
        assert (await client.get(url)).status_code == 401
        assert (await client.get(url, headers=beta.headers())).status_code == 404


# =============================================================================================
# Candidate journey: SSE "parsed" -> 5-second review -> confirm
# =============================================================================================
class TestCandidateConfirmation:
    async def test_candidate_reviews_and_confirms_extracted_profile(
        self,
        client: httpx.AsyncClient,
        admin_engine: AsyncEngine,
        alpha: Workspace,
        job: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(get_settings(), "APPLICATION_EVENTS_MAX_SECONDS", 0.2)
        app = await apply(client, admin_engine, alpha, job["id"], email="alex.m@example.com")
        confirm_url = (
            f"{API}/public/applications/{app['application_id']}/confirm?token={app['status_token']}"
        )
        profile = {
            "full_name": "Alexander Mercer",
            "email": "alex.m@example.com",
            "phone": "+1-555-000-1111",
            "linkedin_url": "https://linkedin.com/in/alexmercer",
            "github_url": "https://github.com/alexmercer-dev",
            "location": "Oakland, CA",
            "skills": ["Go", "Kubernetes", "Rust"],
        }
        early = await client.post(confirm_url, json=profile)
        assert early.status_code == 409  # still being processed

        await post_webhook(client, m3_payload(app))
        parsed = parse_sse((await client.get(app["events_url"])).text)[-1][1]
        assert parsed["status"] == "parsed" and parsed["confirmed"] is False
        assert parsed["candidate"]["full_name"] == "Alex Mercer"
        assert parsed["candidate"]["email"] == "alex.m@example.com"
        assert parsed["candidate"]["skills"] == ["Go", "Kubernetes", "PostgreSQL", "Kafka", "gRPC"]

        forged = confirm_url.replace("token=", "token=0")
        assert (await client.post(forged, json=profile)).status_code == 403

        confirmed = await client.post(confirm_url, json=profile)
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["confirmed"] is True
        row = await application_row(admin_engine, app["application_id"])
        assert row.confirmed_at is not None and row.confirmed["skills"] == [
            "Go",
            "Kubernetes",
            "Rust",
        ]
        assert (row.first_name, row.last_name, row.location) == (
            "Alexander",
            "Mercer",
            "Oakland, CA",
        )
        assert row.profile["verified_skills"] == ["Go", "Kubernetes", "Rust"]
        assert (await client.post(confirm_url, json=profile)).status_code == 409
        assert parse_sse((await client.get(app["events_url"])).text)[-1][1]["confirmed"] is True

    async def test_confirmation_never_rewrites_an_existing_candidate(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, alpha: Workspace, job: dict
    ) -> None:
        email = f"known.{uuid.uuid4().hex[:6]}@example.com"
        first = await apply(
            client, admin_engine, alpha, job["id"], email=email, full_name="Known Person"
        )
        other_job = await create_job(client, alpha, taxonomy=GO_TAXONOMY)
        second = await apply(client, admin_engine, alpha, other_job["id"], email=email)
        await post_webhook(client, m3_payload(second))

        response = await client.post(
            f"{API}/public/applications/{second['application_id']}/confirm?token={second['status_token']}",
            json={"full_name": "Impostor", "email": "impostor@example.com", "skills": []},
        )
        assert response.status_code == 200
        row = await application_row(admin_engine, first["application_id"])
        assert (row.first_name, row.last_name, row.email) == ("Known", "Person", email)
        second_row = await application_row(admin_engine, second["application_id"])
        assert second_row.confirmed["full_name"] == "Impostor"  # kept on that application only


def test_in_memory_broker_is_used_for_tests() -> None:
    assert isinstance(get_event_broker(), InMemoryEventBroker)
