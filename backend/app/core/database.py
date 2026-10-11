"""Async database engine, sessions and per-transaction tenant context (RLS).

Tenant isolation contract
-------------------------
Every RLS policy evaluates::

    tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid

The tenant is bound with ``set_config('app.current_tenant_id', :tenant_id, true)``, the
parameterisable form of ``SET LOCAL app.current_tenant_id = ...`` (``SET`` itself cannot take
bind parameters). It is transaction-scoped, so a pooled connection never carries a tenant into
the next request, and a session with no tenant bound sees zero tenant rows (fail-closed).

The tenant is stored on ``session.info`` and re-applied by an ``after_begin`` hook, so it
survives ``commit()`` — each new transaction gets ``SET LOCAL`` again before its first query.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import Connection, event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, SessionTransaction

from app.core.config import get_settings
from app.core.rls import TENANT_SETTING

_TENANT_INFO_KEY = "tenant_id"
_SET_TENANT_SQL = text(f"SELECT set_config('{TENANT_SETTING}', :tenant_id, true)")


class TenantScopedSession(Session):
    """Sync session class backing AsyncSession; carries the tenant-context hook."""


@event.listens_for(TenantScopedSession, "after_begin")
def _apply_tenant_context(
    session: Session, transaction: SessionTransaction, connection: Connection
) -> None:
    tenant_id = session.info.get(_TENANT_INFO_KEY)
    if tenant_id is not None:
        connection.execute(_SET_TENANT_SQL, {"tenant_id": str(tenant_id)})


def build_engine(database_url: str | None = None) -> AsyncEngine:
    settings = get_settings()
    return create_async_engine(
        database_url or settings.DATABASE_URL,
        pool_size=settings.DB_POOL_SIZE,
        max_overflow=settings.DB_MAX_OVERFLOW,
        pool_timeout=settings.DB_POOL_TIMEOUT,
        pool_recycle=settings.DB_POOL_RECYCLE,
        pool_pre_ping=True,
        echo=settings.DB_ECHO,
    )


engine: AsyncEngine = build_engine()

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    sync_session_class=TenantScopedSession,
    expire_on_commit=False,
    autoflush=False,
)


async def set_tenant_context(session: AsyncSession, tenant_id: uuid.UUID) -> None:
    """Bind ``tenant_id`` to the session for the current and all subsequent transactions."""
    session.info[_TENANT_INFO_KEY] = tenant_id
    if session.in_transaction():
        await session.execute(_SET_TENANT_SQL, {"tenant_id": str(tenant_id)})


def get_tenant_context(session: AsyncSession) -> uuid.UUID | None:
    return session.info.get(_TENANT_INFO_KEY)


async def get_session() -> AsyncIterator[AsyncSession]:
    """Request-scoped session with no tenant bound yet (FastAPI caches it per request)."""
    async with AsyncSessionLocal() as session:
        yield session


async def get_db(
    request: Request, session: Annotated[AsyncSession, Depends(get_session)]
) -> AsyncIterator[AsyncSession]:
    """Tenant-scoped session.

    Reads the tenant resolved for this request from ``request.state.tenant_id`` (set by the auth
    dependency from the JWT ``org_id`` claim, or by the public host resolver) and executes
    ``SET LOCAL app.current_tenant_id`` before any query runs.
    """
    tenant_id: uuid.UUID | None = getattr(request.state, "tenant_id", None)
    if tenant_id is not None:
        await set_tenant_context(session, tenant_id)
    yield session


async def verify_runtime_role_enforces_rls(session: AsyncSession) -> None:
    """Refuse to run on a DB role that would silently bypass Row-Level Security.

    Superusers and BYPASSRLS roles skip RLS entirely, and table owners skip it unless the table
    uses FORCE ROW LEVEL SECURITY. The runtime role must be none of these.
    """
    row = (
        await session.execute(
            text(
                """
                SELECT r.rolname, r.rolsuper, r.rolbypassrls,
                       (SELECT count(*) FROM pg_tables t
                         WHERE t.schemaname = 'public' AND t.tableowner = r.rolname) AS owned
                FROM pg_roles r WHERE r.rolname = current_user
                """
            )
        )
    ).one()
    problems = []
    if row.rolsuper:
        problems.append("is a superuser")
    if row.rolbypassrls:
        problems.append("has BYPASSRLS")
    if row.owned:
        problems.append(f"owns {row.owned} table(s) in schema public")
    if problems:
        raise RuntimeError(
            f"Database role '{row.rolname}' {', '.join(problems)}; Row-Level Security would be "
            "bypassed. Point DATABASE_URL at the restricted application role (default: ats_app)."
        )
