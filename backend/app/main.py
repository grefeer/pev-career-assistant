from __future__ import annotations

from contextlib import AsyncExitStack, asynccontextmanager
import os
from typing import Any, AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import boto3
from botocore.config import Config
import redis

from backend.app.api.router import api_router
from backend.app.config import Settings, get_settings
from backend.app.services.storage import S3BlobStore
from src.checkpointing import checkpointer_context
from src.graph import build_graph
from src.utils import load_env


load_env()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    owned_graph: object | None = None
    owned_redis: redis.Redis | None = None
    owned_object_store_client: Any | None = None
    try:
        async with AsyncExitStack() as stack:
            timeout = app.state.settings.readiness_timeout_seconds
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
                redis_options = {
                    "socket_connect_timeout": timeout,
                    "socket_timeout": timeout,
                }
                redis_password = os.environ.get("REDIS_PASSWORD")
                if redis_password:
                    redis_options["password"] = redis_password
                owned_redis = redis.Redis.from_url(
                    app.state.settings.redis_url, **redis_options
                )
                app.state.redis = owned_redis
                stack.callback(owned_redis.close)
            if not hasattr(app.state, "blob_store") and app.state.settings.app_env != "test":
                owned_object_store_client = boto3.client(
                    "s3",
                    endpoint_url=app.state.settings.object_store_endpoint,
                    region_name=app.state.settings.object_store_region,
                    aws_access_key_id=app.state.settings.object_store_access_key,
                    aws_secret_access_key=app.state.settings.object_store_secret_key,
                    config=Config(
                        connect_timeout=timeout,
                        read_timeout=timeout,
                        retries={"total_max_attempts": 2, "mode": "standard"},
                    ),
                )
                app.state.blob_store = S3BlobStore(
                    owned_object_store_client, app.state.settings.object_store_bucket
                )
                stack.callback(owned_object_store_client.close)
            if hasattr(app.state, "blob_store"):
                app.state.blob_store.ensure_bucket()
            yield
    finally:
        if (
            owned_graph is not None
            and getattr(app.state, "graph", None) is owned_graph
        ):
            del app.state.graph
        if owned_redis is not None and getattr(app.state, "redis", None) is owned_redis:
            del app.state.redis
        if (
            owned_object_store_client is not None
            and hasattr(app.state, "blob_store")
            and getattr(app.state.blob_store, "_client", None)
            is owned_object_store_client
        ):
            del app.state.blob_store


def create_app(
    settings: Settings | None = None,
    *,
    graph: object | None = None,
    checkpointer: object | None = None,
    blob_store: object | None = None,
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
    if blob_store is not None:
        app.state.blob_store = blob_store
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
