from __future__ import annotations

from contextlib import AsyncExitStack, asynccontextmanager
import logging
import os
from typing import Any, AsyncIterator

import boto3
from botocore.config import Config
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import redis
from sqlalchemy.orm import sessionmaker

from backend.app.api.router import api_router
from backend.app.config import Settings, get_settings
from backend.app.middleware import CorrelationIdMiddleware
from backend.app.services.storage import EncryptedObjectStore, S3BlobStore
from backend.app.services.agent_runtime.provider_config import load_project_env


load_project_env()


logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Own infrastructure and the sole production PEV runtime for one app lifetime."""
    owned_redis: redis.Redis | None = None
    owned_object_store_client: Any | None = None
    owned_session_factory: Any | None = None
    owned_agent_runtime: object | None = None
    owned_agent_run_service: object | None = None
    try:
        async with AsyncExitStack() as stack:
            timeout = app.state.settings.readiness_timeout_seconds
            if not hasattr(app.state, "redis"):
                redis_options: dict[str, object] = {
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
            if not hasattr(app.state, "session_factory"):
                from backend.app.db.session import build_engine

                readiness_engine = build_engine(app.state.settings)
                stack.callback(readiness_engine.dispose)
                owned_session_factory = sessionmaker(
                    bind=readiness_engine, autoflush=False, expire_on_commit=False
                )
                app.state.session_factory = owned_session_factory
            if (
                not hasattr(app.state, "blob_store")
                and app.state.settings.app_env != "test"
            ):
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
                    owned_object_store_client,
                    app.state.settings.object_store_bucket,
                    region=app.state.settings.object_store_region,
                )
                stack.callback(owned_object_store_client.close)
            if hasattr(app.state, "blob_store"):
                app.state.blob_store.ensure_bucket()
                app.state.object_store = EncryptedObjectStore(
                    app.state.blob_store, app.state.settings.object_encryption_key
                )
            if not hasattr(app.state, "agent_run_service"):
                from backend.app.services.agent_runtime.executor_agent import ExecutorAgent
                from backend.app.services.agent_runtime.model_gateway import (
                    AgentModelGatewayConfigError,
                    build_agent_model_gateway,
                )
                from backend.app.services.agent_runtime.planner_agent import PlannerAgent
                from backend.app.services.agent_runtime.runtime import AgentRuntime
                from backend.app.services.agent_runtime.service import AgentRunService
                from backend.app.services.agent_runtime.verifier_agent import VerifierAgent
                from backend.app.services.career_skills.registry import (
                    build_career_tool_registry,
                )
                from backend.app.services.career_skills.manifest import (
                    build_career_skill_registry,
                )

                runtime = getattr(app.state, "agent_runtime", None)
                if not hasattr(app.state, "agent_runtime"):
                    app.state.agent_runtime = None
                if runtime is None and app.state.settings.agent_harness_enabled:
                    try:
                        gateway = build_agent_model_gateway(app.state.settings)
                        tools = build_career_tool_registry()
                        skills = build_career_skill_registry(tools)
                        runtime = AgentRuntime(
                            planner=PlannerAgent(gateway=gateway, tools=tools, skills=skills),
                            executor=ExecutorAgent(gateway=gateway, tools=tools, skills=skills),
                            verifier=VerifierAgent(gateway=gateway, tools=tools, skills=skills),
                            agent_version="pev-1",
                            skills=skills,
                        )
                        app.state.agent_runtime = runtime
                        owned_agent_runtime = runtime
                    except AgentModelGatewayConfigError:
                        logger.warning("adaptive PEV runtime unavailable: model key missing")
                owned_agent_run_service = AgentRunService(
                    app.state.settings, runtime=runtime
                )
                app.state.agent_run_service = owned_agent_run_service
                # Bound persisted event payloads so a runaway tool observation
                # can never grow the event table / SSE stream unboundedly. The
                # limit is a no-op in tests (left as ``None``) and only takes
                # effect once the application lifespan configures it. The
                # stack callback restores the no-op default on shutdown so one
                # app's ceiling never leaks into another test's process state.
                from backend.app.repositories import agent_runtime as run_repository

                run_repository.set_event_payload_limit(
                    app.state.settings.agent_harness_max_event_payload_bytes
                )
                stack.callback(run_repository.set_event_payload_limit, None)
                # Mirrored module-level toggles: the fetch fallback switches
                # process-wide behavior, so the lifespan must opt it in from
                # Settings (tests override the module seam directly instead).
                from backend.app.services.career_skills import job_discovery as jd_skill

                jd_skill.enable_playwright_fallback(
                    app.state.settings.job_discovery_playwright_fallback_enabled
                )
                stack.callback(jd_skill.enable_playwright_fallback, False)
                jd_skill.configure_playwright_storage_state(
                    app.state.settings.job_discovery_browser_storage_state_path
                )
                stack.callback(jd_skill.configure_playwright_storage_state, None)
                jd_skill.enable_public_api_adapters(
                    app.state.settings.use_public_api_adapters
                )
                stack.callback(jd_skill.enable_public_api_adapters, False)
                from backend.app.services.career_skills import wechat as wechat_skill

                wechat_skill.enable_wechat_ocr(
                    app.state.settings.job_discovery_ocr_enabled
                )
                stack.callback(wechat_skill.enable_wechat_ocr, False)
            yield
    finally:
        if (
            owned_object_store_client is not None
            and hasattr(app.state, "blob_store")
            and getattr(app.state.blob_store, "_client", None)
            is owned_object_store_client
        ):
            del app.state.blob_store
        if (
            owned_session_factory is not None
            and getattr(app.state, "session_factory", None) is owned_session_factory
        ):
            del app.state.session_factory
        if hasattr(app.state, "object_store"):
            del app.state.object_store
        if (
            owned_agent_run_service is not None
            and getattr(app.state, "agent_run_service", None) is owned_agent_run_service
        ):
            del app.state.agent_run_service
        if (
            owned_agent_runtime is not None
            and getattr(app.state, "agent_runtime", None) is owned_agent_runtime
        ):
            del app.state.agent_runtime
        if owned_redis is not None and getattr(app.state, "redis", None) is owned_redis:
            del app.state.redis


def create_app(
    settings: Settings | None = None,
    *,
    blob_store: object | None = None,
    session_factory: object | None = None,
) -> FastAPI:
    """Create the personal-career-assistant API without a legacy graph fallback."""
    resolved = settings or get_settings()
    app = FastAPI(
        title="Personal Career Assistant API",
        version="3.0.0",
        lifespan=lifespan,
    )
    app.state.settings = resolved
    if blob_store is not None:
        app.state.blob_store = blob_store
    if session_factory is not None:
        app.state.session_factory = session_factory
    app.add_middleware(CorrelationIdMiddleware)
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
