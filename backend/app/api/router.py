"""The public API surface of the personal career assistant."""

from fastapi import APIRouter

from backend.app.api.routes import agent_runtime, auth, health, metrics, profiles


api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(metrics.router)
api_router.include_router(auth.router)
api_router.include_router(profiles.router)
api_router.include_router(agent_runtime.router)
