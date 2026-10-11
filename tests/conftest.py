"""Shared fixtures for headless API tests (pytest + httpx.AsyncClient). No browser testing.

Requires PostgreSQL (with pgvector) reachable at TEST_DATABASE_ADMIN_URL. The test database is
dropped and recreated for every run and migrated with the real Alembic CLI; the app connects as
the restricted runtime role so Row-Level Security is genuinely exercised.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import asyncpg
import httpx
import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"

TEST_DATABASE_ADMIN_URL = os.environ.get(
    "TEST_DATABASE_ADMIN_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/ats_test"
)
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://ats_app:ats_app@localhost:5432/ats_test"
)

# Settings are read at import time, so configure the test environment before importing the app.
os.environ.update(
    {
        "ENVIRONMENT": "test",
        "AUTH_MOCK_MODE": "true",
        "MOCK_JWT_SECRET": "test-only-mock-secret-0123456789abcdef",
        "DATABASE_URL": TEST_DATABASE_URL,
        "DATABASE_ADMIN_URL": TEST_DATABASE_ADMIN_URL,
        "PLATFORM_ROOT_DOMAIN": "localhost",
        "ALLOWED_HOSTS": "test,localhost,*.localhost,*.example.com",
        "CORS_ORIGINS": "http://localhost:3000",
        # Local storage driver in an isolated temp dir; pre-signed URLs target the test client.
        "STORAGE_BACKEND": "local",
        "LOCAL_STORAGE_PATH": tempfile.mkdtemp(prefix="ats-test-uploads-"),
        "API_PUBLIC_URL": "http://test",
        # Phase 3: M3 extractor contract, in-process event fan-out, isolated Redis DB for ARQ.
        "M3_EXTRACTOR_URL": "http://m3.test:8000",
        "M3_EXTRACTOR_API_KEY": "sec_test_m3_key",
        "M3_WEBHOOK_SECRET": "whsec_test_secret",
        "EVENT_BROKER_BACKEND": "memory",
        "REDIS_URL": os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15"),
    }
)


@pytest.fixture(scope="session", autouse=True)
async def database() -> AsyncIterator[None]:
    """Drop/recreate the test database and apply all migrations."""
    admin = make_url(TEST_DATABASE_ADMIN_URL)
    db_name = admin.database or ""
    if not db_name.endswith("_test"):
        pytest.exit(f"Refusing to recreate non-test database {db_name!r}", returncode=2)
    try:
        conn = await asyncpg.connect(
            host=admin.host,
            port=admin.port,
            user=admin.username,
            password=admin.password,
            database="postgres",
        )
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.exit(
            f"PostgreSQL is not reachable for tests ({exc}). Start it with "
            "`docker compose up -d postgres` or `scripts/local_postgres.sh start`.",
            returncode=2,
        )
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{db_name}"')
    finally:
        await conn.close()

    migration = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND_DIR,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
    )
    if migration.returncode != 0:
        pytest.exit(f"Alembic migration failed:\n{migration.stderr}", returncode=2)
    yield


@pytest.fixture(scope="session")
def extraction_queue() -> object:
    from tests.helpers import RecordingExtractionQueue

    return RecordingExtractionQueue()


@pytest.fixture(scope="session")
def embedding_queue() -> object:
    from tests.helpers import RecordingEmbeddingQueue

    return RecordingEmbeddingQueue()


def _provider(value: object) -> Callable[[], object]:
    return lambda: value


@pytest.fixture(scope="session")
def communications_queue() -> object:
    from tests.helpers import RecordingCommunicationsQueue

    return RecordingCommunicationsQueue()


@pytest.fixture(scope="session")
async def api_app(
    database: None,
    extraction_queue: object,
    embedding_queue: object,
    communications_queue: object,
) -> AsyncIterator[object]:
    from app.core.database import engine
    from app.main import app
    from app.services.communications_queue import get_communications_queue
    from app.services.embedding_queue import get_embedding_queue
    from app.services.extraction_queue import get_extraction_queue

    overrides = {
        get_extraction_queue: extraction_queue,
        get_embedding_queue: embedding_queue,
        get_communications_queue: communications_queue,
    }
    for dependency, fake in overrides.items():
        # Zero-argument provider: FastAPI would treat a lambda parameter as a query argument.
        app.dependency_overrides[dependency] = _provider(fake)
    yield app
    for dependency in overrides:
        app.dependency_overrides.pop(dependency, None)
    await engine.dispose()


@pytest.fixture
async def client(api_app: object) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=api_app)  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


@pytest.fixture(scope="session")
async def admin_engine(database: None) -> AsyncIterator[AsyncEngine]:
    """Superuser engine for asserting ground truth underneath RLS. Never used by the app."""
    engine = create_async_engine(TEST_DATABASE_ADMIN_URL)
    yield engine
    await engine.dispose()
