"""Phase 1.1 — configuration loading (pydantic-settings). No database required."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import REPO_ROOT, Settings, get_settings


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Remove every settings variable from the environment so defaults are observable."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_defaults_match_platform_port_and_service_allocations(
    clean_env: pytest.MonkeyPatch,
) -> None:
    settings = Settings(_env_file=None)

    assert settings.ENVIRONMENT == "development"
    assert settings.API_PORT == 8001
    assert settings.FRONTEND_PORT == 3000
    assert settings.M3_EXTRACTOR_URL == "http://localhost:8000"
    assert (
        settings.DATABASE_ADMIN_URL
        == "postgresql+asyncpg://postgres:postgres@localhost:5432/ats_db"
    )
    assert settings.DATABASE_URL == "postgresql+asyncpg://ats_app:ats_app@localhost:5432/ats_db"
    assert settings.REDIS_URL == "redis://localhost:6379/0"
    assert settings.OLLAMA_URL == "http://localhost:11434"
    assert settings.OLLAMA_MODEL == "llama3.2:3b"
    assert settings.STORAGE_BACKEND == "local"
    assert settings.LOCAL_STORAGE_PATH == "./storage/uploads"
    assert settings.local_storage_dir == REPO_ROOT / "storage" / "uploads"
    assert settings.CORS_ORIGINS == ["http://localhost:3000"]
    assert "localhost" in settings.ALLOWED_HOSTS
    assert settings.CLERK_JWKS_URL is None
    assert settings.AUTH_MOCK_MODE is False


def test_environment_variables_override_defaults(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("REDIS_URL", "redis://cache.internal:6380/2")
    clean_env.setenv("API_PORT", "9001")
    clean_env.setenv("CLERK_JWKS_URL", "https://clerk.example.com/.well-known/jwks.json")
    clean_env.setenv("M3_EXTRACTOR_API_KEY", "sec_live_abc123")

    settings = Settings(_env_file=None)

    assert settings.REDIS_URL == "redis://cache.internal:6380/2"
    assert settings.API_PORT == 9001
    assert settings.CLERK_JWKS_URL == "https://clerk.example.com/.well-known/jwks.json"
    assert settings.M3_EXTRACTOR_API_KEY.get_secret_value() == "sec_live_abc123"
    assert "sec_live_abc123" not in repr(settings)  # secrets are masked


@pytest.mark.parametrize(
    "raw",
    [
        "https://a.example.com,https://b.example.com",
        '["https://a.example.com","https://b.example.com"]',
    ],
)
def test_list_settings_accept_csv_and_json(clean_env: pytest.MonkeyPatch, raw: str) -> None:
    clean_env.setenv("CORS_ORIGINS", raw)
    assert Settings(_env_file=None).CORS_ORIGINS == [
        "https://a.example.com",
        "https://b.example.com",
    ]


def test_env_file_is_loaded(clean_env: pytest.MonkeyPatch, tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("OLLAMA_MODEL=llama3.1:8b\nPLATFORM_ROOT_DOMAIN=ATS-Platform.com.\n")
    settings = Settings(_env_file=env_file)
    assert settings.OLLAMA_MODEL == "llama3.1:8b"
    assert settings.PLATFORM_ROOT_DOMAIN == "ats-platform.com"


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_mock_auth_is_forbidden_outside_dev_and_test(
    clean_env: pytest.MonkeyPatch, environment: str
) -> None:
    with pytest.raises(ValidationError, match="AUTH_MOCK_MODE"):
        Settings(
            _env_file=None,
            ENVIRONMENT=environment,
            AUTH_MOCK_MODE=True,
            CLERK_JWKS_URL="https://clerk.example.com/.well-known/jwks.json",
            CLERK_ISSUER="https://clerk.example.com",
        )


def test_production_requires_clerk_configuration(clean_env: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError, match="CLERK_JWKS_URL"):
        Settings(_env_file=None, ENVIRONMENT="production")
    production = {
        "ENVIRONMENT": "production",
        "CLERK_JWKS_URL": "https://clerk.example.com/.well-known/jwks.json",
        "CLERK_ISSUER": "https://clerk.example.com",
        "APP_SECRET_KEY": "a-real-production-secret",
        "STORAGE_BACKEND": "r2",
        "S3_BUCKET": "ats-resumes",
        "S3_ENDPOINT_URL": "https://account.r2.cloudflarestorage.com",
        "M3_WEBHOOK_SECRET": "whsec_production",
    }
    assert Settings(_env_file=None, **production).is_production
    for override, message in (
        ({"APP_SECRET_KEY": Settings.model_fields["APP_SECRET_KEY"].default}, "APP_SECRET_KEY"),
        ({"STORAGE_BACKEND": "local"}, "STORAGE_BACKEND=local"),
        ({"S3_BUCKET": None}, "S3_BUCKET"),
        ({"M3_WEBHOOK_SECRET": ""}, "M3_WEBHOOK_SECRET"),
    ):
        with pytest.raises(ValidationError, match=message):
            Settings(_env_file=None, **{**production, **override})


def test_test_suite_runs_with_mock_auth_against_test_database() -> None:
    settings = get_settings()
    assert settings is get_settings()  # cached singleton
    assert settings.ENVIRONMENT == "test"
    assert settings.AUTH_MOCK_MODE is True
    assert settings.DATABASE_URL.endswith("/ats_test")
