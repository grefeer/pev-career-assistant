from fastapi import APIRouter

from backend.app.api.routes import (
    application_snapshots,
    application_tracking,
    auth,
    company_research,
    devices,
    executor_tasks,
    health,
    interview_prep,
    job_discovery,
    job_feedback,
    job_submissions,
    jobs,
    matches,
    metrics,
    personalized_discovery,
    profiles,
    resume_drafts,
    sessions,
    site_adapters,
)


api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(metrics.router)
api_router.include_router(auth.router)
api_router.include_router(sessions.router)
api_router.include_router(jobs.router)
api_router.include_router(job_submissions.router)
api_router.include_router(profiles.router)
api_router.include_router(devices.router)
api_router.include_router(executor_tasks.router)
api_router.include_router(job_feedback.router)
api_router.include_router(matches.router)
api_router.include_router(resume_drafts.router)
api_router.include_router(application_snapshots.router)
api_router.include_router(job_discovery.router)
api_router.include_router(personalized_discovery.router)
api_router.include_router(site_adapters.router)
api_router.include_router(company_research.router)
api_router.include_router(interview_prep.router)
api_router.include_router(application_tracking.router)
