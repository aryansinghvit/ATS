"""Clerk session-token verification (RS256 via JWKS) with an offline mock mode for tests.

Claim mapping
-------------
* ``sub``                      -> Clerk user id
* ``org_id``  / ``o.id``       -> Clerk organization id, mapped to a tenant
* ``org_role`` / ``o.rol``     -> organization role, mapped to :class:`UserRole`
* ``org_slug`` / ``o.slg``     -> suggested workspace subdomain

Both Clerk session-token shapes are accepted: v1 (flat ``org_*`` claims) and v2 (nested ``o``).

The accepted algorithm is pinned by mode (RS256 for Clerk, HS256 for mock) and never taken from
the token header, which prevents algorithm-confusion attacks.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import httpx
from jose import ExpiredSignatureError, JWTError, jwt
from jose.exceptions import JWTClaimsError

from app.core.config import Settings, get_settings
from app.models.user import UserRole

CLERK_ALGORITHM = "RS256"
MOCK_ALGORITHM = "HS256"

_ROLE_MAP: dict[str, UserRole] = {
    "admin": UserRole.ADMIN,
    "recruiter": UserRole.RECRUITER,
    "hiring_manager": UserRole.HIRING_MANAGER,
    "interviewer": UserRole.INTERVIEWER,
    # Clerk's built-in non-admin role gets the least-privileged ATS role.
    "member": UserRole.INTERVIEWER,
}

_REQUIRED_CLAIMS = {"require_exp": True, "require_iat": True, "require_sub": True}


class AuthenticationError(Exception):
    def __init__(self, detail: str, status_code: int = 401) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class AuthClaims:
    user_id: str
    org_id: str
    org_role: str
    role: UserRole
    org_slug: str | None = None
    email: str | None = None
    session_id: str | None = None


def map_org_role(org_role: str) -> UserRole:
    try:
        return _ROLE_MAP[org_role.removeprefix("org:")]
    except KeyError:
        raise AuthenticationError(f"Unrecognised organization role '{org_role}'", 403) from None


def extract_claims(payload: dict[str, Any]) -> AuthClaims:
    subject = payload.get("sub")
    if not isinstance(subject, str) or not subject:
        raise AuthenticationError("Token is missing the 'sub' claim")

    nested = payload.get("o")
    if isinstance(nested, dict):  # Clerk session token v2
        org_id, org_role, org_slug = nested.get("id"), nested.get("rol"), nested.get("slg")
    else:  # Clerk session token v1
        org_id, org_role = payload.get("org_id"), payload.get("org_role")
        org_slug = payload.get("org_slug")

    if not isinstance(org_id, str) or not org_id or not isinstance(org_role, str) or not org_role:
        raise AuthenticationError(
            "Token has no active organization; select a workspace and retry", 403
        )

    email = payload.get("email")
    return AuthClaims(
        user_id=subject,
        org_id=org_id,
        org_role=org_role,
        role=map_org_role(org_role),
        org_slug=org_slug if isinstance(org_slug, str) and org_slug else None,
        email=email if isinstance(email, str) and email else None,
        session_id=payload.get("sid"),
    )


class JWKSClient:
    """Caches Clerk's JWKS and refetches on unknown ``kid`` (key rotation), rate-limited."""

    def __init__(
        self,
        jwks_url: str,
        *,
        cache_ttl_seconds: int = 3600,
        min_refresh_interval_seconds: float = 30.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._jwks_url = jwks_url
        self._cache_ttl = cache_ttl_seconds
        self._min_refresh_interval = min_refresh_interval_seconds
        self._http = http_client
        self._owns_http = http_client is None
        self._keys: dict[str, dict[str, Any]] = {}
        self._fetched_at = 0.0
        self._lock = asyncio.Lock()

    async def get_signing_key(self, kid: str) -> dict[str, Any]:
        keys = await self._get_keys(force_refresh=False)
        if kid not in keys:
            keys = await self._get_keys(force_refresh=True)
        try:
            return keys[kid]
        except KeyError:
            raise AuthenticationError("Token signed with an unknown key") from None

    async def _get_keys(self, *, force_refresh: bool) -> dict[str, dict[str, Any]]:
        async with self._lock:
            age = time.monotonic() - self._fetched_at
            fresh = bool(self._keys) and age < self._cache_ttl
            if fresh and (not force_refresh or age < self._min_refresh_interval):
                return self._keys
            if self._http is None:
                self._http = httpx.AsyncClient(timeout=5.0)
            try:
                response = await self._http.get(self._jwks_url)
                response.raise_for_status()
                document = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                if self._keys:  # serve stale keys rather than lock everyone out
                    return self._keys
                raise AuthenticationError("Unable to fetch signing keys", 503) from exc
            self._keys = {
                key["kid"]: key
                for key in document.get("keys", [])
                if key.get("kid") and key.get("kty") == "RSA"
            }
            self._fetched_at = time.monotonic()
            return self._keys

    async def aclose(self) -> None:
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None


class TokenVerifier:
    def __init__(self, settings: Settings, jwks_client: JWKSClient | None = None) -> None:
        self._settings = settings
        self._jwks = jwks_client
        if self._jwks is None and settings.CLERK_JWKS_URL and not settings.AUTH_MOCK_MODE:
            self._jwks = JWKSClient(
                settings.CLERK_JWKS_URL, cache_ttl_seconds=settings.CLERK_JWKS_CACHE_TTL_SECONDS
            )

    async def verify(self, token: str) -> AuthClaims:
        try:
            header = jwt.get_unverified_header(token)
        except JWTError:
            raise AuthenticationError("Malformed token") from None

        if self._settings.AUTH_MOCK_MODE:
            payload = self._decode(
                token,
                header,
                key=self._settings.MOCK_JWT_SECRET.get_secret_value(),
                algorithm=MOCK_ALGORITHM,
                issuer=self._settings.MOCK_JWT_ISSUER,
            )
        else:
            payload = await self._decode_clerk(token, header)
        return extract_claims(payload)

    async def _decode_clerk(self, token: str, header: dict[str, Any]) -> dict[str, Any]:
        if self._jwks is None or not self._settings.CLERK_ISSUER:
            raise AuthenticationError("Authentication provider is not configured", 503)
        kid = header.get("kid")
        if header.get("alg") != CLERK_ALGORITHM or not kid:
            raise AuthenticationError("Unsupported token algorithm")
        key = await self._jwks.get_signing_key(kid)
        payload = self._decode(
            token, header, key=key, algorithm=CLERK_ALGORITHM, issuer=self._settings.CLERK_ISSUER
        )
        authorized_parties = self._settings.CLERK_AUTHORIZED_PARTIES
        azp = payload.get("azp")
        if authorized_parties and azp and azp not in authorized_parties:
            raise AuthenticationError("Token was issued for an unauthorized party")
        return payload

    def _decode(
        self,
        token: str,
        header: dict[str, Any],
        *,
        key: str | dict[str, Any],
        algorithm: str,
        issuer: str,
    ) -> dict[str, Any]:
        if header.get("alg") != algorithm:
            raise AuthenticationError("Unsupported token algorithm")
        try:
            return jwt.decode(
                token,
                key,
                algorithms=[algorithm],
                issuer=issuer,
                options={
                    "verify_aud": False,
                    "leeway": self._settings.JWT_LEEWAY_SECONDS,
                    **_REQUIRED_CLAIMS,
                },
            )
        except ExpiredSignatureError:
            raise AuthenticationError("Token has expired") from None
        except JWTClaimsError:
            raise AuthenticationError("Token claims are invalid") from None
        except JWTError:
            raise AuthenticationError("Token signature is invalid") from None

    async def aclose(self) -> None:
        if self._jwks is not None:
            await self._jwks.aclose()


@lru_cache
def get_token_verifier() -> TokenVerifier:
    return TokenVerifier(get_settings())


def create_mock_token(
    *,
    user_id: str,
    org_id: str,
    org_role: str,
    org_slug: str | None = None,
    email: str | None = None,
    expires_in: int = 3600,
    token_version: int = 1,
    settings: Settings | None = None,
) -> str:
    """Mint a Clerk-shaped HS256 token. Only works when AUTH_MOCK_MODE is enabled."""
    settings = settings or get_settings()
    if not settings.AUTH_MOCK_MODE:
        raise RuntimeError("Mock tokens can only be minted when AUTH_MOCK_MODE is enabled")
    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": user_id,
        "iss": settings.MOCK_JWT_ISSUER,
        "iat": now,
        "nbf": now,
        "exp": now + expires_in,
        "sid": f"sess_mock_{user_id}",
    }
    if token_version == 2:
        claims["v"] = 2
        claims["o"] = {"id": org_id, "rol": org_role.removeprefix("org:"), "slg": org_slug}
    else:
        claims.update(org_id=org_id, org_role=org_role, org_slug=org_slug)
    if email:
        claims["email"] = email
    return jwt.encode(claims, settings.MOCK_JWT_SECRET.get_secret_value(), algorithm=MOCK_ALGORITHM)
