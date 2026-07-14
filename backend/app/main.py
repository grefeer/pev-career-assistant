from __future__ import annotations

from contextlib import asynccontextmanager
import os
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import redis

from backend.app.api.router import api_router
from backend.app.config import Settings, get_settings
from src.graph import build_graph
from src.utils import load_env


load_env()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    if not hasattr(app.state, "graph"):
        app.state.graph = build_graph()
    redis_options = {}
    redis_password = os.environ.get("REDIS_PASSWORD")
    if redis_password:
        redis_options["password"] = redis_password
    app.state.redis = redis.Redis.from_url(app.state.settings.redis_url, **redis_options)
    try:
        yield
    finally:
        app.state.redis.close()


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or get_settings()
    app = FastAPI(
        title="Campus Recruitment Career Assistant API",
        version="2.0.0",
        lifespan=lifespan,
    )
    app.state.settings = resolved
    app.add_middleware(
        CORSMiddleware,
        allow_origins=resolved.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(api_router, prefix="/api")
    return app


app = create_app()
