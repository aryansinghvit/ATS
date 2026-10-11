"""Phase 1.4 — L5 HR Tester: tenant provisioning, role JWTs, cross-tenant isolation and RBAC.

Strict testing mandate: headless Python script tests only (pytest + httpx.AsyncClient).
No web/browser testing.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwk, jwt
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.config import Settings, get_settings
from app.core.database import (
    AsyncSessionLocal,
    set_tenant_context,
    verify_runtime_role_enforces_rls,
)
from app.core.security import AuthenticationError, JWKSClient, TokenVerifier
from app.core.tenancy import HostTenantKey, normalize_subdomain, parse_tenant_host
from app.models import Tenant, User, UserRole
from tests.helpers import API, Workspace, bearer, mint_token, provision_workspace

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
        yield await provision_workspace(http, "Alpha"), await provision_workspace(http, "Beta")


@pytest.fixture
def alpha(workspaces: tuple[Workspace, Workspace]) -> Workspace:
    return workspaces[0]


@pytest.fixture
def beta(workspaces: tuple[Workspace, Workspace]) -> Workspace:
    return workspaces[1]


# =============================================================================================
# 1. Tenant provisioning creates an isolated workspace
# =============================================================================================
class TestTenantProvisioning:
    async def test_org_admin_provisions_isolated_workspace(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine
    ) -> None:
        suffix = uuid.uuid4().hex[:8]
        org_id = f"org_{suffix}"
        token = mint_token(
            clerk_user_id=f"user_founder_{suffix}",
            org_id=org_id,
            role=ADMIN,
            org_slug=f"Gamma Labs {suffix}",
        )
        response = await client.post(
            f"{API}/auth/sync-user",
            headers=bearer(token),
            json={
                "email": "Founder@Gamma.example.com",
                "full_name": "Gamma Founder",
                "organization": {"name": "Gamma Labs"},
            },
        )

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["tenant_created"] is True and body["user_created"] is True
        assert body["tenant"]["subdomain"] == f"gamma-labs-{suffix}"  # from the Clerk org slug
        assert body["tenant"]["primary_color"] == "#2563EB"
        assert body["user"]["role"] == "admin"
        assert body["user"]["email"] == "founder@gamma.example.com"
        assert body["user"]["tenant_id"] == body["tenant"]["id"]

        async with admin_engine.connect() as conn:  # ground truth beneath RLS
            row = (
                await conn.execute(
                    text("SELECT clerk_org_id, name FROM tenants WHERE id = :id"),
                    {"id": body["tenant"]["id"]},
                )
            ).one()
        assert row.clerk_org_id == org_id and row.name == "Gamma Labs"

        team = await client.get(f"{API}/users", headers=bearer(token))
        assert [member["id"] for member in team.json()] == [body["user"]["id"]]

    async def test_sync_is_idempotent_and_refreshes_profile(
        self, client: httpx.AsyncClient, alpha: Workspace
    ) -> None:
        admin = alpha.members[ADMIN]
        response = await client.post(
            f"{API}/auth/sync-user",
            headers=alpha.headers(ADMIN),
            json={"email": admin.email, "full_name": "Alpha Founder", "department": "Leadership"},
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["tenant_created"] is False and body["user_created"] is False
        assert body["tenant"]["id"] == str(alpha.tenant_id)
        assert body["user"]["id"] == str(admin.user_id)
        assert body["user"]["full_name"] == "Alpha Founder"
        assert body["user"]["department"] == "Leadership"

    async def test_only_an_org_admin_can_provision_a_workspace(
        self, client: httpx.AsyncClient
    ) -> None:
        suffix = uuid.uuid4().hex[:8]
        token = mint_token(clerk_user_id=f"user_{suffix}", org_id=f"org_{suffix}", role=RECRUITER)
        response = await client.post(
            f"{API}/auth/sync-user",
            headers=bearer(token),
            json={"email": "r@x.example.com", "full_name": "R", "organization": {"name": "X"}},
        )
        assert response.status_code == 403
        assert "admin" in response.json()["detail"]

    async def test_provisioning_requires_organization_details(
        self, client: httpx.AsyncClient
    ) -> None:
        suffix = uuid.uuid4().hex[:8]
        token = mint_token(clerk_user_id=f"user_{suffix}", org_id=f"org_{suffix}", role=ADMIN)
        response = await client.post(
            f"{API}/auth/sync-user",
            headers=bearer(token),
            json={"email": "a@x.example.com", "full_name": "A"},
        )
        assert response.status_code == 422

    @pytest.mark.parametrize("subdomain, expected", [("api", 422), ("--", 422)])
    async def test_invalid_or_reserved_subdomain_rejected(
        self, client: httpx.AsyncClient, subdomain: str, expected: int
    ) -> None:
        suffix = uuid.uuid4().hex[:8]
        token = mint_token(clerk_user_id=f"user_{suffix}", org_id=f"org_{suffix}", role=ADMIN)
        response = await client.post(
            f"{API}/auth/sync-user",
            headers=bearer(token),
            json={
                "email": "a@x.example.com",
                "full_name": "A",
                "organization": {"name": "X", "subdomain": subdomain},
            },
        )
        assert response.status_code == expected

    async def test_subdomain_is_globally_unique(
        self, client: httpx.AsyncClient, alpha: Workspace, admin_engine: AsyncEngine
    ) -> None:
        suffix = uuid.uuid4().hex[:8]
        org_id = f"org_{suffix}"
        token = mint_token(clerk_user_id=f"user_{suffix}", org_id=org_id, role=ADMIN)
        response = await client.post(
            f"{API}/auth/sync-user",
            headers=bearer(token),
            json={
                "email": "a@squatter.example.com",
                "full_name": "Squatter",
                "organization": {"name": "Squatter", "subdomain": alpha.subdomain},
            },
        )
        assert response.status_code == 409
        async with admin_engine.connect() as conn:
            created = await conn.scalar(
                text("SELECT count(*) FROM tenants WHERE clerk_org_id = :org"), {"org": org_id}
            )
        assert created == 0

    async def test_email_must_be_unique_within_workspace(
        self, client: httpx.AsyncClient, alpha: Workspace
    ) -> None:
        token = mint_token(clerk_user_id="user_impostor", org_id=alpha.org_id, role=RECRUITER)
        response = await client.post(
            f"{API}/auth/sync-user",
            headers=bearer(token),
            json={"email": alpha.members[RECRUITER].email, "full_name": "Impostor"},
        )
        assert response.status_code == 409

    async def test_unsynced_identities_are_denied(
        self, client: httpx.AsyncClient, alpha: Workspace
    ) -> None:
        unknown_org = mint_token(clerk_user_id="user_nobody", org_id="org_unknown", role=ADMIN)
        unsynced_member = mint_token(clerk_user_id="user_unsynced", org_id=alpha.org_id, role=ADMIN)
        for token in (unknown_org, unsynced_member):
            response = await client.get(f"{API}/auth/me", headers=bearer(token))
            assert response.status_code == 403
            assert "sync-user" in response.json()["detail"]

    async def test_deactivated_user_is_locked_out(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine
    ) -> None:
        workspace = await provision_workspace(client, "Epsilon")
        member = workspace.members[RECRUITER]
        async with admin_engine.begin() as conn:
            await conn.execute(
                text("UPDATE users SET is_active = false WHERE id = :id"), {"id": member.user_id}
            )
        me = await client.get(f"{API}/auth/me", headers=bearer(member.token))
        resync = await client.post(
            f"{API}/auth/sync-user",
            headers=bearer(member.token),
            json={"email": member.email, "full_name": "Back Again"},
        )
        assert me.status_code == 403 and resync.status_code == 403

    async def test_schema_enables_rls_and_required_extensions(
        self, admin_engine: AsyncEngine
    ) -> None:
        async with admin_engine.connect() as conn:
            rls = dict(
                (
                    await conn.execute(
                        text(
                            "SELECT relname, relrowsecurity FROM pg_class "
                            "WHERE relname IN ('tenants', 'users') AND relkind = 'r'"
                        )
                    )
                ).all()
            )
            policies = set(
                (await conn.execute(text("SELECT policyname FROM pg_policies"))).scalars()
            )
            installed = set(
                (await conn.execute(text("SELECT extname FROM pg_extension"))).scalars()
            )
            available = set(
                (await conn.execute(text("SELECT name FROM pg_available_extensions"))).scalars()
            )
        assert rls == {"tenants": True, "users": True}
        assert {"tenant_isolation_tenants", "tenant_isolation_users"} <= policies
        assert "vector" in installed
        for optional in ("uuid-ossp", "pgcrypto"):  # enabled wherever the server ships them
            assert (optional in installed) == (optional in available)

    async def test_runtime_role_cannot_bypass_rls(self, admin_engine: AsyncEngine) -> None:
        async with AsyncSessionLocal() as session:
            await verify_runtime_role_enforces_rls(session)  # ats_app: must not raise
        async with AsyncSession(admin_engine) as superuser_session:
            with pytest.raises(RuntimeError, match="superuser"):
                await verify_runtime_role_enforces_rls(superuser_session)


# =============================================================================================
# 2. Every role logs in with a valid JWT carrying the correct role claim
# =============================================================================================
class TestRoleTokens:
    @pytest.mark.parametrize("role", list(UserRole), ids=lambda role: role.value)
    async def test_each_role_receives_valid_jwt_with_correct_claims(
        self, client: httpx.AsyncClient, alpha: Workspace, role: UserRole
    ) -> None:
        member = alpha.members[role]

        claims = await TokenVerifier(get_settings()).verify(member.token)
        assert claims.role is role
        assert claims.org_role == f"org:{role.value}"
        assert claims.org_id == alpha.org_id
        assert claims.user_id == member.clerk_user_id

        response = await client.get(f"{API}/auth/me", headers=bearer(member.token))
        assert response.status_code == 200, response.text
        me = response.json()
        assert me["user"]["role"] == role.value
        assert me["user"]["id"] == str(member.user_id)
        assert me["tenant"]["id"] == str(alpha.tenant_id)
        assert me["tenant"]["subdomain"] == alpha.subdomain
        assert "settings" not in me["tenant"]  # admin-only workspace data is not exposed

    async def test_clerk_v2_session_token_shape_is_supported(
        self, client: httpx.AsyncClient, alpha: Workspace
    ) -> None:
        member = alpha.members[HIRING_MANAGER]
        token = mint_token(
            clerk_user_id=member.clerk_user_id,
            org_id=alpha.org_id,
            role=HIRING_MANAGER,
            token_version=2,
        )
        assert jwt.get_unverified_claims(token)["o"]["rol"] == "hiring_manager"
        response = await client.get(f"{API}/auth/me", headers=bearer(token))
        assert response.status_code == 200
        assert response.json()["user"]["role"] == "hiring_manager"

    async def test_clerk_basic_member_maps_to_least_privileged_role(self) -> None:
        token = mint_token(clerk_user_id="user_m", org_id="org_m", role="org:member")
        claims = await TokenVerifier(get_settings()).verify(token)
        assert claims.role is INTERVIEWER

    async def test_role_change_in_clerk_is_applied_on_next_request(
        self, client: httpx.AsyncClient, admin_engine: AsyncEngine
    ) -> None:
        workspace = await provision_workspace(client, "Delta")
        member = workspace.members[INTERVIEWER]
        promoted = mint_token(
            clerk_user_id=member.clerk_user_id, org_id=workspace.org_id, role=HIRING_MANAGER
        )
        response = await client.get(f"{API}/auth/me", headers=bearer(promoted))
        assert response.json()["user"]["role"] == "hiring_manager"
        async with admin_engine.connect() as conn:
            stored = await conn.scalar(
                text("SELECT role::text FROM users WHERE id = :id"), {"id": member.user_id}
            )
        assert stored == "hiring_manager"

    async def test_invalid_credentials_are_rejected(
        self, client: httpx.AsyncClient, alpha: Workspace
    ) -> None:
        settings = get_settings()
        secret = settings.MOCK_JWT_SECRET.get_secret_value()
        now = int(time.time())
        base = {
            "sub": alpha.members[ADMIN].clerk_user_id,
            "org_id": alpha.org_id,
            "org_role": "org:admin",
            "iss": settings.MOCK_JWT_ISSUER,
            "iat": now,
            "exp": now + 300,
        }

        def b64(data: dict[str, object]) -> str:
            return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

        def without(*keys: str) -> dict[str, object]:
            return {key: value for key, value in base.items() if key not in keys}

        cases: dict[str, tuple[str | None, int]] = {
            "missing_token": (None, 401),
            "malformed": ("not-a-jwt", 401),
            "expired": (jwt.encode({**base, "iat": now - 600, "exp": now - 60}, secret), 401),
            "wrong_signing_key": (jwt.encode(base, "attacker-controlled-secret"), 401),
            "wrong_issuer": (jwt.encode({**base, "iss": "https://evil.example.com"}, secret), 401),
            "missing_exp": (jwt.encode(without("exp"), secret), 401),
            "alg_none": (f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64(base)}.", 401),
            "no_active_org": (jwt.encode(without("org_id", "org_role"), secret), 403),
            "unknown_role": (jwt.encode({**base, "org_role": "org:billing_admin"}, secret), 403),
        }
        for name, (token, expected) in cases.items():
            response = await client.get(f"{API}/auth/me", headers=bearer(token) if token else {})
            assert response.status_code == expected, (
                f"{name}: {response.status_code} {response.text}"
            )
            if expected == 401:
                assert response.headers.get("www-authenticate") == "Bearer", name


class TestClerkJWKSVerification:
    """The production RS256 path, with Clerk's JWKS endpoint served by an in-process mock."""

    ISSUER = "https://clerk.ats.test"
    JWKS_URL = "https://clerk.ats.test/.well-known/jwks.json"
    KID = "ins_test_key_1"

    @pytest.fixture(scope="class")
    @classmethod
    def rsa_keys(cls) -> tuple[bytes, bytes, dict[str, str]]:
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        private_pem = private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        public_pem = private_key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        public_jwk = {**jwk.construct(public_pem, "RS256").to_dict(), "kid": cls.KID, "use": "sig"}
        return private_pem, public_pem, public_jwk

    def _verifier(self, public_jwk: dict[str, str], fetches: list[str]) -> TokenVerifier:
        def serve_jwks(request: httpx.Request) -> httpx.Response:
            fetches.append(str(request.url))
            return httpx.Response(200, json={"keys": [public_jwk]})

        settings = Settings(
            _env_file=None,
            ENVIRONMENT="test",
            AUTH_MOCK_MODE=False,
            CLERK_JWKS_URL=self.JWKS_URL,
            CLERK_ISSUER=self.ISSUER,
            CLERK_AUTHORIZED_PARTIES=["http://localhost:3000"],
        )
        jwks_client = JWKSClient(
            self.JWKS_URL,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(serve_jwks)),
            min_refresh_interval_seconds=0,
        )
        return TokenVerifier(settings, jwks_client)

    def _token(self, private_pem: bytes, kid: str | None = None, **overrides: object) -> str:
        now = int(time.time())
        claims = {
            "sub": "user_clerk_1",
            "iss": self.ISSUER,
            "iat": now,
            "nbf": now,
            "exp": now + 60,
            "azp": "http://localhost:3000",
            "sid": "sess_1",
            "v": 2,
            "o": {"id": "org_clerk_1", "rol": "interviewer", "slg": "acme"},
            **overrides,
        }
        return jwt.encode(claims, private_pem, algorithm="RS256", headers={"kid": kid or self.KID})

    async def test_valid_clerk_token_is_verified_and_jwks_cached(self, rsa_keys) -> None:
        private_pem, _, public_jwk = rsa_keys
        fetches: list[str] = []
        verifier = self._verifier(public_jwk, fetches)

        claims = await verifier.verify(self._token(private_pem))
        await verifier.verify(self._token(private_pem))

        assert (claims.user_id, claims.org_id, claims.org_slug) == (
            "user_clerk_1",
            "org_clerk_1",
            "acme",
        )
        assert claims.role is INTERVIEWER
        assert fetches == [self.JWKS_URL]  # second verification served from cache

    async def test_rejects_forged_or_misdirected_clerk_tokens(self, rsa_keys) -> None:
        private_pem, public_pem, public_jwk = rsa_keys
        fetches: list[str] = []
        verifier = self._verifier(public_jwk, fetches)
        valid = self._token(private_pem)

        # Privilege escalation by editing the payload but keeping the original signature.
        header, payload, signature = valid.split(".")
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        claims["o"]["rol"] = "admin"
        forged_payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
        tampered = f"{header}.{forged_payload}.{signature}"

        # Algorithm confusion: HS256 "signed" with the public key as the HMAC secret.
        confused_header = base64.urlsafe_b64encode(
            json.dumps({"alg": "HS256", "kid": self.KID}).encode()
        ).rstrip(b"=")
        signing_input = confused_header + b"." + payload.encode()
        confused_sig = base64.urlsafe_b64encode(
            hmac.new(public_pem, signing_input, hashlib.sha256).digest()
        ).rstrip(b"=")
        confused = (signing_input + b"." + confused_sig).decode()

        other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        cases = {
            "tampered_payload": tampered,
            "alg_confusion": confused,
            "wrong_issuer": self._token(private_pem, iss="https://evil.example.com"),
            "unauthorized_party": self._token(private_pem, azp="https://evil.example.com"),
            "expired": self._token(
                private_pem, iat=int(time.time()) - 600, exp=int(time.time()) - 60
            ),
            "unknown_kid": self._token(other_key, kid="ins_rotated_unknown"),
            "untrusted_key_same_kid": self._token(other_key),
        }
        for name, token in cases.items():
            with pytest.raises(AuthenticationError):
                await verifier.verify(token)
                pytest.fail(f"{name} was accepted")


