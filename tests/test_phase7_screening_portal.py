"""Phase 7 — L5 HR Tester: candidate screening portal, transcription, advisory rubric scoring.

Strict testing mandate: headless Python script tests only (pytest + httpx.AsyncClient).
No web/browser testing. The portal is exercised exactly as the browser would call it; the local
LLM and speech-to-text are deterministic fakes, plus an opt-in live faster-whisper test.
"""

from __future__ import annotations

import shutil
import subprocess
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import REPO_ROOT, get_settings
from app.models import UserRole
from app.services.events import get_event_broker
from app.services.llm_client import LLMUnavailableError
from app.services.screening_evaluator import FasterWhisperTranscriber, heuristic_rubric
from app.services.storage import get_storage_service
from app.workers import communications_worker
from tests.helpers import (
    API,
    GO_TAXONOMY,
    FakeLLM,
    FakeTranscriber,
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
QUESTIONS = {
    "questions": [
        {
            "question": "You know Go well; how would you build a Kubernetes operator?",
            "targets": "Kubernetes",
            "rationale": "gap",
        },
        {
            "question": "How would you compare gRPC and REST for internal services?",
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
            "justification": "Concrete reconcile loop.",
        },
        {
            "index": 1,
            "technical_depth": 3,
            "clarity": 5,
            "relevance": 4,
            "justification": "Clear trade-offs.",
        },
        {
            "index": 2,
            "technical_depth": 5,
            "clarity": 4,
            "relevance": 5,
            "justification": "Deep race analysis.",
        },
    ],
    "summary": "Strong Go engineer with sensible Kubernetes instincts.",
    "strengths": ["Concurrency debugging", "Clear communication"],
    "probe_questions": ["How would you test the operator's reconcile loop?"],
}
WEBM = b"\x1aE\xdf\xa3" + b"\x00" * 2048  # EBML header of a WebM/Opus recording


@pytest.fixture(scope="module")
async def workspaces(api_app: object) -> AsyncIterator[tuple[Workspace, Workspace]]:
    transport = httpx.ASGITransport(app=api_app)  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield await provision_workspace(http, "Ember"), await provision_workspace(http, "Fjord")


@pytest.fixture
def alpha(workspaces: tuple) -> Workspace:
    return workspaces[0]


@pytest.fixture
def beta(workspaces: tuple) -> Workspace:
    return workspaces[1]


@pytest.fixture
async def invited(
    client: httpx.AsyncClient, admin_engine: AsyncEngine, alpha: Workspace, api_app: Any
) -> dict[str, Any]:
    """Scored application with AI questions generated and the candidate invited."""
    from app.services.llm_client import get_llm_client

    job = await create_job(client, alpha, title="Platform Engineer", taxonomy=GO_TAXONOMY)
    app = await apply(client, admin_engine, alpha, job["id"], full_name="Sam Rivera")
    await post_webhook(client, m3_payload(app, full_name="Sam Rivera"))
    api_app.dependency_overrides[get_llm_client] = lambda: FakeLLM(QUESTIONS)
    try:
        generated = await client.post(
            f"{API}/applications/{app['application_id']}/screening-questions",
            json={"count": 3},
            headers=alpha.headers(RECRUITER),
        )
        assert generated.status_code == 200, generated.text
    finally:
        api_app.dependency_overrides.pop(get_llm_client, None)
    invite = await client.post(
        f"{API}/applications/{app['application_id']}/screening/invite",
        headers=alpha.headers(RECRUITER),
    )
    assert invite.status_code == 200, invite.text
    link = invite.json()["candidate_link"]
    return {
        **app,
        "job_id": job["id"],
        "token": link.rsplit("/screen/", 1)[1],
        "screening": invite.json(),
    }


def portal(token: str) -> str:
    return f"{API}/public/screenings/{token}"


async def answer_all(client: httpx.AsyncClient, token: str) -> None:
    assert (
        await client.put(
            f"{portal(token)}/answers/0",
            json={"text": "I'd write a reconcile loop in Go with controller-runtime."},
        )
    ).status_code == 200
    audio = await client.put(
        f"{portal(token)}/answers/1/audio",
        content=WEBM,
        headers={"Content-Type": "audio/webm;codecs=opus"},
    )
    assert audio.status_code == 200, audio.text
    assert (
        await client.put(
            f"{portal(token)}/answers/2",
            json={"text": "A data race in a Go worker pool; fixed with channels."},
        )
    ).status_code == 200


def worker_ctx(**overrides: Any) -> dict[str, Any]:
    return {
        "llm": FakeLLM(RUBRIC),
        "storage": get_storage_service(),
        "transcriber": FakeTranscriber(),
        "broker": get_event_broker(),
        **overrides,
    }


# =============================================================================================
# Secure, single-use signed token portal
# =============================================================================================
class TestPortalAccess:
    async def test_invitation_emails_a_signed_link_and_tracks_status(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, alpha: Workspace, invited: dict
    ) -> None:
        assert invited["screening"]["invited_at"] is not None
        assert invited["token"].count(".") == 3
        async with admin_engine.connect() as conn:
            email = (
                await conn.execute(
                    text(
                        "SELECT template_key, status FROM email_outbox WHERE application_id = :id AND template_key = 'screening_invitation'"
                    ),
                    {"id": invited["application_id"]},
                )
            ).one()
            status_ = await conn.scalar(
                text("SELECT status::text FROM job_applications WHERE id = :id"),
                {"id": invited["application_id"]},
            )
        assert email.status == "queued" and status_ == "screening_invited"

        preview = await client.post(
            f"{API}/communications/templates/screening_invitation/preview",
            json={"application_id": invited["application_id"]},
            headers=alpha.headers(RECRUITER),
        )
        assert f"/screen/{invited['token']}" in preview.json()["body"]  # link rebuilt at send time

    async def test_candidate_opens_portal_without_login(
        self, client: httpx.AsyncClient, alpha: Workspace, invited: dict
    ) -> None:
        response = await client.get(portal(invited["token"]))
        assert response.status_code == 200, response.text
        view = response.json()
        assert view["status"] == "opened" and view["company_name"] == alpha.name
        assert view["job_title"] == "Platform Engineer" and view["candidate_first_name"] == "Sam"
        assert [q["index"] for q in view["questions"]] == [0, 1, 2]
        assert set(view["questions"][0]) == {"index", "question"}  # targets/rationale stay internal
        assert view["limits"] == {
            "max_audio_seconds": 90,
            "max_audio_bytes": 10 * 1024 * 1024,
            "max_text_chars": 4000,
        }

        recruiter_view = (
            await client.get(
                f"{API}/applications/{invited['application_id']}/screening",
                headers=alpha.headers(HIRING_MANAGER),
            )
        ).json()
        assert recruiter_view["status"] == "opened" and recruiter_view["opened_at"]

    async def test_forged_replaced_and_cross_tenant_tokens_are_rejected(
        self, client: httpx.AsyncClient, alpha: Workspace, beta: Workspace, invited: dict
    ) -> None:
        tenant, screening, expires, signature = invited["token"].split(".")
        for bad in (
            f"{tenant}.{screening}.{expires}.{'0' * 64}",
            f"{beta.tenant_id}.{screening}.{expires}.{signature}",
            f"{tenant}.{screening}.{int(expires) + 3600}.{signature}",
            "not-a-token",
        ):
            assert (await client.get(portal(bad))).status_code == 403, bad

        reinvite = await client.post(
            f"{API}/applications/{invited['application_id']}/screening/invite",
            headers=alpha.headers(ADMIN),
        )
        new_token = reinvite.json()["candidate_link"].rsplit("/screen/", 1)[1]
        assert new_token != invited["token"]
        assert (await client.get(portal(invited["token"]))).status_code == 403  # old link revoked
        assert (await client.get(portal(new_token))).status_code == 200

    async def test_expired_links_return_410(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, alpha: Workspace, invited: dict
    ) -> None:
        async with admin_engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE screenings SET expires_at = now() - interval '1 minute' WHERE application_id = :id"
                ),
                {"id": invited["application_id"]},
            )
        view = (
            await client.get(
                f"{API}/applications/{invited['application_id']}/screening", headers=alpha.headers()
            )
        ).json()
        token = view["candidate_link"].rsplit("/screen/", 1)[1]
        expired = await client.get(portal(token))
        assert expired.status_code == 410 and "expired" in expired.json()["detail"]
        after = (
            await client.get(
                f"{API}/applications/{invited['application_id']}/screening", headers=alpha.headers()
            )
        ).json()
        assert after["status"] == "expired" and after["candidate_link"] is None


