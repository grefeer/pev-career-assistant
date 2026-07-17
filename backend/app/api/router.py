from fastapi import APIRouter

from backend.app.api.routes import (
    analysis,
    auth,
    devices,
    executor_tasks,
    health,
    job_feedback,
    job_submissions,
    jobs,
    profiles,
    sessions,
)


api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(sessions.router)
api_router.include_router(jobs.router)
api_router.include_router(job_submissions.router)
api_router.include_router(analysis.router)
api_router.include_router(profiles.router)
api_router.include_router(devices.router)
api_router.include_router(executor_tasks.router)
api_router.include_router(job_feedback.router)
