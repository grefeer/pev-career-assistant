from __future__ import annotations

from contextlib import AsyncExitStack, asynccontextmanager
import logging
import os
from typing import Any, AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import boto3
from botocore.config import Config
import redis
from sqlalchemy.orm import sessionmaker

from backend.app.api.router import api_router
from backend.app.config import Settings, get_settings
from backend.app.middleware import CorrelationIdMiddleware
from backend.app.services.storage import EncryptedObjectStore, S3BlobStore
from src.checkpointing import checkpointer_context
from src.graph import build_graph
from src.utils import load_env


load_env()


logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    owned_graph: object | None = None
    owned_redis: redis.Redis | None = None
    owned_object_store_client: Any | None = None
    owned_session_factory: Any | None = None
    owned_match_service: object | None = None
    owned_draft_service: object | None = None
    owned_interview_prep_service: object | None = None
    owned_agent_runtime: object | None = None
    owned_agent_run_service: object | None = None
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
            if not hasattr(app.state, "match_service"):
                from src.agents import build_llm
                from src.evidence_matching.graph import EvidenceMatchingGraph
                from backend.app.services.match_service import MatchService

                model = build_llm("analyst")
                match_graph = EvidenceMatchingGraph(model)
                owned_match_service = MatchService(match_graph)
                app.state.match_service = owned_match_service
            if not hasattr(app.state, "draft_service"):
                from backend.app.services.resume_draft_service import ResumeDraftService
                from backend.app.services.resume_tailoring.generator import (
                    LLMDraftGenerator,
                )
                from backend.app.services.resume_tailoring.llm_factory import (
                    build_draft_generator_llm,
                )

                # Construct the agent-driven generator when an LLM key is
                # available; otherwise fall back to a generator-less service so
                # the app still boots (drafts finalize as
                # ``draft_generation_interrupted`` until a key is configured).
                try:
                    draft_llm = build_draft_generator_llm(app.state.settings)
                    owned_draft_service = ResumeDraftService(
                        LLMDraftGenerator(draft_llm, app.state.settings)
                    )
                except Exception:
                    logger.warning(
                        "resume-tailoring LLM unavailable; drafts disabled",
                        exc_info=True,
                    )
                    owned_draft_service = ResumeDraftService()
                app.state.draft_service = owned_draft_service
            if not hasattr(app.state, "interview_prep_service"):
                from backend.app.services.interview_prep.generator import (
                    LLMInterviewPrepGenerator,
                )
                from backend.app.services.interview_prep.llm_factory import (
                    build_interview_prep_llm,
                )
                from backend.app.services.interview_prep.service import (
                    InterviewPrepService,
                )

                # Construct the agent-driven generator when an LLM key is
                # available; otherwise fall back to a generator-less service so
                # the app still boots (kits finalize as failed with
                # ``interview_prep_generator_unavailable`` until a key is set).
                try:
                    prep_llm = build_interview_prep_llm(app.state.settings)
                    owned_interview_prep_service = InterviewPrepService(
                        app.state.settings,
                        generator=LLMInterviewPrepGenerator(
                            prep_llm, app.state.settings
                        ),
                    )
                except Exception:
                    logger.warning(
                        "interview-prep LLM unavailable; prep disabled",
                        exc_info=True,
                    )
                    owned_interview_prep_service = InterviewPrepService(
                        app.state.settings
                    )
                app.state.interview_prep_service = owned_interview_prep_service
            if not hasattr(app.state, "application_tracking_service"):
                from backend.app.services.application_tracking.service import (
                    ApplicationTrackingService,
                )

                # Non-agent skill: no LLM / object store, so construction never
                # fails. Held on app.state so the DI provider can reuse one
                # instance across requests.
                app.state.application_tracking_service = ApplicationTrackingService(
                    app.state.settings
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
                from backend.app.services.career_skills.registry import (
                    build_career_tool_registry,
                )
                from backend.app.services.agent_runtime.verifier_agent import VerifierAgent

                runtime = getattr(app.state, "agent_runtime", None)
                if not hasattr(app.state, "agent_runtime"):
                    app.state.agent_runtime = None
                if runtime is None and app.state.settings.agent_harness_enabled:
                    try:
                        gateway = build_agent_model_gateway(app.state.settings)
                        tools = build_career_tool_registry()
                        runtime = AgentRuntime(
                            planner=PlannerAgent(gateway=gateway, tools=tools),
                            executor=ExecutorAgent(gateway=gateway, tools=tools),
                            verifier=VerifierAgent(gateway=gateway, tools=tools),
                            agent_version="pev-1",
                        )
                        app.state.agent_runtime = runtime
                        owned_agent_runtime = runtime
                    except AgentModelGatewayConfigError:
                        logger.warning("adaptive PEV runtime unavailable: model key missing")
                owned_agent_run_service = AgentRunService(
                    app.state.settings, runtime=runtime
                )
                app.state.agent_run_service = owned_agent_run_service
            yield
    finally:
        if owned_graph is not None and getattr(app.state, "graph", None) is owned_graph:
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
        if (
            owned_session_factory is not None
            and getattr(app.state, "session_factory", None) is owned_session_factory
        ):
            del app.state.session_factory
        if hasattr(app.state, "object_store"):
            del app.state.object_store
        if owned_match_service is not None and getattr(app.state, "match_service", None) is owned_match_service:
            del app.state.match_service
        if owned_draft_service is not None and getattr(app.state, "draft_service", None) is owned_draft_service:
            del app.state.draft_service
        if (
            owned_interview_prep_service is not None
            and getattr(app.state, "interview_prep_service", None)
            is owned_interview_prep_service
        ):
            del app.state.interview_prep_service
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


def create_app(
    settings: Settings | None = None,
    *,
    graph: object | None = None,
    checkpointer: object | None = None,
    blob_store: object | None = None,
    session_factory: object | None = None,
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
