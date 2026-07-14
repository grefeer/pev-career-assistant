from fastapi import APIRouter

from backend.app.api.routes import analysis, auth, health, sessions


api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(sessions.router)
api_router.include_router(analysis.router)
