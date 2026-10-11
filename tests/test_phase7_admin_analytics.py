"""Phase 7 — L5 HR Tester: pipeline analytics, GDPR erasure, stage editor, team, interviews,
Cal.com bookings and offer letters.

Strict testing mandate: headless Python script tests only (pytest + httpx.AsyncClient).
No web/browser testing.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import get_settings
from app.core.database import AsyncSessionLocal, set_tenant_context
from app.core.pdf import render_text_pdf
from app.models import UserRole
from app.services.embedding_service import EmbeddingService
from tests.helpers import (
    API,
    GO_TAXONOMY,
    HashingEmbedder,
    Workspace,
    add_member,
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


@pytest.fixture(scope="module")
async def workspaces(api_app: object) -> AsyncIterator[tuple[Workspace, Workspace]]:
    transport = httpx.ASGITransport(app=api_app)  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield await provision_workspace(http, "Granite"), await provision_workspace(http, "Harbor")


@pytest.fixture
def alpha(workspaces: tuple) -> Workspace:
    return workspaces[0]


@pytest.fixture
def beta(workspaces: tuple) -> Workspace:
    return workspaces[1]


async def move(
    client: httpx.AsyncClient, ws: Workspace, job_id: str, app_id: str, stage: str
) -> dict:
    pipeline = await board(client, ws, job_id)
    card = next(c for s in pipeline["stages"] for c in s["cards"] if c["application_id"] == app_id)
    response = await client.patch(
        f"{API}/applications/{app_id}/move-stage",
        json={"to_stage_id": column(pipeline, stage)["id"], "expected_version": card["version"]},
        headers=ws.headers(ADMIN),
    )
    assert response.status_code == 200, response.text
    return response.json()


# =============================================================================================
# Pipeline conversion metrics & time-to-hire
# =============================================================================================
class TestPipelineAnalytics:
    async def test_funnel_drop_off_and_time_to_hire(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, alpha: Workspace
    ) -> None:
        job = await create_job(client, alpha, title="Analytics Engineer", taxonomy=GO_TAXONOMY)
        apps = [await apply(client, admin_engine, alpha, job["id"]) for _ in range(5)]
        for app in apps:
            await post_webhook(client, m3_payload(app, score=80.0))
        async with admin_engine.begin() as conn:  # the hire applied 10 days ago
            await conn.execute(
                text(
                    "UPDATE job_applications SET applied_at = now() - interval '10 days' WHERE id = :id"
                ),
                {"id": apps[0]["application_id"]},
            )
        a, b, c, d, _e = (x["application_id"] for x in apps)
        for stage in ("Pre-Screen", "Interview", "Offer", "Hired"):
            await move(client, alpha, job["id"], a, stage)  # hired
        await move(client, alpha, job["id"], b, "Pre-Screen")
        await move(client, alpha, job["id"], b, "Interview")  # still interviewing
        await move(client, alpha, job["id"], c, "Pre-Screen")
        await move(client, alpha, job["id"], c, "Rejected")  # dropped after pre-screen
        await move(client, alpha, job["id"], d, "Interview")  # skipped Pre-Screen

        response = await client.get(
            f"{API}/analytics/pipeline",
            params={"job_id": job["id"]},
            headers=alpha.headers(HIRING_MANAGER),
        )

        assert response.status_code == 200, response.text
        data = response.json()
        assert data["totals"] == {
            "applications": 5, "in_progress": 3, "hired": 1, "rejected": 1, "withdrawn": 0, "hire_rate": 0.2
        }  # fmt: skip
        funnel = {f["name"]: f for f in data["funnel"]}
        assert [f["name"] for f in data["funnel"]] == [
            "Applied",
            "Pre-Screen",
            "Tech Screen",
            "Interview",
            "Offer",
            "Hired",
        ]
        assert {n: funnel[n]["reached"] for n in funnel} == {
            "Applied": 5, "Pre-Screen": 4, "Tech Screen": 3, "Interview": 3, "Offer": 1, "Hired": 1
        }  # fmt: skip
        assert funnel["Pre-Screen"]["conversion_from_previous"] == 0.8
        assert funnel["Interview"]["drop_off_rate"] == pytest.approx(0.6667, abs=1e-3)
        assert funnel["Pre-Screen"]["rejected_here"] == 1
        assert funnel["Applied"]["current"] == 1 and funnel["Interview"]["current"] == 2
        assert funnel["Applied"]["avg_days_in_stage"] is not None
        tth = data["time_to_hire"]
        assert (
            tth["hired"] == 1
            and 9.9 < tth["average_days"] < 10.1
            and tth["median_days"] == tth["average_days"]
        )
        [job_metrics] = data["jobs"]
        assert job_metrics["applications"] == 5 and job_metrics["average_match_score"] == 80.0
        assert sum(w["applications"] for w in data["weekly_applications"]) == 5

    async def test_filters_rbac_and_isolation(
        self,
        client: httpx.AsyncClient,
        admin_engine: AsyncEngine,
        alpha: Workspace,
        beta: Workspace,
    ) -> None:
        job = await create_job(client, alpha, taxonomy=GO_TAXONOMY)
        await apply(client, admin_engine, alpha, job["id"])
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        future = await client.get(
            f"{API}/analytics/pipeline",
            params={"job_id": job["id"], "from": tomorrow},
            headers=alpha.headers(),
        )
        assert future.json()["totals"]["applications"] == 0
        assert (
            await client.get(
                f"{API}/analytics/pipeline",
                params={"from": tomorrow, "to": date.today().isoformat()},
                headers=alpha.headers(),
            )
        ).status_code == 422
        assert (
            await client.get(f"{API}/analytics/pipeline", headers=alpha.headers(INTERVIEWER))
        ).status_code == 403
        other = await client.get(
            f"{API}/analytics/pipeline", params={"job_id": job["id"]}, headers=beta.headers()
        )
        assert other.json()["totals"]["applications"] == 0 and other.json()["jobs"] == []


# =============================================================================================
# GDPR Article 17 — right to be forgotten
# =============================================================================================
class TestGdprErasure:
    async def test_forget_removes_every_trace_and_keeps_anonymised_audit(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, alpha: Workspace
    ) -> None:
        email = f"forget.me.{uuid.uuid4().hex[:6]}@example.com"
        job = await create_job(client, alpha, taxonomy=GO_TAXONOMY)
        app = await apply(
            client, admin_engine, alpha, job["id"], email=email, full_name="Forget Me"
        )
        await post_webhook(client, m3_payload(app, full_name="Forget Me"))
        await client.post(
            f"{API}/applications/{app['application_id']}/notes",
            json={"content": f"Strong. @[Admin]({alpha.members[ADMIN].user_id})"},
            headers=alpha.headers(RECRUITER),
        )
        await client.post(
            f"{API}/applications/{app['application_id']}/offer-letter",
            json={"base_salary": "150000", "currency": "USD", "start_date": "2026-12-01"},
            headers=alpha.headers(RECRUITER),
        )
        async with AsyncSessionLocal() as session:
            await set_tenant_context(session, alpha.tenant_id)
            await EmbeddingService(session, HashingEmbedder()).index_candidate(
                uuid.UUID(app["candidate_id"])
            )
        storage = get_settings().local_storage_dir
        files = [
            p
            for p in storage.rglob("*")
            if p.is_file()
            and (app["application_id"] in str(p) or p.name == app["s3_key"].rsplit("/", 1)[-1])
        ]
        assert len(files) >= 2  # resume + offer letter

        lookup = await client.get(
            f"{API}/compliance/candidates",
            params={"email": email.upper()},
            headers=alpha.headers(ADMIN),
        )
        assert [c["candidate_id"] for c in lookup.json()] == [app["candidate_id"]]
        assert (
            await client.delete(
                f"{API}/compliance/candidates/{app['candidate_id']}/forget",
                headers=alpha.headers(RECRUITER),
            )
        ).status_code == 403

        erased = await client.delete(
            f"{API}/compliance/candidates/{app['candidate_id']}/forget",
            headers=alpha.headers(ADMIN),
        )

        assert erased.status_code == 200, erased.text
        report = erased.json()
        assert report["status"] == "erased" and report["applications_deleted"] == 1
        assert report["files_deleted"] >= 2 and report["file_errors"] == 0
        assert not any(p.exists() for p in files)
        async with admin_engine.connect() as conn:
            for table, column_name, value in (
                ("candidates", "id", app["candidate_id"]),
                ("job_applications", "candidate_id", app["candidate_id"]),
                ("candidate_embeddings", "candidate_id", app["candidate_id"]),
                ("candidate_notes", "application_id", app["application_id"]),
                ("email_outbox", "application_id", app["application_id"]),
                ("notifications", "entity_id", app["application_id"]),
            ):
                count = await conn.scalar(
                    text(f"SELECT count(*) FROM {table} WHERE {column_name} = :v"), {"v": value}
                )
                assert count == 0, table
            audit = (
                await conn.execute(
                    text(
                        "SELECT actor_id, metadata_json::text AS meta FROM audit_logs WHERE action = 'candidate_erased' AND entity_id = :id"
                    ),
                    {"id": app["candidate_id"]},
                )
            ).one()
        assert audit.actor_id == alpha.members[ADMIN].user_id
        assert (
            "GDPR Art. 17" in audit.meta
            and email not in audit.meta
            and "Forget Me" not in audit.meta
        )
        again = await client.delete(
            f"{API}/compliance/candidates/{app['candidate_id']}/forget",
            headers=alpha.headers(ADMIN),
        )
        assert again.status_code == 404

    async def test_erasure_is_tenant_scoped(
        self,
        client: httpx.AsyncClient,
        admin_engine: AsyncEngine,
        alpha: Workspace,
        beta: Workspace,
    ) -> None:
        job = await create_job(client, alpha, taxonomy=GO_TAXONOMY)
        app = await apply(client, admin_engine, alpha, job["id"])
        response = await client.delete(
            f"{API}/compliance/candidates/{app['candidate_id']}/forget", headers=beta.headers(ADMIN)
        )
        assert response.status_code == 404


# =============================================================================================
# Stage pipeline editor
# =============================================================================================
class TestStageEditor:
    async def test_add_rename_reorder_and_delete_stages(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, alpha: Workspace
    ) -> None:
        job = await create_job(client, alpha, taxonomy=GO_TAXONOMY)
        url = f"{API}/jobs/{job['id']}/stages"
        created = await client.post(
            url,
            json={
                "name": "Take-Home",
                "stage_type": "screen",
                "automated_actions": ["ai_pre_screen"],
            },
            headers=alpha.headers(RECRUITER),
        )
        assert created.status_code == 201, created.text
        names = [s["name"] for s in created.json()]
        assert names[-3:] == ["Take-Home", "Hired", "Rejected"]  # inserted before terminal columns
        take_home = next(s for s in created.json() if s["name"] == "Take-Home")
        assert take_home["automated_actions"] == ["ai_pre_screen"]

        for payload, expected in (
            ({"name": "Bad", "stage_type": "screen", "automated_actions": ["launch_rockets"]}, 422),
            ({"name": "take-home", "stage_type": "screen"}, 409),
            ({"name": "Second Applied", "stage_type": "applied"}, 422),
        ):
            assert (
                await client.post(url, json=payload, headers=alpha.headers(RECRUITER))
            ).status_code == expected
        assert (
            await client.post(
                url,
                json={"name": "X", "stage_type": "screen"},
                headers=alpha.headers(HIRING_MANAGER),
            )
        ).status_code == 403

        renamed = await client.patch(
            f"{url}/{take_home['id']}",
            json={"name": "Coding Exercise"},
            headers=alpha.headers(RECRUITER),
        )
        assert "Coding Exercise" in [s["name"] for s in renamed.json()]

        stages = renamed.json()
        applied, rest = stages[0], stages[1:]
        order = [applied["id"], *[s["id"] for s in reversed(rest)]]
        reordered = await client.put(
            f"{url}/order", json={"stage_ids": order}, headers=alpha.headers(RECRUITER)
        )
        assert [s["id"] for s in reordered.json()] == order
        bad_order = await client.put(
            f"{url}/order",
            json={"stage_ids": order[1:] + order[:1]},
            headers=alpha.headers(RECRUITER),
        )
        assert bad_order.status_code == 422  # Applied must stay first
        assert (
            await client.put(
                f"{url}/order", json={"stage_ids": order[:-1]}, headers=alpha.headers(RECRUITER)
            )
        ).status_code == 422

        app = await apply(client, admin_engine, alpha, job["id"])
        await move(client, alpha, job["id"], app["application_id"], "Coding Exercise")
        occupied = await client.delete(f"{url}/{take_home['id']}", headers=alpha.headers(RECRUITER))
        assert occupied.status_code == 409 and "1 candidate" in occupied.json()["detail"]
        assert (
            await client.delete(f"{url}/{applied['id']}", headers=alpha.headers(RECRUITER))
        ).status_code == 422
        await move(client, alpha, job["id"], app["application_id"], "Applied")
        deleted = await client.delete(f"{url}/{take_home['id']}", headers=alpha.headers(RECRUITER))
        assert deleted.status_code == 200 and "Coding Exercise" not in [
            s["name"] for s in deleted.json()
        ]
        assert [s["position_index"] for s in deleted.json()] == list(range(len(deleted.json())))
        actions = await client.get(f"{API}/stage-actions", headers=alpha.headers(HIRING_MANAGER))
        assert {a["key"] for a in actions.json()} >= {"ai_pre_screen", "polite_rejection_email"}


# =============================================================================================
# Team management
# =============================================================================================
class TestTeamManagement:
    async def test_admin_deactivates_and_reactivates_members(
        self, client: httpx.AsyncClient, alpha: Workspace
    ) -> None:
        temp = await add_member(client, alpha, INTERVIEWER, "Temp Contractor")
        url = f"{API}/users/{temp.user_id}"
        assert (
            await client.patch(url, json={"is_active": False}, headers=alpha.headers(RECRUITER))
        ).status_code == 403
        off = await client.patch(
            url,
            json={"is_active": False, "department": "Contractors"},
            headers=alpha.headers(ADMIN),
        )
        assert (
            off.status_code == 200
            and off.json()["is_active"] is False
            and off.json()["department"] == "Contractors"
        )
        locked_out = await client.get(
            f"{API}/auth/me", headers={"Authorization": f"Bearer {temp.token}"}
        )
        assert locked_out.status_code == 403
        assert (
            await client.patch(url, json={"is_active": True}, headers=alpha.headers(ADMIN))
        ).json()["is_active"] is True
        self_off = await client.patch(
            f"{API}/users/{alpha.members[ADMIN].user_id}",
            json={"is_active": False},
            headers=alpha.headers(ADMIN),
        )
        assert self_off.status_code == 422
        assert (
            await client.patch(url, json={"role": "admin"}, headers=alpha.headers(ADMIN))
        ).status_code == 422  # roles live in Clerk


# =============================================================================================
# Interviews: manual scheduling and Cal.com booking webhooks
# =============================================================================================
def calcom_body(
    app_email: str, organizer: str, uid: str, trigger: str = "BOOKING_CREATED"
) -> bytes:
    start = datetime(2026, 11, 3, 15, 0, tzinfo=UTC)
    return json.dumps(
        {
            "triggerEvent": trigger,
            "payload": {
                "uid": uid,
                "title": "Technical interview",
                "startTime": start.isoformat().replace("+00:00", "Z"),
                "endTime": (start + timedelta(minutes=45)).isoformat().replace("+00:00", "Z"),
                "attendees": [{"email": app_email.upper(), "name": "Candidate"}],
                "organizer": {"email": organizer, "name": "Interviewer"},
                "metadata": {"videoCallUrl": "https://meet.example.com/abc"},
            },
        }
    ).encode()


class TestInterviews:
    async def test_manual_scheduling_and_status(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, alpha: Workspace
    ) -> None:
        job = await create_job(client, alpha, taxonomy=GO_TAXONOMY)
        app = await apply(client, admin_engine, alpha, job["id"])
        url = f"{API}/applications/{app['application_id']}/interviews"
        body = {
            "title": "System design",
            "interviewer_id": str(alpha.members[INTERVIEWER].user_id),
            "scheduled_start": "2026-11-02T10:00:00+00:00",
            "scheduled_end": "2026-11-02T11:00:00+00:00",
            "meeting_url": "https://meet.example.com/xyz",
        }
        created = await client.post(url, json=body, headers=alpha.headers(HIRING_MANAGER))
        assert created.status_code == 201, created.text
        assert (
            created.json()["interviewer_name"].endswith("Interviewer")
            and created.json()["source"] == "manual"
        )
        bad = await client.post(
            url,
            json={**body, "scheduled_end": "2026-11-02T09:00:00+00:00"},
            headers=alpha.headers(),
        )
        assert bad.status_code == 422
        done = await client.patch(
            f"{API}/interviews/{created.json()['id']}",
            json={"status": "completed"},
            headers=alpha.headers(),
        )
        assert done.json()["status"] == "completed"
        assert len((await client.get(url, headers=alpha.headers())).json()) == 1
        assert (
            await client.post(url, json=body, headers=alpha.headers(INTERVIEWER))
        ).status_code == 403

    async def test_calcom_webhook_records_bookings(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, alpha: Workspace
    ) -> None:
        email = f"booked.{uuid.uuid4().hex[:6]}@example.com"
        job = await create_job(client, alpha, taxonomy=GO_TAXONOMY)
        app = await apply(client, admin_engine, alpha, job["id"], email=email)
        await move(client, alpha, job["id"], app["application_id"], "Interview")
        hook = f"{API}/webhooks/scheduling/calcom/{alpha.subdomain}"
        uid = f"cal_{uuid.uuid4().hex[:8]}"
        body = calcom_body(email, alpha.members[INTERVIEWER].email, uid)

        unconfigured = await client.post(hook, content=body, headers={"X-Cal-Signature-256": "x"})
        assert unconfigured.status_code == 403
        secret = "calcom-signing-secret-123"
        configured = await client.put(
            f"{API}/communications/calcom-webhook",
            json={"secret": secret},
            headers=alpha.headers(RECRUITER),
        )
        assert configured.json() == {
            "configured": True,
            "webhook_path": f"/api/v1/webhooks/scheduling/calcom/{alpha.subdomain}",
        }

        def sign(raw: bytes) -> dict[str, str]:
            return {
                "X-Cal-Signature-256": hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest(),
                "Content-Type": "application/json",
            }

        assert (
            await client.post(hook, content=body, headers={"X-Cal-Signature-256": "0" * 64})
        ).status_code == 403
        recorded = await client.post(hook, content=body, headers=sign(body))
        assert recorded.json() == {"status": "recorded"}
        assert (await client.post(hook, content=body, headers=sign(body))).json() == {
            "status": "recorded"
        }  # redelivery
        [interview] = (
            await client.get(
                f"{API}/applications/{app['application_id']}/interviews", headers=alpha.headers()
            )
        ).json()
        assert (
            interview["source"] == "calcom"
            and interview["meeting_url"] == "https://meet.example.com/abc"
        )
        assert interview["interviewer_id"] == str(alpha.members[INTERVIEWER].user_id)
        assert interview["scheduled_start"].startswith("2026-11-03T15:00")

        cancel = calcom_body(email, "x@y.z", uid, trigger="BOOKING_CANCELLED")
        assert (await client.post(hook, content=cancel, headers=sign(cancel))).json() == {
            "status": "cancelled"
        }
        stranger = calcom_body("nobody@example.com", "x@y.z", "cal_unknown")
        assert (await client.post(hook, content=stranger, headers=sign(stranger))).json() == {
            "status": "ignored"
        }

        settings = await client.get(f"{API}/communications/settings", headers=alpha.headers(ADMIN))
        assert settings.status_code == 200 and "calcom" not in json.dumps(
            settings.json()
        )  # secret never echoed


# =============================================================================================
# Offer letter PDF
# =============================================================================================
def _pdf_is_well_formed(pdf: bytes) -> bool:
    """Every xref offset must point at the matching 'n 0 obj' header."""
    xref = int(re.search(rb"startxref\n(\d+)", pdf).group(1))
    rows = pdf[xref:].split(b"\n")[3:]
    offsets = [int(r[:10]) for r in rows if r.endswith(b" n ")]
    return all(pdf[o:].startswith(f"{i} 0 obj".encode()) for i, o in enumerate(offsets, start=1))


class TestOfferLetters:
    async def test_generate_download_and_regenerate(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, alpha: Workspace
    ) -> None:
        job = await create_job(client, alpha, title="Staff Engineer", taxonomy=GO_TAXONOMY)
        app = await apply(client, admin_engine, alpha, job["id"], full_name="Olivia Offer")
        url = f"{API}/applications/{app['application_id']}/offer-letter"
        assert (await client.get(url, headers=alpha.headers())).status_code == 404
        terms = {
            "base_salary": "185000",
            "currency": "usd",
            "start_date": "2026-12-07",
            "equity": "0.1% over 4 years",
            "respond_by": "2026-11-20",
        }
        assert (
            await client.post(url, json=terms, headers=alpha.headers(HIRING_MANAGER))
        ).status_code == 403

        generated = await client.post(url, json=terms, headers=alpha.headers(RECRUITER))

        assert generated.status_code == 200, generated.text
        offer = generated.json()
        assert offer["details"]["candidate_name"] == "Olivia Offer"
        assert (
            offer["details"]["currency"] == "USD" and offer["details"]["base_salary"] == "185000.00"
        )
        pdf = (await client.get(offer["download_url"])).content
        assert pdf.startswith(b"%PDF-1.4") and pdf.rstrip().endswith(b"%%EOF")
        assert (
            b"Dear Olivia Offer," in pdf
            and b"USD 185,000.00" in pdf
            and b"December 07, 2026" in pdf
        )
        assert _pdf_is_well_formed(pdf)

        first_key = offer["download_url"].split("/storage/local/")[1].split("?")[0]
        regenerated = await client.post(
            url, json={**terms, "base_salary": "190000"}, headers=alpha.headers(ADMIN)
        )
        assert regenerated.json()["details"]["base_salary"] == "190000.00"
        assert not (
            get_settings().local_storage_dir / first_key
        ).exists()  # superseded letter removed
        assert (await client.get(url, headers=alpha.headers(HIRING_MANAGER))).status_code == 200

    def test_pdf_writer_handles_long_and_special_text(self) -> None:
        pdf = render_text_pdf(
            [("title", "Ünïcode (offer) \\ test")] + [("body", "word " * 400)] * 3,
            title="Long (test)",
        )
        assert _pdf_is_well_formed(pdf) and pdf.count(b"/Type /Page ") >= 2
