from __future__ import annotations

from contextlib import AsyncExitStack, asynccontextmanager
import os
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import redis

from backend.app.api.router import api_router
from backend.app.config import Settings, get_settings
from src.checkpointing import checkpointer_context
from src.graph import build_graph
from src.utils import load_env


load_env()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    owned_graph: object | None = None
    owned_redis: redis.Redis | None = None
    try:
        async with AsyncExitStack() as stack:
            if not hasattr(app.state, "graph"):
                if hasattr(app.state, "checkpointer"):
                    checkpointer = app.state.checkpointer
                else:
                    checkpointer = stack.enter_context(
                        checkpointer_context(app.state.settings)
                    )
                owned_graph = build_graph(checkpointer=checkpointer)
                app.state.graph = owned_graph
            if not hasattr(app.state, "redis"):
                redis_options = {}
                redis_password = os.environ.get("REDIS_PASSWORD")
                if redis_password:
                    redis_options["password"] = redis_password
                owned_redis = redis.Redis.from_url(
                    app.state.settings.redis_url, **redis_options
                )
                app.state.redis = owned_redis
            try:
                yield
            finally:
                if owned_redis is not None:
                    owned_redis.close()
    finally:
        if (
            owned_graph is not None
            and getattr(app.state, "graph", None) is owned_graph
        ):
            del app.state.graph
        if owned_redis is not None and getattr(app.state, "redis", None) is owned_redis:
            del app.state.redis


def create_app(
    settings: Settings | None = None,
    *,
    graph: object | None = None,
    checkpointer: object | None = None,
) -> FastAPI:
    resolved = settings or get_settings()
    app = FastAPI(
        title="Campus Recruitment Career Assistant API",
        version="2.0.0",
        lifespan=lifespan,
    )
    app.state.settings = resolved
    if graph is not None:
        app.state.graph = graph
    if checkpointer is not None:
        app.state.checkpointer = checkpointer
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
