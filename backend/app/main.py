"""FastAPI application entrypoint: `uvicorn app.main:app --port 8001` (run from backend/)."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.database import AsyncSessionLocal, engine, verify_runtime_role_enforces_rls
from app.core.exceptions import ServiceError
from app.core.security import AuthenticationError, get_token_verifier
from app.services.communications_queue import close_communications_queue
from app.services.embedding_queue import close_embedding_queue
from app.services.extraction_queue import close_extraction_queue

logger = logging.getLogger("app")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    try:
        async with AsyncSessionLocal() as session:
            await verify_runtime_role_enforces_rls(session)
    except (OSError, SQLAlchemyError) as exc:
        logger.warning("Database unreachable at startup (%s); readiness checks will fail", exc)
    yield
    await close_extraction_queue()
    await close_embedding_queue()
    await close_communications_queue()
    await get_token_verifier().aclose()
    await engine.dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    logging.basicConfig(level=settings.LOG_LEVEL)

    app = FastAPI(
        title=settings.PROJECT_NAME,
        version="0.1.0",
        openapi_url=f"{settings.API_V1_PREFIX}/openapi.json",
        docs_url=f"{settings.API_V1_PREFIX}/docs",
        lifespan=lifespan,
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.ALLOWED_HOSTS)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(AuthenticationError)
    async def _authentication_error(_: Request, exc: AuthenticationError) -> JSONResponse:
        headers = {"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else None
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=headers)

    @app.exception_handler(ServiceError)
    async def _service_error(_: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    @app.get("/health", tags=["health"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready", tags=["health"])
    async def readiness() -> JSONResponse:
        try:
            async with AsyncSessionLocal() as session:
                await session.execute(text("SELECT 1"))
        except (OSError, SQLAlchemyError):
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return JSONResponse({"status": "ready"})

    app.include_router(api_router, prefix=settings.API_V1_PREFIX)
    return app


app = create_app()
