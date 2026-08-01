"""HTTP contract tests for authenticated, field-whitelisted PEV run access."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.api.routes import agent_runtime as routes_module
from backend.app.db.base import Base
from backend.app.db.models import User, UserRole
from backend.app.domain.agent_runtime import RunStatus
from backend.app.services.agent_runtime.runtime import AgentRunResult
from backend.app.services.agent_runtime.service import AgentRuntimeDisabledError
from backend.app.services.auth import AuthService
from tests.conftest import settings_override


def _build_app(service) -> FastAPI:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with Session(engine) as db:
        db.add(
            User(
                id="user-a",
                account="user-a@example.test",
                nickname="user-a",
                password_hash="not-a-real-password-hash",
                role=UserRole.STUDENT,
            )
        )
        db.commit()
    app = FastAPI()
    app.state.settings = settings_override(agent_harness_enabled=True)
    app.state.session_factory = factory
    app.state.agent_run_service = service
    app.include_router(routes_module.router, prefix="/api")
    return app


def _headers(app: FastAPI) -> dict[str, str]:
    token = AuthService(app.state.settings).issue_user_token(
        SimpleNamespace(id="user-a", role=UserRole.STUDENT)
    )
    return {"Authorization": f"Bearer {token}"}


def test_post_agent_run_returns_only_safe_run_summary() -> None:
    """Create endpoint must not expose raw context, prompts or internal budget."""
    service = MagicMock()
    service.create_run.return_value = AgentRunResult(
        run_id="run-1", status=RunStatus.succeeded, summary="找到 2 个岗位"
    )
    app = _build_app(service)

    response = TestClient(app).post(
        "/api/agent-runs",
        headers=_headers(app),
        json={
            "goal": "找 AI 应用开发岗位",
            "allowed_skills": ["job-discovery"],
            "context": {"city": "上海"},
        },
    )

    assert response.status_code == 201
    assert response.json() == {
        "id": "run-1",
        "status": "succeeded",
        "summary": "找到 2 个岗位",
        "error_code": None,
    }
    assert service.create_run.call_args.kwargs["user_id"] == "user-a"


def test_post_agent_run_rejects_unknown_input_and_disabled_harness() -> None:
    """Unbounded request fields and disabled runtime both fail predictably."""
    service = MagicMock()
    app = _build_app(service)
    client = TestClient(app)
    invalid = client.post(
        "/api/agent-runs",
        headers=_headers(app),
        json={"goal": "找岗位", "allowed_skills": ["job-discovery"], "unsafe": True},
    )
    assert invalid.status_code == 422

    service.create_run.side_effect = AgentRuntimeDisabledError("agent_harness_disabled")
    disabled = client.post(
        "/api/agent-runs",
        headers=_headers(app),
        json={"goal": "找岗位", "allowed_skills": ["job-discovery"]},
    )
    assert disabled.status_code == 503
    assert disabled.json()["detail"]["code"] == "agent_harness_disabled"


def test_get_agent_run_and_events_project_owner_safe_fields() -> None:
    """Trace API exposes evidence summaries but never hidden run context."""
    service = MagicMock()
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    service.get_run.return_value = SimpleNamespace(
        id="run-1",
        goal="找岗位",
        status=RunStatus.succeeded,
        complexity=SimpleNamespace(value="L2"),
        final_summary="完成",
        error_code=None,
        created_at=now,
        updated_at=now,
    )
    service.list_events.return_value = [
        SimpleNamespace(
            sequence=1,
            event_type="run_started",
            payload_json={"agent_version": "pev-1"},
            created_at=now,
        )
    ]
    app = _build_app(service)
    client = TestClient(app)

    run = client.get("/api/agent-runs/run-1", headers=_headers(app))
    events = client.get("/api/agent-runs/run-1/events", headers=_headers(app))

    assert run.status_code == 200
    assert set(run.json()) == {
        "id", "goal", "status", "complexity", "summary", "error_code", "created_at", "updated_at"
    }
    assert events.status_code == 200
    assert events.json()["items"][0]["event_type"] == "run_started"


def test_list_agent_artifacts_projects_only_owner_safe_artifact_fields() -> None:
    """JD evidence is readable by its owner without exposing run context or prompts."""
    service = MagicMock()
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    service.list_artifacts.return_value = [
        SimpleNamespace(
            id="artifact-1",
            artifact_type="public_job_page",
            source_url="https://jobs.example/agent",
            content_hash="a" * 64,
            content_json={"title": "AI Agent 开发工程师", "visible_text": "岗位职责"},
            created_at=now,
        )
    ]
    app = _build_app(service)

    response = TestClient(app).get(
        "/api/agent-runs/run-1/artifacts", headers=_headers(app)
    )

    assert response.status_code == 200
    assert response.json() == {"items": [{
        "id": "artifact-1",
        "artifact_type": "public_job_page",
        "source_url": "https://jobs.example/agent",
        "content_hash": "a" * 64,
        "content": {"title": "AI Agent 开发工程师", "visible_text": "岗位职责"},
        "created_at": "2026-08-01T00:00:00Z",
    }]}
