"""Candidate dossier for the split-screen review modal (project.md Module 4)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import CurrentUser, TenantDB
from app.schemas.dossier import CandidateDossier
from app.services.dossier_service import DossierService
from app.services.storage import StorageService, get_storage_service

router = APIRouter(tags=["candidates"])


@router.get("/applications/{application_id}/dossier", response_model=CandidateDossier)
async def get_dossier(
    application_id: uuid.UUID,
    user: CurrentUser,
    db: TenantDB,
    storage: Annotated[StorageService, Depends(get_storage_service)],
) -> CandidateDossier:
    """Parsed profile, match breakdown, expiring resume URL and stage history.

    Blind interviewers receive a redacted dossier: no name/contact details, graduation years,
    deterministic score or original resume (project.md §8.2).
    """
    return await DossierService(db, storage).get(application_id, viewer=user)