# =============================================================================================
# 3. Cross-tenant leak tests: Tenant A can never reach Tenant B's records
# =============================================================================================
class TestCrossTenantIsolation:
    async def test_tenant_a_user_cannot_read_tenant_b_users(
        self, client: httpx.AsyncClient, alpha: Workspace, beta: Workspace
    ) -> None:
        for role in UserRole:
            for target in beta.members.values():
                response = await client.get(
                    f"{API}/users/{target.user_id}", headers=alpha.headers(role)
                )
                assert response.status_code == 404, (role, target.role, response.text)
        own = await client.get(
            f"{API}/users/{alpha.members[RECRUITER].user_id}", headers=alpha.headers(INTERVIEWER)
        )
        assert own.status_code == 200

    async def test_team_directory_contains_only_own_tenant(
        self, client: httpx.AsyncClient, alpha: Workspace, beta: Workspace
    ) -> None:
        response = await client.get(f"{API}/users", headers=alpha.headers(ADMIN))
        assert response.status_code == 200
        listed = {uuid.UUID(member["id"]) for member in response.json()}
        assert listed == {member.user_id for member in alpha.members.values()}
        assert not listed & {member.user_id for member in beta.members.values()}
        assert {member["tenant_id"] for member in response.json()} == {str(alpha.tenant_id)}

    async def test_token_is_rejected_on_another_tenants_subdomain(
        self, client: httpx.AsyncClient, alpha: Workspace, beta: Workspace
    ) -> None:
        foreign_host = {**alpha.headers(ADMIN), "Host": f"{beta.subdomain}.localhost"}
        own_host = {**alpha.headers(ADMIN), "Host": f"{alpha.subdomain}.localhost"}
        assert (await client.get(f"{API}/auth/me", headers=foreign_host)).status_code == 403
        assert (await client.get(f"{API}/users", headers=foreign_host)).status_code == 403
        assert (await client.get(f"{API}/auth/me", headers=own_host)).status_code == 200

    async def test_identity_not_provisioned_in_tenant_b_is_denied(
        self, client: httpx.AsyncClient, alpha: Workspace, beta: Workspace
    ) -> None:
        """Alpha's admin presents a Beta organization context without being a Beta member."""
        token = mint_token(
            clerk_user_id=alpha.members[ADMIN].clerk_user_id, org_id=beta.org_id, role=ADMIN
        )
        target = beta.members[RECRUITER].user_id
        for path in ("/auth/me", "/users", f"/users/{target}", "/workspace/settings"):
            response = await client.get(f"{API}{path}", headers=bearer(token))
            assert response.status_code == 403, (path, response.text)

    async def test_workspace_settings_changes_are_tenant_scoped(
        self, client: httpx.AsyncClient, alpha: Workspace, beta: Workspace
    ) -> None:
        before = (await client.get(f"{API}/workspace/settings", headers=beta.headers())).json()
        patched = await client.patch(
            f"{API}/workspace/settings",
            headers=alpha.headers(),
            json={"name": "Alpha Renamed", "settings": {"timezone": "UTC"}},
        )
        assert patched.status_code == 200 and patched.json()["name"] == "Alpha Renamed"
        after = (await client.get(f"{API}/workspace/settings", headers=beta.headers())).json()
        assert after["name"] == before["name"]
        assert "timezone" not in after["settings"]

    async def test_rls_hides_other_tenants_rows_at_database_level(
        self, alpha: Workspace, beta: Workspace, admin_engine: AsyncEngine
    ) -> None:
        async with admin_engine.connect() as conn:  # ground truth: Beta's rows really exist
            beta_rows = await conn.scalar(
                text("SELECT count(*) FROM users WHERE tenant_id = :t"), {"t": beta.tenant_id}
            )
        assert beta_rows == len(beta.members)

        async with AsyncSessionLocal() as session:
            await set_tenant_context(session, alpha.tenant_id)
            visible_tenants = set((await session.execute(select(User.tenant_id))).scalars())
            explicit_beta = (
                await session.execute(select(User).where(User.tenant_id == beta.tenant_id))
            ).all()
            tenant_rows = list((await session.execute(select(Tenant.id))).scalars())
        assert visible_tenants == {alpha.tenant_id}
        assert explicit_beta == []
        assert tenant_rows == [alpha.tenant_id]

    async def test_rls_fails_closed_without_tenant_context(self) -> None:
        async with AsyncSessionLocal() as session:
            users = await session.scalar(select(func.count()).select_from(User))
            tenants = await session.scalar(select(func.count()).select_from(Tenant))
        assert users == 0 and tenants == 0

    async def test_rls_blocks_cross_tenant_writes(
        self, alpha: Workspace, beta: Workspace, admin_engine: AsyncEngine
    ) -> None:
        async with AsyncSessionLocal() as session:
            await set_tenant_context(session, alpha.tenant_id)
            updated = await session.execute(
                update(User).where(User.tenant_id == beta.tenant_id).values(full_name="pwned")
            )
            deleted = await session.execute(delete(User).where(User.tenant_id == beta.tenant_id))
            assert updated.rowcount == 0 and deleted.rowcount == 0
            await session.commit()

            session.add(
                User(
                    tenant_id=beta.tenant_id,
                    clerk_user_id="user_intruder",
                    email="intruder@evil.example.com",
                    full_name="Intruder",
                    role=ADMIN,
                )
            )
            with pytest.raises(DBAPIError, match="row-level security"):
                await session.flush()
            await session.rollback()

            with pytest.raises(DBAPIError, match="row-level security"):
                await session.execute(
                    update(User)
                    .where(User.id == alpha.members[RECRUITER].user_id)
                    .values(tenant_id=beta.tenant_id)
                )
            await session.rollback()

        async with admin_engine.connect() as conn:
            names = set(
                (
                    await conn.execute(
                        text("SELECT full_name FROM users WHERE tenant_id = :t"),
                        {"t": beta.tenant_id},
                    )
                ).scalars()
            )
            intruders = await conn.scalar(
                text("SELECT count(*) FROM users WHERE clerk_user_id = 'user_intruder'")
            )
            recruiter_tenant = await conn.scalar(
                text("SELECT tenant_id FROM users WHERE id = :id"),
                {"id": alpha.members[RECRUITER].user_id},
            )
        assert "pwned" not in names and len(names) == len(beta.members)
        assert intruders == 0
        assert recruiter_tenant == alpha.tenant_id

    async def test_tenant_context_is_transaction_scoped_and_survives_commit(
        self, alpha: Workspace
    ) -> None:
        current = text("SELECT current_setting('app.current_tenant_id', true)")
        async with AsyncSessionLocal() as session:
            await set_tenant_context(session, alpha.tenant_id)
            assert await session.scalar(select(func.count()).select_from(User)) == len(
                alpha.members
            )
            await session.commit()
            # The after_begin hook re-issues SET LOCAL for the next transaction.
            assert await session.scalar(current) == str(alpha.tenant_id)
            assert await session.scalar(select(func.count()).select_from(User)) == len(
                alpha.members
            )

        async with AsyncSessionLocal() as fresh:  # pooled connection carries no tenant over
            assert await fresh.scalar(current) in (None, "")
            assert await fresh.scalar(select(func.count()).select_from(User)) == 0


