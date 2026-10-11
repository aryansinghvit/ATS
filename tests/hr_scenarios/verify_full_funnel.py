"""HR acceptance scenario — the complete hiring journey end to end (task.md Prompt 7.4).

Run:  python -m pytest tests/hr_scenarios/verify_full_funnel.py -v
  or  python -m tests.hr_scenarios.verify_full_funnel

Strict testing mandate: headless Python only (pytest + httpx). No browser automation. External
dependencies are simulated at their boundaries exactly as they behave in production: the M3
extractor (HMAC-signed webhook), Ollama (deterministic fake), Whisper (fake transcriber),
Cal.com (HMAC-signed booking webhook) and email delivery (log transport).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import get_settings
from app.models import UserRole
from app.services.communications import LogEmailSender, dispatch_due_emails
from app.services.events import get_event_broker
from app.services.llm_client import get_llm_client
from app.services.storage import get_storage_service
from app.workers import communications_worker
from tests.helpers import (
    API,
    PDF_BYTES,
    FakeLLM,
    FakeTranscriber,
    apply,
    board,
    column,
    m3_payload,
    post_webhook,
    provision_workspace,
)

GENERATED_JD = {
    "title": "Senior Go Engineer",
    "department": "Platform",
    "seniority_level": "Senior",
    "employment_type": "full_time",
    "location": "Remote (EU)",
    "remote_policy": "remote",
    "compensation": {"min": None, "max": None, "currency": None, "equity": False},
    "about_team": "The platform team runs the payments infrastructure for millions of users.",
    "responsibilities": ["Build high-throughput Go services", "Operate Kubernetes workloads"],
    "required_qualifications": ["5+ years backend engineering", "Production Go experience"],
    "preferred_qualifications": ["Kafka or gRPC exposure"],
    "required_skills": [{"name": "golang", "min_years": 4}, {"name": "k8s", "min_years": 2}],
    "preferred_skills": [{"name": "grpc"}],
    "min_total_experience_years": 5,
    "education_level": None,
}
QUESTIONS = {
    "questions": [
        {
            "question": "You know Go well; how would you build a Kubernetes operator?",
            "targets": "Kubernetes",
            "rationale": "gap",
        },
        {
            "question": "When would you choose gRPC over REST for internal services?",
            "targets": "gRPC",
            "rationale": "gap",
        },
        {
            "question": "Describe the hardest concurrency bug you fixed in Go.",
            "targets": "Go",
            "rationale": "depth",
        },
    ]
}
RUBRIC = {
    "scores": [
        {
            "index": 0,
            "technical_depth": 4,
            "clarity": 4,
            "relevance": 5,
            "justification": "Concrete.",
        },
        {"index": 1, "technical_depth": 4, "clarity": 5, "relevance": 4, "justification": "Clear."},
        {"index": 2, "technical_depth": 5, "clarity": 4, "relevance": 5, "justification": "Deep."},
    ],
    "summary": "Strong Go engineer.",
    "strengths": ["Concurrency"],
    "probe_questions": ["Operator testing strategy?"],
}
CALCOM_SECRET = "full-funnel-calcom-secret"


async def _move(client: httpx.AsyncClient, ws: Any, job_id: str, app_id: str, stage: str) -> dict:
    pipeline = await board(client, ws, job_id)
    card = next(c for s in pipeline["stages"] for c in s["cards"] if c["application_id"] == app_id)
    response = await client.patch(
        f"{API}/applications/{app_id}/move-stage",
        json={"to_stage_id": column(pipeline, stage)["id"], "expected_version": card["version"]},
        headers=ws.headers(UserRole.RECRUITER),
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _emails(engine: AsyncEngine, app_id: str) -> dict[str, list[str]]:
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT template_key, status FROM email_outbox WHERE application_id = :id ORDER BY created_at"
                ),
                {"id": app_id},
            )
        ).all()
    result: dict[str, list[str]] = {}
    for key, status in rows:
        result.setdefault(key, []).append(status)
    return result


async def test_complete_hiring_journey(
    client: httpx.AsyncClient,
    admin_engine: AsyncEngine,
    api_app: Any,
    communications_queue: Any,
    extraction_queue: Any,
) -> None:
    report: list[str] = []

    # 1. Provision workspace; create a job requisition with an AI-generated description + taxonomy.
    ws = await provision_workspace(client, "Funnel")
    api_app.dependency_overrides[get_llm_client] = lambda: FakeLLM(GENERATED_JD)
    try:
        draft = (
            await client.post(
                f"{API}/jobs/generate-description",
                json={"prompt": "Senior Go engineer, Kubernetes, payments, remote EU"},
                headers=ws.headers(UserRole.RECRUITER),
            )
        ).json()
    finally:
        api_app.dependency_overrides.pop(get_llm_client, None)
    job = (
        await client.post(
            f"{API}/jobs",
            json={**{k: v for k, v in draft.items() if k != "model"}, "status": "published"},
            headers=ws.headers(UserRole.RECRUITER),
        )
    ).json()
    assert [s["name"] for s in job["taxonomy"]["required_skills"]] == ["Go", "Kubernetes"]
    report.append(f"1. Workspace '{ws.name}' + job '{job['title']}' (taxonomy: Go, Kubernetes)")

    # 2. Candidate drops a resume; the M3 webhook returns the parsed profile and a 92% score.
    star = await apply(
        client, admin_engine, ws, job["id"], email="star@example.com", full_name="Stella Star"
    )
    assert (star["tenant_id"], star["application_id"]) and any(
        str(app_id) == star["application_id"] for _, app_id in extraction_queue.jobs
    )
    payload = m3_payload(star, score=92.0, full_name="Stella Star")
    payload["deterministic_match"]["breakdown"]["missing_required"] = [{"skill": "Kubernetes"}]
    assert (await post_webhook(client, payload)).json()["status"] == "processed"
    rival = await apply(client, admin_engine, ws, job["id"], email="rival@example.com")
    await post_webhook(client, m3_payload(rival, score=61.0, full_name="Ray Rival"))
    report.append("2. Resume uploaded -> extraction queued -> signed webhook: score 92.0")

    # 3. Recruiter sees the candidate at the top of the Applied column.
    applied = column(await board(client, ws, job["id"]), "Applied")["cards"]
    assert applied[0]["application_id"] == star["application_id"]
    assert applied[0]["deterministic_match_score"] == 92.0
    report.append("3. Kanban: 92/100 card ranked first in Applied")

    # 4. Pre-Screen: dynamic questions -> portal answers (text + audio) -> rubric scorecard.
    await _move(client, ws, job["id"], star["application_id"], "Pre-Screen")
    assert communications_queue.screenings[-1][1] == uuid.UUID(star["application_id"])
    await communications_worker.generate_screening(
        {"llm": FakeLLM(QUESTIONS), "storage": get_storage_service()},
        star["tenant_id"],
        star["application_id"],
    )
    screening = (
        await client.get(
            f"{API}/applications/{star['application_id']}/screening", headers=ws.headers()
        )
    ).json()
    assert len(screening["questions"]) == 3 and screening["skill_gaps"][0] == "Kubernetes"
    token = screening["candidate_link"].rsplit("/screen/", 1)[1]
    portal = f"{API}/public/screenings/{token}"
    assert (await client.get(portal)).json()["status"] == "opened"
    await client.put(
        f"{portal}/answers/0", json={"text": "Reconcile loops with controller-runtime in Go."}
    )
    await client.put(
        f"{portal}/answers/1/audio",
        content=b"\x1aE\xdf\xa3" + b"\x00" * 512,
        headers={"Content-Type": "audio/webm"},
    )
    await client.put(
        f"{portal}/answers/2", json={"text": "Fixed a data race in a worker pool using channels."}
    )
    assert (await client.post(f"{portal}/submit")).status_code == 202
    score = await communications_worker.evaluate_screening(
        {
            "llm": FakeLLM(RUBRIC),
            "storage": get_storage_service(),
            "transcriber": FakeTranscriber(),
            "broker": get_event_broker(),
        },
        star["tenant_id"],
        screening["id"],
    )
    evaluated = (
        await client.get(
            f"{API}/applications/{star['application_id']}/screening", headers=ws.headers()
        )
    ).json()
    assert evaluated["evaluation"]["advisory"] is True and evaluated["responses"][1]["transcript"]
    report.append(
        f"4. Pre-Screen: 3 gap questions, portal answers (text+audio), advisory rubric {score}/5"
    )

    # 5. Interview booked through the Cal.com self-scheduling link (signed booking webhook).
    await _move(client, ws, job["id"], star["application_id"], "Interview")
    await client.put(
        f"{API}/communications/calcom-webhook", json={"secret": CALCOM_SECRET}, headers=ws.headers()
    )
    start = datetime.now(UTC).replace(microsecond=0) + timedelta(days=3)
    booking = json.dumps({
        "triggerEvent": "BOOKING_CREATED",
        "payload": {
            "uid": f"cal_{uuid.uuid4().hex[:8]}", "title": "Technical interview",
            "startTime": start.isoformat(), "endTime": (start + timedelta(hours=1)).isoformat(),
            "attendees": [{"email": "star@example.com"}],
            "organizer": {"email": ws.members[UserRole.INTERVIEWER].email},
            "metadata": {"videoCallUrl": "https://meet.example.com/funnel"},
        },
    }).encode()  # fmt: skip
    signature = hmac.new(CALCOM_SECRET.encode(), booking, hashlib.sha256).hexdigest()
    booked = await client.post(
        f"{API}/webhooks/scheduling/calcom/{ws.subdomain}",
        content=booking,
        headers={"X-Cal-Signature-256": signature, "Content-Type": "application/json"},
    )
    assert booked.json() == {"status": "recorded"}
    [interview] = (
        await client.get(
            f"{API}/applications/{star['application_id']}/interviews", headers=ws.headers()
        )
    ).json()
    report.append(
        f"5. Interview self-scheduled via Cal.com for {interview['scheduled_start'][:16]}"
    )

    # 6. Interviewer submits a blind scorecard (no PII, hidden from the hiring manager until they submit).
    blind = await client.put(
        f"{API}/applications/{star['application_id']}/scorecards/me",
        json={
            "technical_competence": 5,
            "communication": 4,
            "problem_solving": 5,
            "overall_recommendation": "strong_yes",
            "submit": True,
        },
        headers=ws.headers(UserRole.INTERVIEWER),
    )
    assert blind.status_code == 200
    dossier = (
        await client.get(
            f"{API}/applications/{star['application_id']}/dossier",
            headers=ws.headers(UserRole.INTERVIEWER),
        )
    ).json()
    assert dossier["redacted"] is True and dossier["candidate"]["email"] is None
    hm_view = (
        await client.get(
            f"{API}/applications/{star['application_id']}/scorecards",
            headers=ws.headers(UserRole.HIRING_MANAGER),
        )
    ).json()
    assert hm_view["hidden_count"] == 1
    report.append("6. Blind scorecard (Strong Yes) submitted; hidden from HM until they submit")

    # 7. Offer generated (stage move + offer letter PDF).
    await _move(client, ws, job["id"], star["application_id"], "Offer")
    offer = (
        await client.post(
            f"{API}/applications/{star['application_id']}/offer-letter",
            json={
                "base_salary": "160000",
                "currency": "EUR",
                "start_date": (datetime.now(UTC).date() + timedelta(days=30)).isoformat(),
            },
            headers=ws.headers(UserRole.RECRUITER),
        )
    ).json()
    pdf = (await client.get(offer["download_url"])).content
    assert pdf.startswith(b"%PDF") and b"Dear Stella Star," in pdf
    await _move(client, ws, job["id"], star["application_id"], "Hired")
    report.append("7. Offer letter PDF generated (EUR 160,000) -> candidate Hired")

    # 8. Second candidate rejected: the polite email is queued with a delay, not sent now.
    await _move(client, ws, job["id"], rival["application_id"], "Rejected")
    sender = LogEmailSender()
    await dispatch_due_emails(sender, limit=1000)
    assert not any(
        m["to"] == "rival@example.com" and "Your application" in m["subject"] for m in sender.sent
    )
    async with admin_engine.connect() as conn:
        scheduled = await conn.scalar(
            text(
                "SELECT scheduled_for FROM email_outbox WHERE application_id = :id AND template_key = 'rejection'"
            ),
            {"id": rival["application_id"]},
        )
    assert scheduled - datetime.now(UTC) >= timedelta(hours=24)
    star_emails = await _emails(admin_engine, star["application_id"])
    assert {
        "application_received",
        "screening_invitation",
        "interview_invitation",
        "offer_extended",
    } <= set(star_emails)
    report.append(
        f"8. Rejection queued for {scheduled:%a %d %b %H:%M} UTC (polite delay), not sent"
    )

    # 9. GDPR Article 17: erase the rejected candidate from PostgreSQL and storage.
    resume = get_settings().local_storage_dir / rival["s3_key"]
    assert resume.exists()
    erased = await client.delete(
        f"{API}/compliance/candidates/{rival['candidate_id']}/forget",
        headers=ws.headers(UserRole.ADMIN),
    )
    assert erased.status_code == 200 and not resume.exists()
    async with admin_engine.connect() as conn:
        leftovers = await conn.scalar(
            text(
                "SELECT (SELECT count(*) FROM candidates WHERE id = :c) + (SELECT count(*) FROM job_applications WHERE candidate_id = :c) + (SELECT count(*) FROM email_outbox WHERE application_id = :a)"
            ),
            {"c": rival["candidate_id"], "a": rival["application_id"]},
        )
    assert leftovers == 0
    report.append("9. GDPR erasure: candidate, application, emails and resume file removed")

    # Analytics reflect the journey.
    metrics = (
        await client.get(
            f"{API}/analytics/pipeline", params={"job_id": job["id"]}, headers=ws.headers()
        )
    ).json()
    assert metrics["totals"]["hired"] == 1 and metrics["time_to_hire"]["hired"] == 1
    report.append(
        f"10. Analytics: {metrics['totals']['applications']} application(s), 1 hire, funnel {[f['reached'] for f in metrics['funnel']]}"
    )

    print("\n" + "\n".join(["FULL-FUNNEL ACCEPTANCE REPORT", *report, "RESULT: PASS"]))
    assert PDF_BYTES  # the dropzone used a real PDF upload


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "-s"]))
