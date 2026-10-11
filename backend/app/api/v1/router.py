from fastapi import APIRouter

from app.api.v1.endpoints import (
    admin,
    applications,
    auth,
    candidates,
    collaboration,
    communications,
    jobs,
    pipeline,
    public,
    screenings,
    storage,
    talent_pool,
    users,
    webhooks,
    workspace,
)

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(workspace.router)
api_router.include_router(jobs.router)
api_router.include_router(jobs.public_router)
api_router.include_router(applications.router)
api_router.include_router(pipeline.router)
api_router.include_router(candidates.router)
api_router.include_router(talent_pool.router)
api_router.include_router(collaboration.router)
api_router.include_router(screenings.router)
api_router.include_router(screenings.public_router)
api_router.include_router(admin.router)
api_router.include_router(communications.router)
api_router.include_router(webhooks.router)
api_router.include_router(storage.router)
api_router.include_router(public.router)