class TestHostRouting:
    @pytest.mark.parametrize(
        "host, expected",
        [
            ("acme.localhost", HostTenantKey("subdomain", "acme")),
            ("ACME.localhost:8001", HostTenantKey("subdomain", "acme")),
            ("careers.acme.com", HostTenantKey("custom_domain", "careers.acme.com")),
            ("localhost:8001", None),
            ("api.localhost", None),  # reserved platform subdomain
            ("deep.acme.localhost", None),
            ("127.0.0.1:8001", None),
            ("[::1]:8001", None),
            ("test", None),
            (None, None),
        ],
    )
    def test_parse_tenant_host(self, host: str | None, expected: HostTenantKey | None) -> None:
        settings = get_settings()
        parsed = parse_tenant_host(
            host,
            root_domain=settings.PLATFORM_ROOT_DOMAIN,
            reserved_subdomains=settings.RESERVED_SUBDOMAINS,
        )
        assert parsed == expected

    def test_normalize_subdomain(self) -> None:
        reserved = get_settings().RESERVED_SUBDOMAINS
        assert normalize_subdomain("  Acme Corp! ", reserved_subdomains=reserved) == "acme-corp"
        for bad in ("api", "---", ""):
            with pytest.raises(ValueError):
                normalize_subdomain(bad, reserved_subdomains=reserved)

    async def test_public_branding_resolves_subdomain_and_custom_domain(
        self, client: httpx.AsyncClient, alpha: Workspace, beta: Workspace
    ) -> None:
        by_subdomain = await client.get(
            f"{API}/public/tenant", headers={"Host": f"{beta.subdomain}.localhost"}
        )
        assert by_subdomain.status_code == 200
        assert by_subdomain.json()["subdomain"] == beta.subdomain
        assert set(by_subdomain.json()) == {"name", "subdomain", "logo_url", "primary_color"}

        domain = f"careers-{uuid.uuid4().hex[:8]}.example.com"
        claimed = await client.patch(
            f"{API}/workspace/settings", headers=alpha.headers(), json={"custom_domain": domain}
        )
        assert claimed.status_code == 200 and claimed.json()["custom_domain"] == domain
        by_domain = await client.get(f"{API}/public/tenant", headers={"Host": f"{domain}:443"})
        assert by_domain.status_code == 200 and by_domain.json()["subdomain"] == alpha.subdomain

        stolen = await client.patch(
            f"{API}/workspace/settings", headers=beta.headers(), json={"custom_domain": domain}
        )
        assert stolen.status_code == 409

        for host in ("nobody-here.localhost", "test"):
            response = await client.get(f"{API}/public/tenant", headers={"Host": host})
            assert response.status_code == 404, host


