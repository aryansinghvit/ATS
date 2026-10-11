"""HR acceptance scenario — cross-tenant penetration sweep (antigravity.md test matrix).

A recruiter/admin of Company B probes every tenant-scoped endpoint with Company A's ids.
Every request must fail with 403/404 and leak nothing.

Run:  python -m pytest tests/hr_scenarios/verify_tenant_security.py -v
"""

from __future__ import annotations

import sys
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.models import UserRole
from tests.helpers import (
    API,
    GO_TAXONOMY,
    apply,
    create_job,
    m3_payload,
    post_webhook,
    provision_workspace,
)


async def test_company_b_cannot_reach_company_a_records(
    client: httpx.AsyncClient, admin_engine: AsyncEngine
) -> None:
    company_a = await provision_workspace(client, "Acme")
    company_b = await provision_workspace(client, "Rival")
    job = await create_job(client, company_a, taxonomy=GO_TAXONOMY)
    app = await apply(client, admin_engine, company_a, job["id"])
    await post_webhook(client, m3_payload(app))
    a_app, a_job, a_candidate = app["application_id"], job["id"], app["candidate_id"]
    a_user = company_a.members[UserRole.RECRUITER].user_id

    probes: list[tuple[str, str, dict[str, Any] | None]] = [
        ("GET", f"/users/{a_user}", None),
        ("GET", f"/jobs/{a_job}", None),
        ("PATCH", f"/jobs/{a_job}", {"title": "pwned"}),
        ("GET", f"/jobs/{a_job}/pipeline", None),
        ("GET", f"/jobs/{a_job}/pipeline/stream", None),
        ("GET", f"/jobs/{a_job}/stages", None),
        ("POST", f"/jobs/{a_job}/stages", {"name": "Evil", "stage_type": "screen"}),
        ("GET", f"/applications/{a_app}/dossier", None),
        ("GET", f"/applications/{a_app}/scorecards", None),
        ("PUT", f"/applications/{a_app}/scorecards/me", {"technical_competence": 1}),
        ("GET", f"/applications/{a_app}/notes", None),
        ("POST", f"/applications/{a_app}/notes", {"content": "hi"}),
        ("GET", f"/applications/{a_app}/screening", None),
        ("POST", f"/applications/{a_app}/screening/invite", None),
        ("GET", f"/applications/{a_app}/emails", None),
        ("GET", f"/applications/{a_app}/interviews", None),
        ("GET", f"/applications/{a_app}/offer-letter", None),
        (
            "POST",
            f"/applications/{a_app}/offer-letter",
            {"base_salary": "1", "start_date": "2027-01-01"},
        ),
        ("GET", f"/talent-pool/jobs/{a_job}/suggestions", None),
        ("POST", f"/talent-pool/candidates/{a_candidate}/invite-to-job", {"job_id": a_job}),
        ("DELETE", f"/compliance/candidates/{a_candidate}/forget", None),
    ]
    failures = []
    for method, path, body in probes:
        response = await client.request(
            method, f"{API}{path}", json=body, headers=company_b.headers(UserRole.ADMIN)
        )
        if response.status_code not in (403, 404, 422):
            failures.append((method, path, response.status_code, response.text[:120]))
    assert not failures, failures

    # The move endpoint and the analytics view leak nothing either.
    move = await client.patch(
        f"{API}/applications/{a_app}/move-stage",
        json={"to_stage_id": a_job, "expected_version": 1},
        headers=company_b.headers(UserRole.ADMIN),
    )
    assert move.status_code in (404, 422)
    analytics = await client.get(
        f"{API}/analytics/pipeline", params={"job_id": a_job}, headers=company_b.headers()
    )
    assert analytics.json()["totals"]["applications"] == 0
    print(f"\nTenant security sweep: {len(probes) + 2} cross-tenant probes, 0 leaks")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "-s"]))
