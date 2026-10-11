"""Centralised application configuration (pydantic-settings).

Values are resolved in priority order: process environment > backend/.env > <repo>/.env > defaults.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_DIR.parent

Environment = Literal["development", "test", "staging", "production"]

_DEV_SECRET_KEY = "insecure-dev-secret-key-change-me-0123456789"

# Environments in which offline mock-token authentication may be enabled.
MOCK_AUTH_ENVIRONMENTS: frozenset[str] = frozenset({"development", "test"})

CommaList = Annotated[list[str], NoDecode]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env", BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # ------------------------------------------------------------------ general
    PROJECT_NAME: str = "ATS SaaS Platform"
    ENVIRONMENT: Environment = "development"
    API_V1_PREFIX: str = "/api/v1"
    LOG_LEVEL: str = "INFO"
    # HMAC key for signed local-storage URLs and candidate application status tokens.
    APP_SECRET_KEY: SecretStr = SecretStr(_DEV_SECRET_KEY)

    # --------------------------------------------------------- port allocations
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8001
    # Externally reachable base URL of this API (used to build local-storage upload URLs).
    API_PUBLIC_URL: str = "http://localhost:8001"
    FRONTEND_PORT: int = 3000
    FRONTEND_URL: str = "http://localhost:3000"

    # ----------------------------------------------------------------- database
    # Runtime connection. MUST be a role that is not a superuser, does not have BYPASSRLS and
    # does not own the tables — otherwise PostgreSQL silently skips Row-Level Security.
    DATABASE_URL: str = "postgresql+asyncpg://ats_app:ats_app@localhost:5432/ats_db"
    # Owner/superuser connection used only by Alembic migrations (DDL, RLS policies, grants).
    DATABASE_ADMIN_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/ats_db"
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    DB_POOL_TIMEOUT: int = 30
    DB_POOL_RECYCLE: int = 1800
    DB_ECHO: bool = False

    # -------------------------------------------------------------------- redis
    REDIS_URL: str = "redis://localhost:6379/0"
    # Real-time fan-out for Kanban SSE: "redis" (multi-process) or "memory" (single process/tests).
    EVENT_BROKER_BACKEND: Literal["redis", "memory"] = "redis"
    PIPELINE_STREAM_MAX_SECONDS: float = 3600.0
    PIPELINE_STREAM_KEEPALIVE_SECONDS: float = 15.0

    # ------------------------------------------------- M3 resume extractor (ext)
    M3_EXTRACTOR_URL: str = "http://localhost:8000"
    M3_EXTRACTOR_API_KEY: SecretStr = SecretStr("")
    M3_WEBHOOK_SECRET: SecretStr = SecretStr("")
    M3_REQUEST_TIMEOUT_SECONDS: float = 30.0
    # Callback URL handed to M3; defaults to {API_PUBLIC_URL}{API_V1_PREFIX}/webhooks/extractor.
    M3_WEBHOOK_URL: str | None = None
    M3_HEALTH_PATH: str = "/health"
    # Outbound dispatch concurrency; matches the extractor's asyncio.Semaphore(1) GPU lock.
    M3_MAX_CONCURRENCY: int = 1
    # Backoff between attempts on 503 / timeout (project.md §6.4: 3 retries at 5s, 15s, 45s).
    M3_RETRY_DELAYS_SECONDS: CommaList = Field(default_factory=lambda: ["5", "15", "45"])
    # Applications still pending this long without a tracking id are re-queued by the sweeper.
    EXTRACTION_SWEEP_STALE_SECONDS: int = 600

    # -------------------------------------------------------------------- clerk
    CLERK_JWKS_URL: str | None = None
    CLERK_ISSUER: str | None = None
    # Allowed `azp` (authorized party) origins for Clerk session tokens; empty = skip check.
    CLERK_AUTHORIZED_PARTIES: CommaList = Field(default_factory=list)
    CLERK_JWKS_CACHE_TTL_SECONDS: int = 3600
    JWT_LEEWAY_SECONDS: int = 5

    # Offline mock-token verification for headless Python test scripts (HS256).
    AUTH_MOCK_MODE: bool = False
    MOCK_JWT_SECRET: SecretStr = SecretStr("insecure-mock-secret-change-me-0123456789")
    MOCK_JWT_ISSUER: str = "https://mock.clerk.local"

    # ------------------------------------------------------------ local ollama
    OLLAMA_URL: str = "http://localhost:11434"
    OLLAMA_MODEL: str = "llama3.2:3b"
    OLLAMA_TIMEOUT_SECONDS: float = 120.0

    # ------------------------------------------------ semantic talent pool (RAG)
    EMBEDDING_MODEL: str = "BAAI/bge-small-en-v1.5"
    EMBEDDING_DIM: int = 384
    EMBEDDING_CACHE_DIR: str = "./.cache/fastembed"
    # Optional folder with a manually downloaded ONNX model (skips the Hugging Face download).
    EMBEDDING_MODEL_PATH: str | None = None
    EMBEDDING_THREADS: int | None = None
    # bge models expect this instruction in front of search queries (not documents).
    EMBEDDING_QUERY_INSTRUCTION: str = "Represent this sentence for searching relevant passages: "
    # HNSW candidate list size per query; higher = better recall under RLS post-filtering.
    TALENT_POOL_EF_SEARCH: int = 200
    # Job-publish resurfacing: minimum cosine similarity and max suggestions per job.
    RESURFACING_THRESHOLD: float = 0.70
    RESURFACING_LIMIT: int = 20

    # ------------------------------------------------------- AI pre-screening
    SCREENING_TOKEN_TTL_HOURS: int = 72
    SCREENING_MAX_AUDIO_BYTES: int = 10 * 1024 * 1024
    SCREENING_MAX_AUDIO_SECONDS: int = 90
    SCREENING_MAX_TEXT_CHARS: int = 4000
    # Local speech-to-text (faster-whisper / CTranslate2) for audio answers.
    WHISPER_MODEL: str = "base.en"
    WHISPER_CACHE_DIR: str = "./.cache/whisper"
    WHISPER_MODEL_PATH: str | None = None

    # ---------------------------------------------------- candidate communications
    # "log" records emails in the outbox and logs them (dev/test); "smtp" delivers via SMTP.
    EMAIL_BACKEND: Literal["log", "smtp"] = "log"
    EMAIL_FROM: str = "ATS Recruiting <no-reply@localhost>"
    SMTP_HOST: str | None = None
    SMTP_PORT: int = 587
    SMTP_USERNAME: str | None = None
    SMTP_PASSWORD: SecretStr | None = None
    SMTP_STARTTLS: bool = True
    EMAIL_MAX_ATTEMPTS: int = 3
    DEFAULT_REJECTION_DELAY_HOURS: int = 48

    # ------------------------------------------------------------------ storage
    STORAGE_BACKEND: Literal["local", "s3", "r2"] = "local"
    LOCAL_STORAGE_PATH: str = "./storage/uploads"
    S3_BUCKET: str | None = None
    S3_REGION: str | None = None
    S3_ENDPOINT_URL: str | None = None  # Cloudflare R2 / MinIO endpoint
    S3_ACCESS_KEY_ID: SecretStr | None = None
    S3_SECRET_ACCESS_KEY: SecretStr | None = None
    PRESIGNED_URL_TTL_SECONDS: int = 900

    # ------------------------------------------------------- candidate ingestion
    MAX_RESUME_SIZE_BYTES: int = 15 * 1024 * 1024
    APPLICATION_STATUS_TOKEN_TTL_SECONDS: int = 24 * 3600
    APPLICATION_EVENTS_POLL_SECONDS: float = 2.0
    APPLICATION_EVENTS_MAX_SECONDS: float = 300.0

    # ------------------------------------------------------- http / multi-tenant
    CORS_ORIGINS: CommaList = Field(default_factory=lambda: ["http://localhost:3000"])
    ALLOWED_HOSTS: CommaList = Field(
        default_factory=lambda: ["localhost", "127.0.0.1", "*.localhost"]
    )
    # Root domain for tenant subdomains, e.g. "ats-platform.com" -> acme.ats-platform.com.
    PLATFORM_ROOT_DOMAIN: str = "localhost"
    # Subdomains that belong to the platform itself and never identify a tenant.
    RESERVED_SUBDOMAINS: CommaList = Field(
        default_factory=lambda: ["www", "app", "api", "admin", "auth", "domains", "static", "cdn"]
    )

    @field_validator(
        "CLERK_AUTHORIZED_PARTIES",
        "M3_RETRY_DELAYS_SECONDS",
        "CORS_ORIGINS",
        "ALLOWED_HOSTS",
        "RESERVED_SUBDOMAINS",
        mode="before",
    )
    @classmethod
    def _parse_list(cls, value: Any) -> Any:
        """Accept JSON arrays (`["a","b"]`) or comma-separated strings (`a,b`)."""
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return []
            if stripped.startswith("["):
                return json.loads(stripped)
            return [item.strip() for item in stripped.split(",") if item.strip()]
        return value

    @field_validator("PLATFORM_ROOT_DOMAIN")
    @classmethod
    def _normalise_root_domain(cls, value: str) -> str:
        return value.strip().lower().rstrip(".")

    @field_validator("RESERVED_SUBDOMAINS")
    @classmethod
    def _normalise_reserved(cls, value: list[str]) -> list[str]:
        return [item.lower() for item in value]

    @model_validator(mode="after")
    def _guard_configuration(self) -> Settings:
        if self.AUTH_MOCK_MODE and self.ENVIRONMENT not in MOCK_AUTH_ENVIRONMENTS:
            raise ValueError(
                f"AUTH_MOCK_MODE is only permitted when ENVIRONMENT is one of "
                f"{sorted(MOCK_AUTH_ENVIRONMENTS)} (got {self.ENVIRONMENT!r})."
            )
        if self.STORAGE_BACKEND in ("s3", "r2") and not self.S3_BUCKET:
            raise ValueError(f"S3_BUCKET is required when STORAGE_BACKEND={self.STORAGE_BACKEND}.")
        if self.STORAGE_BACKEND == "r2" and not self.S3_ENDPOINT_URL:
            raise ValueError("S3_ENDPOINT_URL is required when STORAGE_BACKEND=r2.")
        if self.ENVIRONMENT == "production":
            if not (self.CLERK_JWKS_URL and self.CLERK_ISSUER):
                raise ValueError("CLERK_JWKS_URL and CLERK_ISSUER are required in production.")
            if self.APP_SECRET_KEY.get_secret_value() == _DEV_SECRET_KEY:
                raise ValueError("APP_SECRET_KEY must be changed from the development default.")
            if self.STORAGE_BACKEND == "local":
                raise ValueError("STORAGE_BACKEND=local is for development/test only.")
            if not self.M3_WEBHOOK_SECRET.get_secret_value():
                raise ValueError("M3_WEBHOOK_SECRET is required in production.")
        if self.EMAIL_BACKEND == "smtp" and not self.SMTP_HOST:
            raise ValueError("SMTP_HOST is required when EMAIL_BACKEND=smtp.")
        return self

    @property
    def m3_webhook_url(self) -> str:
        return self.M3_WEBHOOK_URL or (
            f"{self.API_PUBLIC_URL.rstrip('/')}{self.API_V1_PREFIX}/webhooks/extractor"
        )

    @property
    def m3_retry_delays(self) -> list[int]:
        return [int(delay) for delay in self.M3_RETRY_DELAYS_SECONDS]

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"

    @property
    def embedding_cache_dir(self) -> Path:
        path = Path(self.EMBEDDING_CACHE_DIR)
        return path if path.is_absolute() else (REPO_ROOT / path).resolve()

    @property
    def local_storage_dir(self) -> Path:
        path = Path(self.LOCAL_STORAGE_PATH)
        return path if path.is_absolute() else (REPO_ROOT / path).resolve()


@lru_cache
def get_settings() -> Settings:
    return Settings()