# =============================================================================================
# Text / audio answers, auto-save and the single-use submission
# =============================================================================================
class TestPortalAnswers:
    async def test_autosave_audio_and_submit_once(
        self,
        client: httpx.AsyncClient,
        admin_engine: AsyncEngine,
        alpha: Workspace,
        invited: dict,
        communications_queue: Any,
    ) -> None:
        token = invited["token"]
        draft = await client.put(f"{portal(token)}/answers/0", json={"text": "first draft"})
        assert draft.status_code == 200 and draft.json()["type"] == "text"
        await client.put(f"{portal(token)}/answers/0", json={"text": "second draft"})
        assert (await client.get(portal(token))).json()["answers"][0]["text"] == "second draft"

        incomplete = await client.post(f"{portal(token)}/submit")
        assert incomplete.status_code == 422 and "2, 3" in incomplete.json()["detail"]

        await answer_all(client, token)
        saved = (await client.get(portal(token))).json()["answers"]
        assert [a["type"] for a in saved] == ["text", "audio", "text"]
        assert saved[1]["has_audio"] and saved[1]["audio_bytes"] == len(WEBM)

        submitted = await client.post(f"{portal(token)}/submit")
        assert submitted.status_code == 202, submitted.text
        assert submitted.json()["status"] == "completed"
        screening_id = uuid.UUID(invited["screening"]["id"])
        assert communications_queue.evaluations[-1] == (alpha.tenant_id, screening_id)

        for call in (client.get(portal(token)), client.post(f"{portal(token)}/submit"),
                     client.put(f"{portal(token)}/answers/0", json={"text": "late edit"})):  # fmt: skip
            assert (await call).status_code == 410  # single use
        async with admin_engine.connect() as conn:
            status_ = await conn.scalar(
                text("SELECT status::text FROM job_applications WHERE id = :id"),
                {"id": invited["application_id"]},
            )
        assert status_ == "screening_completed"

    async def test_audio_validation_and_replacement(
        self, client: httpx.AsyncClient, invited: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        token = invited["token"]
        first = await client.put(
            f"{portal(token)}/answers/1/audio", content=WEBM, headers={"Content-Type": "audio/webm"}
        )
        assert first.status_code == 200
        storage_root = get_settings().local_storage_dir
        recordings = list((storage_root / "tenants").rglob("*.webm"))
        before = {p for p in recordings if invited["screening"]["id"] in str(p)}
        assert len(before) == 1

        await client.put(f"{portal(token)}/answers/1", json={"text": "Switched to writing."})
        after = {
            p
            for p in (storage_root / "tenants").rglob("*.webm")
            if invited["screening"]["id"] in str(p)
        }
        assert after == set()  # the replaced recording is deleted

        bad_type = await client.put(
            f"{portal(token)}/answers/1/audio",
            content=b"MZ",
            headers={"Content-Type": "application/x-msdownload"},
        )
        assert bad_type.status_code == 422
        assert (
            await client.put(f"{portal(token)}/answers/9", json={"text": "x"})
        ).status_code == 404
        assert (
            await client.put(f"{portal(token)}/answers/0", json={"text": "x" * 4001})
        ).status_code == 422
        monkeypatch.setattr(get_settings(), "SCREENING_MAX_AUDIO_BYTES", 100)
        too_big = await client.put(
            f"{portal(token)}/answers/1/audio", content=WEBM, headers={"Content-Type": "audio/webm"}
        )
        assert too_big.status_code == 413


# =============================================================================================
# Async processing: transcription + advisory rubric scorecard
# =============================================================================================
class TestEvaluationEngine:
    async def test_worker_transcribes_and_attaches_advisory_scorecard(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine, alpha: Workspace, invited: dict
    ) -> None:
        await move_to(client, alpha, invited, "Pre-Screen")
        await answer_all(client, invited["token"])
        await client.post(f"{portal(invited['token'])}/submit")
        transcriber = FakeTranscriber("I'd pick gRPC for typed, streaming internal APIs.")

        score = await communications_worker.evaluate_screening(
            worker_ctx(transcriber=transcriber), str(alpha.tenant_id), invited["screening"]["id"]
        )

        assert score == pytest.approx(4.3, abs=0.05)
        view = (
            await client.get(
                f"{API}/applications/{invited['application_id']}/screening",
                headers=alpha.headers(HIRING_MANAGER),
            )
        ).json()
        assert view["evaluation_status"] == "completed"
        evaluation = view["evaluation"]
        assert evaluation["source"] == "ai" and evaluation["advisory"] is True
        assert evaluation["requires_human_review"] is True
        assert [r["technical_depth"] for r in evaluation["rubric"]] == [4, 3, 5]
        assert all(
            1 <= r[c] <= 5
            for r in evaluation["rubric"]
            for c in ("technical_depth", "clarity", "relevance")
        )
        assert evaluation["category_averages"] == {
            "technical_depth": 4.0,
            "clarity": 4.33,
            "relevance": 4.67,
        }
        assert evaluation["strengths"] == ["Concurrency debugging", "Clear communication"]
        assert evaluation["probe_questions"]
        audio = view["responses"][1]
        assert (
            audio["type"] == "audio"
            and audio["transcript"] == "I'd pick gRPC for typed, streaming internal APIs."
        )
        assert audio["transcription_engine"] == "fake-whisper"
        playback = await client.get(audio["audio_url"])
        assert playback.status_code == 200 and playback.content == WEBM

        # Anti-bias guardrail: the scorecard never moves or rejects the candidate.
        pipeline = await board(client, alpha, invited["job_id"])
        card = next(
            c
            for s in pipeline["stages"]
            for c in s["cards"]
            if c["application_id"] == invited["application_id"]
        )
        assert column(pipeline, "Pre-Screen")["id"] == card["stage_id"]
        assert card["status"] == "screening_completed"
        inbox = (await client.get(f"{API}/notifications", headers=alpha.headers(RECRUITER))).json()
        assert any(n["type"] == "screening" and "(advisory)" in n["title"] for n in inbox["items"])
        async with admin_engine.connect() as conn:
            audit = await conn.scalar(
                text(
                    "SELECT metadata_json FROM audit_logs WHERE entity_id = :id AND action = 'screening_evaluated'"
                ),
                {"id": invited["application_id"]},
            )
        assert audit["advisory"] is True and audit["source"] == "ai"

    async def test_fallbacks_when_models_are_unavailable(
        self, client: httpx.AsyncClient, alpha: Workspace, invited: dict
    ) -> None:
        await answer_all(client, invited["token"])
        await client.post(f"{portal(invited['token'])}/submit")
        await communications_worker.evaluate_screening(
            worker_ctx(
                llm=FakeLLM(LLMUnavailableError("Ollama model not found")),
                transcriber=FakeTranscriber(fail=True),
            ),
            str(alpha.tenant_id),
            invited["screening"]["id"],
        )
        view = (
            await client.get(
                f"{API}/applications/{invited['application_id']}/screening", headers=alpha.headers()
            )
        ).json()
        evaluation = view["evaluation"]
        assert evaluation["source"] == "heuristic" and evaluation["model"] is None
        assert "listen to the recordings" in evaluation["note"]
        assert view["responses"][1]["transcript"] is None
        assert "unavailable" in view["responses"][1]["transcription_error"]
        assert view["responses"][1]["audio_url"]  # recording still available to the recruiter

    def test_heuristic_rubric_rewards_on_topic_substance(self) -> None:
        questions = [{"targets": "Kubernetes"}, {"targets": "gRPC"}]
        rubric = heuristic_rubric(
            questions,
            ["I built a Kubernetes operator with controller-runtime; reconcile loops, CRDs, finalizers, "
             "leader election and backoff, deployed with Helm across clusters for two years.", "dunno"],
            ["Kubernetes", "Go", "Helm"],
        )  # fmt: skip
        assert (
            rubric[0].relevance > rubric[1].relevance
            and rubric[0].technical_depth > rubric[1].technical_depth
        )
        assert all(
            1 <= getattr(r, c) <= 5
            for r in rubric
            for c in ("technical_depth", "clarity", "relevance")
        )

    async def test_pre_screen_stage_generates_questions_and_invites(
        self,
        client: httpx.AsyncClient,
        admin_engine: AsyncEngine,
        alpha: Workspace,
        communications_queue: Any,
    ) -> None:
        job = await create_job(client, alpha, taxonomy=GO_TAXONOMY)
        app = await apply(client, admin_engine, alpha, job["id"])
        await post_webhook(client, m3_payload(app))
        await move_to(client, alpha, {**app, "job_id": job["id"]}, "Pre-Screen")
        assert communications_queue.screenings[-1] == (
            alpha.tenant_id,
            uuid.UUID(app["application_id"]),
        )

        source = await communications_worker.generate_screening(
            {"llm": FakeLLM(QUESTIONS), "storage": get_storage_service()},
            str(alpha.tenant_id),
            app["application_id"],
        )
        assert source == "ai"
        view = (
            await client.get(
                f"{API}/applications/{app['application_id']}/screening", headers=alpha.headers()
            )
        ).json()
        assert view["invited_at"] and view["candidate_link"]
        async with admin_engine.connect() as conn:
            keys = set(
                (
                    await conn.execute(
                        text("SELECT template_key FROM email_outbox WHERE application_id = :id"),
                        {"id": app["application_id"]},
                    )
                ).scalars()
            )
        assert "screening_invitation" in keys

    async def test_rbac_and_isolation(
        self, client: httpx.AsyncClient, alpha: Workspace, beta: Workspace, invited: dict
    ) -> None:
        url = f"{API}/applications/{invited['application_id']}/screening"
        assert (
            await client.post(f"{url}/invite", headers=alpha.headers(HIRING_MANAGER))
        ).status_code == 403
        assert (await client.get(url, headers=alpha.headers(INTERVIEWER))).status_code == 403
        assert (await client.post(f"{url}/invite", headers=beta.headers())).status_code == 404


async def move_to(client: httpx.AsyncClient, workspace: Workspace, app: dict, stage: str) -> None:
    pipeline = await board(client, workspace, app["job_id"])
    card = next(
        c
        for s in pipeline["stages"]
        for c in s["cards"]
        if c["application_id"] == app["application_id"]
    )
    response = await client.patch(
        f"{API}/applications/{app['application_id']}/move-stage",
        json={"to_stage_id": column(pipeline, stage)["id"], "expected_version": card["version"]},
        headers=workspace.headers(RECRUITER),
    )
    assert response.status_code == 200, response.text


# =============================================================================================
# Opt-in: real on-device transcription (macOS `say` synthesises speech; faster-whisper base.en)
# =============================================================================================
WHISPER_CACHE = REPO_ROOT / ".cache" / "whisper"


@pytest.mark.skipif(
    shutil.which("say") is None or not any(WHISPER_CACHE.glob("**/model.bin")),
    reason="needs macOS `say` and a cached faster-whisper base.en model",
)
def test_live_faster_whisper_transcribes_speech(tmp_path: Path) -> None:
    audio = tmp_path / "answer.aiff"
    subprocess.run(
        [
            "say",
            "-o",
            str(audio),
            "I would build a Kubernetes operator in Go with a reconcile loop.",
        ],
        check=True,
    )
    transcriber = FasterWhisperTranscriber("base.en", cache_dir=str(WHISPER_CACHE))
    transcript = transcriber.transcribe(str(audio)).text.lower()
    assert "kubernetes" in transcript and "operator" in transcript