# =============================================================================================
# 4. Role permissions: only Admin / Founder may manage workspace settings & billing
# =============================================================================================
class TestRolePermissions:
    @pytest.mark.parametrize("role", [INTERVIEWER, HIRING_MANAGER, RECRUITER], ids=str)
    @pytest.mark.parametrize(
        "method, path",
        [
            ("GET", "/workspace/settings"),
            ("PATCH", "/workspace/settings"),
            ("GET", "/workspace/billing"),
        ],
    )
    async def test_non_admin_roles_are_forbidden(
        self, client: httpx.AsyncClient, alpha: Workspace, role: UserRole, method: str, path: str
    ) -> None:
        response = await client.request(
            method,
            f"{API}{path}",
            headers=alpha.headers(role),
            json={"name": "Hijacked"} if method == "PATCH" else None,
        )
        assert response.status_code == 403, response.text
        assert "admin" in response.json()["detail"]

    async def test_interviewer_cannot_alter_billing_or_settings(
        self, client: httpx.AsyncClient, alpha: Workspace
    ) -> None:
        before = (await client.get(f"{API}/workspace/settings", headers=alpha.headers())).json()
        for method, path, body in (
            ("GET", "/workspace/billing", None),
            (
                "PATCH",
                "/workspace/settings",
                {"name": "Interviewer Was Here", "primary_color": "#000000"},
            ),
        ):
            response = await client.request(
                method, f"{API}{path}", headers=alpha.headers(INTERVIEWER), json=body
            )
            assert response.status_code == 403
        after = (await client.get(f"{API}/workspace/settings", headers=alpha.headers())).json()
        assert after["name"] == before["name"]
        assert after["primary_color"] == before["primary_color"]

    async def test_admin_can_manage_workspace_and_billing(
        self, client: httpx.AsyncClient, alpha: Workspace
    ) -> None:
        settings = await client.get(f"{API}/workspace/settings", headers=alpha.headers())
        assert settings.status_code == 200
        assert settings.json()["clerk_org_id"] == alpha.org_id

        billing = await client.get(f"{API}/workspace/billing", headers=alpha.headers())
        assert billing.status_code == 200
        assert billing.json() == {
            "plan": "trial",
            "status": "active",
            "seats_used": len(alpha.members),
        }

        patched = await client.patch(
            f"{API}/workspace/settings", headers=alpha.headers(), json={"primary_color": "#10b981"}
        )
        assert patched.status_code == 200 and patched.json()["primary_color"] == "#10B981"

    async def test_admin_cannot_self_assign_billing_plan(
        self, client: httpx.AsyncClient, alpha: Workspace
    ) -> None:
        response = await client.patch(
            f"{API}/workspace/settings",
            headers=alpha.headers(),
            json={"settings": {"billing": {"plan": "enterprise"}}},
        )
        assert response.status_code == 422
