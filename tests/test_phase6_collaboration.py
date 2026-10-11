"""Phase 6 — L5 HR Tester: interview scorecards (blind rules), team notes, notifications.

Strict testing mandate: headless Python script tests only (pytest + httpx.AsyncClient).
No web/browser testing.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.models import UserRole
from tests.helpers import (
    API,
    GO_TAXONOMY,
    Member,
    Workspace,
    add_member,
    apply,
    bearer,
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
FULL_SCORECARD = {
    "technical_competence": 4,
    "communication": 5,
    "problem_solving": 3,
    "overall_recommendation": "yes",
    "notes_markdown": "Strong systems design; solid Go.",
}


@pytest.fixture(scope="module")
async def workspaces(api_app: object) -> AsyncIterator[tuple[Workspace, Workspace, Member]]:
    transport = httpx.ASGITransport(app=api_app)  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        alpha = await provision_workspace(http, "Atlas")
        beta = await provision_workspace(http, "Borealis")
        second_interviewer = await add_member(http, alpha, INTERVIEWER, "Ivy Second")
        yield alpha, beta, second_interviewer


@pytest.fixture
def alpha(workspaces: tuple) -> Workspace:
    return workspaces[0]


@pytest.fixture
def beta(workspaces: tuple) -> Workspace:
    return workspaces[1]


@pytest.fixture
def interviewer_b(workspaces: tuple) -> Member:
    return workspaces[2]


@pytest.fixture
async def application(
    client: httpx.AsyncClient, admin_engine: AsyncEngine, alpha: Workspace
) -> dict[str, str]:
    job = await create_job(client, alpha, taxonomy=GO_TAXONOMY)
    app = await apply(client, admin_engine, alpha, job["id"])
    await post_webhook(client, m3_payload(app))
    return {**app, "job_id": job["id"]}


def headers(member: Member) -> dict[str, str]:
    return bearer(member.token)


async def save(
    client: httpx.AsyncClient, application_id: str, auth: dict[str, str], **body: Any
) -> httpx.Response:
    return await client.put(
        f"{API}/applications/{application_id}/scorecards/me", json=body, headers=auth
    )


async def board(client: httpx.AsyncClient, application_id: str, auth: dict[str, str]) -> dict:
    response = await client.get(f"{API}/applications/{application_id}/scorecards", headers=auth)
    assert response.status_code == 200, response.text
    return response.json()


# =============================================================================================
# Standardized rubric scorecards + blind rules
# =============================================================================================
class TestScorecards:
    async def test_rubric_drafts_submission_and_locking(
        self,
        client: httpx.AsyncClient,
        admin_engine: AsyncEngine,
        alpha: Workspace,
        application: dict,
    ) -> None:
        app_id = application["application_id"]
        auth = alpha.headers(INTERVIEWER)

        draft = await save(client, app_id, auth, technical_competence=4)
        assert draft.status_code == 200, draft.text
        assert draft.json()["is_submitted"] is False and draft.json()["communication"] is None

        incomplete = await save(client, app_id, auth, technical_competence=4, submit=True)
        assert incomplete.status_code == 422
        out_of_range = await save(client, app_id, auth, **{**FULL_SCORECARD, "communication": 6})
        assert out_of_range.status_code == 422
        bad_recommendation = await save(
            client, app_id, auth, **{**FULL_SCORECARD, "overall_recommendation": "maybe"}
        )
        assert bad_recommendation.status_code == 422

        submitted = await save(client, app_id, auth, **FULL_SCORECARD, submit=True)
        assert submitted.status_code == 200
        card = submitted.json()
        assert card["is_submitted"] and card["submitted_at"]
        assert (card["technical_competence"], card["communication"], card["problem_solving"]) == (
            4,
            5,
            3,
        )
        assert card["overall_recommendation"] == "yes" and card["evaluator_role"] == "interviewer"

        locked = await save(client, app_id, auth, **{**FULL_SCORECARD, "technical_competence": 1})
        assert locked.status_code == 409

        async with admin_engine.connect() as conn:
            audit = await conn.scalar(
                text(
                    "SELECT metadata_json FROM audit_logs WHERE entity_id = :id "
                    "AND action = 'scorecard_submitted'"
                ),
                {"id": app_id},
            )
        assert audit == {"recommendation": "yes", "blind": True}

    @pytest.mark.parametrize("recommendation", ["strong_yes", "yes", "neutral", "no", "strong_no"])
    async def test_all_five_recommendations_are_accepted(
        self, client: httpx.AsyncClient, alpha: Workspace, application: dict, recommendation: str
    ) -> None:
        response = await save(
            client,
            application["application_id"],
            alpha.headers(RECRUITER),
            **{**FULL_SCORECARD, "overall_recommendation": recommendation},
            submit=True,
        )
        assert (
            response.status_code == 200
            and response.json()["overall_recommendation"] == recommendation
        )

    async def test_blind_interviewers_never_see_colleagues_scorecards(
        self, client: httpx.AsyncClient, alpha: Workspace, interviewer_b: Member, application: dict
    ) -> None:
        app_id = application["application_id"]
        await save(client, app_id, alpha.headers(INTERVIEWER), **FULL_SCORECARD, submit=True)

        before = await board(client, app_id, headers(interviewer_b))
        assert before["others"] == [] and before["mine"] is None
        assert before["hidden_count"] == 1 and before["can_view_others"] is False
        assert before["summary"] is None
        assert "Blind review" in before["visibility_rule"]

        await save(
            client,
            app_id,
            headers(interviewer_b),
            **{**FULL_SCORECARD, "overall_recommendation": "no"},
            submit=True,
        )
        after = await board(client, app_id, headers(interviewer_b))
        # Even after submitting, a blind interviewer sees only their own scorecard.
        assert after["others"] == [] and after["hidden_count"] == 1
        assert after["mine"]["overall_recommendation"] == "no"

        dossier = await client.get(
            f"{API}/applications/{app_id}/dossier", headers=headers(interviewer_b)
        )
        assert dossier.json()["redacted"] is True  # no candidate PII while scoring

    async def test_hiring_manager_sees_feedback_only_after_submitting(
        self, client: httpx.AsyncClient, alpha: Workspace, application: dict
    ) -> None:
        app_id = application["application_id"]
        await save(client, app_id, alpha.headers(INTERVIEWER), **FULL_SCORECARD, submit=True)
        hm = alpha.headers(HIRING_MANAGER)

        hidden = await board(client, app_id, hm)
        assert hidden["others"] == [] and hidden["hidden_count"] == 1

        await save(client, app_id, hm, **FULL_SCORECARD)  # a draft does not unlock anything
        assert (await board(client, app_id, hm))["hidden_count"] == 1

        await save(
            client,
            app_id,
            hm,
            **{**FULL_SCORECARD, "overall_recommendation": "strong_yes"},
            submit=True,
        )
        visible = await board(client, app_id, hm)
        assert visible["can_view_others"] is True and visible["hidden_count"] == 0
        assert [c["evaluator_role"] for c in visible["others"]] == ["interviewer"]
        assert visible["summary"]["submitted"] == 2
        assert visible["summary"]["recommendations"] == {"yes": 1, "strong_yes": 1}

    async def test_recruiters_see_submitted_scorecards_but_never_drafts(
        self, client: httpx.AsyncClient, alpha: Workspace, interviewer_b: Member, application: dict
    ) -> None:
        app_id = application["application_id"]
        await save(client, app_id, alpha.headers(INTERVIEWER), **FULL_SCORECARD, submit=True)
        await save(client, app_id, headers(interviewer_b), technical_competence=2)  # draft only

        recruiter_view = await board(client, app_id, alpha.headers(RECRUITER))
        assert recruiter_view["can_view_others"] is True
        assert len(recruiter_view["others"]) == 1  # the draft stays private
        assert recruiter_view["others"][0]["evaluator_name"].endswith("Interviewer")
        assert recruiter_view["summary"]["averages"] == {
            "technical_competence": 4.0,
            "communication": 5.0,
            "problem_solving": 3.0,
        }

    async def test_scorecards_are_tenant_isolated(
        self, client: httpx.AsyncClient, beta: Workspace, application: dict
    ) -> None:
        app_id = application["application_id"]
        assert (
            await client.get(f"{API}/applications/{app_id}/scorecards", headers=beta.headers())
        ).status_code == 404
        assert (await save(client, app_id, beta.headers(), **FULL_SCORECARD)).status_code == 404


# =============================================================================================
# Internal team notes feed with @mentions -> notifications
# =============================================================================================
class TestNotesAndNotifications:
    async def test_note_with_mention_notifies_teammate(
        self,
        client: httpx.AsyncClient,
        admin_engine: AsyncEngine,
        alpha: Workspace,
        application: dict,
    ) -> None:
        app_id = application["application_id"]
        hm = alpha.members[HIRING_MANAGER]
        content = (
            f"Great Go depth. @[Hiring Manager]({hm.user_id}) can you take the system design round?"
        )

        created = await client.post(
            f"{API}/applications/{app_id}/notes",
            json={"content": content},
            headers=alpha.headers(RECRUITER),
        )

        assert created.status_code == 201, created.text
        note = created.json()
        assert note["mentions"] == [
            {"user_id": str(hm.user_id), "name": f"{alpha.name.split()[0]} Hiring_Manager"}
        ]
        feed = (
            await client.get(f"{API}/applications/{app_id}/notes", headers=alpha.headers(ADMIN))
        ).json()
        assert [n["id"] for n in feed] == [note["id"]] and feed[0]["author_name"].endswith(
            "Recruiter"
        )

        inbox = (
            await client.get(f"{API}/notifications", headers=alpha.headers(HIRING_MANAGER))
        ).json()
        mention = next(n for n in inbox["items"] if n["entity_id"] == app_id)
        assert mention["type"] == "mention" and inbox["unread"] >= 1
        assert "mentioned you" in mention["title"]
        assert "@Hiring Manager can you take" in mention["body"]
        assert mention["link"].endswith(f"?application={app_id}")
        # The author is not notified, and nobody else sees the HM's inbox.
        recruiter_inbox = (
            await client.get(f"{API}/notifications", headers=alpha.headers(RECRUITER))
        ).json()
        assert all(n["id"] != mention["id"] for n in recruiter_inbox["items"])

        async with admin_engine.connect() as conn:
            assert (
                await conn.scalar(
                    text(
                        "SELECT count(*) FROM audit_logs "
                        "WHERE entity_id = :id AND action = 'note_added'"
                    ),
                    {"id": app_id},
                )
                == 1
            )

    async def test_invalid_and_blind_mentions_are_rejected(
        self, client: httpx.AsyncClient, alpha: Workspace, beta: Workspace, application: dict
    ) -> None:
        app_id = application["application_id"]
        url = f"{API}/applications/{app_id}/notes"
        for target, reason in (
            (alpha.members[INTERVIEWER].user_id, "blind interviewer"),
            (beta.members[ADMIN].user_id, "other tenant"),
            (uuid.uuid4(), "unknown user"),
        ):
            response = await client.post(
                url,
                json={"content": f"ping @[Someone]({target})"},
                headers=alpha.headers(RECRUITER),
            )
            assert response.status_code == 422, reason
        assert (
            await client.post(url, json={"content": ""}, headers=alpha.headers())
        ).status_code == 422

    async def test_blind_interviewers_cannot_read_or_post_notes(
        self, client: httpx.AsyncClient, alpha: Workspace, beta: Workspace, application: dict
    ) -> None:
        url = f"{API}/applications/{application['application_id']}/notes"
        interviewer = alpha.headers(INTERVIEWER)
        assert (await client.get(url, headers=interviewer)).status_code == 403
        assert (
            await client.post(url, json={"content": "hi"}, headers=interviewer)
        ).status_code == 403
        assert (await client.get(url, headers=beta.headers())).status_code == 404

    async def test_notification_read_state(
        self, client: httpx.AsyncClient, alpha: Workspace, application: dict
    ) -> None:
        admin = alpha.members[ADMIN]
        for _ in range(2):
            await client.post(
                f"{API}/applications/{application['application_id']}/notes",
                json={"content": f"FYI @[Admin]({admin.user_id})"},
                headers=alpha.headers(RECRUITER),
            )
        inbox = (
            await client.get(f"{API}/notifications?unread_only=true", headers=alpha.headers(ADMIN))
        ).json()
        assert inbox["unread"] >= 2
        first = inbox["items"][0]["id"]
        assert (
            await client.post(f"{API}/notifications/{first}/read", headers=alpha.headers(ADMIN))
        ).json() == {"updated": 1}
        after = (await client.get(f"{API}/notifications", headers=alpha.headers(ADMIN))).json()
        assert after["unread"] == inbox["unread"] - 1

        other = await client.post(
            f"{API}/notifications/{first}/read", headers=alpha.headers(RECRUITER)
        )
        assert other.status_code == 404  # cannot touch someone else's notification
        cleared = await client.post(f"{API}/notifications/read-all", headers=alpha.headers(ADMIN))
        assert cleared.json()["updated"] == after["unread"]
        assert (await client.get(f"{API}/notifications", headers=alpha.headers(ADMIN))).json()[
            "unread"
        ] == 0
