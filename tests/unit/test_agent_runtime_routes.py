"""HTTP contract tests for authenticated, field-whitelisted PEV run access."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.api.routes import agent_runtime as routes_module
from backend.app.db.base import Base
from backend.app.db.models import User, UserRole
from backend.app.domain.agent_runtime import RunStatus
from backend.app.services.agent_runtime.runtime import AgentRunResult
from backend.app.services.agent_runtime.service import (
    AgentRunNotFoundError,
    AgentRunNotResumableError,
    AgentRuntimeDisabledError,
    AgentRuntimeUnavailableError,
)
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
    service.queue_run.return_value = AgentRunResult(
        run_id="run-1", status=RunStatus.queued, summary=None
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
        "status": "queued",
        "summary": None,
        "error_code": None,
    }
    assert service.queue_run.call_args.kwargs["user_id"] == "user-a"
    assert service.queue_run.call_args.kwargs["task"].budget.max_agent_turns == 12


def test_post_agent_run_scales_hard_turn_ceiling_for_multi_skill_work() -> None:
    service = MagicMock()
    service.queue_run.return_value = AgentRunResult(
        run_id="run-1", status=RunStatus.queued, summary=None
    )
    app = _build_app(service)

    response = TestClient(app).post(
        "/api/agent-runs",
        headers=_headers(app),
        json={
            "goal": "找岗位并完成匹配、简历优化和面试计划",
            "allowed_skills": [
                "job-discovery",
                "job-matching",
                "resume-tailoring",
                "career-planning",
            ],
        },
    )

    assert response.status_code == 201
    assert service.queue_run.call_args.kwargs["task"].budget.max_agent_turns == 36


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

    service.queue_run.side_effect = AgentRuntimeDisabledError("agent_harness_disabled")
    disabled = client.post(
        "/api/agent-runs",
        headers=_headers(app),
        json={"goal": "找岗位", "allowed_skills": ["job-discovery"]},
    )
    assert disabled.status_code == 503
    assert disabled.json()["detail"]["code"] == "agent_harness_disabled"


def test_post_agent_run_resume_accepts_only_a_human_reply() -> None:
    """The browser resumes a durable Run without supplying budgets or private context."""
    service = MagicMock()
    service.resume_run.return_value = AgentRunResult(
        run_id="run-1", status=RunStatus.succeeded, summary="已按北京筛选"
    )
    app = _build_app(service)
    client = TestClient(app)

    response = client.post(
        "/api/agent-runs/run-1/resume",
        headers=_headers(app),
        json={"user_response": "北京"},
    )
    invalid = client.post(
        "/api/agent-runs/run-1/resume",
        headers=_headers(app),
        json={"user_response": "北京", "budget": {"max_agent_turns": 99}},
    )

    assert response.status_code == 200
    assert response.json() == {
        "id": "run-1",
        "status": "succeeded",
        "summary": "已按北京筛选",
        "error_code": None,
    }
    assert invalid.status_code == 422
    assert service.resume_run.call_args.kwargs == {
        "user_id": "user-a", "run_id": "run-1", "user_response": "北京"
    }


def test_post_agent_run_recover_accepts_no_client_context_or_budget() -> None:
    service = MagicMock()
    service.recover_run.return_value = AgentRunResult(
        run_id="run-1", status=RunStatus.running, summary="正在从已持久化检查点恢复"
    )
    app = _build_app(service)
    client = TestClient(app)

    response = client.post("/api/agent-runs/run-1/recover", headers=_headers(app), json={})
    invalid = client.post(
        "/api/agent-runs/run-1/recover",
        headers=_headers(app),
        json={"context": {"unsafe": True}},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "running"
    assert invalid.status_code == 422
    assert service.recover_run.call_args.kwargs == {"user_id": "user-a", "run_id": "run-1"}


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


def test_agent_event_stream_replays_only_owner_safe_events_after_a_cursor() -> None:
    """SSE reconnects from a durable sequence cursor rather than client context."""
    service = MagicMock()
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    service.list_events.return_value = [
        SimpleNamespace(sequence=1, event_type="run_started", payload_json={"safe": True}, created_at=now),
        SimpleNamespace(sequence=2, event_type="plan_created", payload_json={"revision": 1}, created_at=now),
    ]
    app = _build_app(service)

    response = TestClient(app).get(
        "/api/agent-runs/run-1/events/stream?after_sequence=1&follow=false",
        headers=_headers(app),
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "id: 2" in response.text
    assert "event: plan_created" in response.text
    assert '"revision":1' in response.text
    assert "run_started" not in response.text
    assert service.list_events.call_args.kwargs == {"user_id": "user-a", "run_id": "run-1"}


def test_agent_event_stream_honors_last_event_id_and_hides_missing_runs() -> None:
    assert routes_module._effective_event_cursor(2, "5") == 5
    assert routes_module._effective_event_cursor(2, "invalid") == 2
    service = MagicMock()
    service.list_events.side_effect = AgentRunNotFoundError("run-1")
    app = _build_app(service)

    response = TestClient(app).get(
        "/api/agent-runs/run-1/events/stream?follow=false",
        headers={**_headers(app), "Last-Event-ID": "4"},
    )

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "not_found"


def test_following_sse_stream_heartbeats_then_stops_when_owner_run_disappears(monkeypatch) -> None:
    service = MagicMock()
    service.list_events.side_effect = [[], AgentRunNotFoundError("run-1")]
    monkeypatch.setattr(routes_module.time, "sleep", lambda _seconds: None)
    response = routes_module.stream_agent_events(
        "run-1", MagicMock(), SimpleNamespace(id="user-a"), service, follow=True
    )

    async def consume() -> list[str]:
        iterator = response.body_iterator
        first = await anext(iterator)
        with pytest.raises(StopAsyncIteration):
            await anext(iterator)
        return [first]

    assert asyncio.run(consume()) == [": keep-alive\n\n"]


def test_list_agent_runs_projects_only_safe_owner_history_fields() -> None:
    """Workspace history exposes safe summaries, never context or model messages."""
    service = MagicMock()
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    service.list_runs.return_value = [
        SimpleNamespace(
            id="run-1", goal="找岗位", status=RunStatus.succeeded,
            complexity=SimpleNamespace(value="L2"), final_summary="完成", error_code=None,
            created_at=now, updated_at=now,
        )
    ]
    app = _build_app(service)

    response = TestClient(app).get("/api/agent-runs?limit=20", headers=_headers(app))

    assert response.status_code == 200
    assert response.json() == {"items": [{
        "id": "run-1", "goal": "找岗位", "status": "succeeded", "complexity": "L2",
        "summary": "完成", "error_code": None,
        "created_at": "2026-08-01T00:00:00Z", "updated_at": "2026-08-01T00:00:00Z",
    }]}
    assert service.list_runs.call_args.kwargs == {"user_id": "user-a", "limit": 20}


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


def test_list_agent_plans_projects_only_owner_safe_plan_fields() -> None:
    service = MagicMock()
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    service.list_plans.return_value = [
        SimpleNamespace(
            id="plan-1", revision=2, complexity=SimpleNamespace(value="L3"),
            plan_json={
                "task": {"private_context": {"skills": ["secret"]}},
                "success_criteria": ["输出可核验匹配"],
                "steps": [{
                    "step_id": "match", "objective": "匹配", "allowed_skills": ["job-matching"],
                    "success_criteria": ["给出证据"], "requires_verification": True,
                }],
            },
            created_at=now,
        )
    ]
    app = _build_app(service)

    response = TestClient(app).get("/api/agent-runs/run-1/plans", headers=_headers(app))

    assert response.status_code == 200
    assert response.json() == {"items": [{
        "id": "plan-1", "revision": 2, "complexity": "L3",
        "success_criteria": ["输出可核验匹配"],
        "steps": [{
            "id": "match", "objective": "匹配", "allowed_skills": ["job-matching"],
            "success_criteria": ["给出证据"], "requires_verification": True,
        }],
        "created_at": "2026-08-01T00:00:00Z",
    }]}


def test_serializers_drop_malformed_plan_values_and_default_missing_enums() -> None:
    """A corrupt persisted payload cannot leak or break the owner-safe projection."""
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    projected = routes_module._to_plan_response(
        SimpleNamespace(
            id="plan-1",
            revision=1,
            complexity=None,
            plan_json={
                "success_criteria": ["valid", 3],
                "steps": [None, {"step_id": 1, "objective": "bad"}, {
                    "step_id": "bad-skills", "objective": "bad", "allowed_skills": [1],
                }, {
                    "step_id": "bad-criteria", "objective": "bad", "allowed_skills": [],
                    "success_criteria": [1],
                }],
            },
            created_at=now,
        )
    )
    run = routes_module._to_run_response(
        SimpleNamespace(
            id="run-1", goal="x", status=None, complexity=None,
            final_summary=None, error_code=None, created_at=now, updated_at=now,
        )
    )

    assert projected.complexity == "L1"
    assert projected.success_criteria == []
    assert projected.steps == []
    assert run.status == "failed"
    assert run.complexity is None


def test_to_plan_response_skips_steps_when_payload_steps_is_not_a_list() -> None:
    """A non-list ``steps`` value skips the projection loop (71->98)."""
    projected = routes_module._to_plan_response(
        SimpleNamespace(
            id="plan-1",
            revision=1,
            complexity=None,
            plan_json={"steps": "not-a-list"},
            created_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        )
    )
    assert projected.steps == []


def test_create_run_reports_unavailable_model_gateway() -> None:
    service = MagicMock()
    service.queue_run.side_effect = AgentRuntimeUnavailableError("agent_harness_unavailable")
    app = _build_app(service)

    response = TestClient(app).post(
        "/api/agent-runs", headers=_headers(app),
        json={"goal": "找岗位", "allowed_skills": ["job-discovery"]},
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "agent_harness_unavailable"


def test_resume_and_recover_translate_all_recoverable_service_errors() -> None:
    cases = (
        ("resume_run", "/api/agent-runs/run-1/resume", {"user_response": "北京"}, AgentRunNotFoundError("x"), 404),
        ("resume_run", "/api/agent-runs/run-1/resume", {"user_response": "北京"}, AgentRunNotResumableError("x"), 409),
        ("resume_run", "/api/agent-runs/run-1/resume", {"user_response": "北京"}, AgentRuntimeDisabledError("x"), 503),
        ("resume_run", "/api/agent-runs/run-1/resume", {"user_response": "北京"}, AgentRuntimeUnavailableError("x"), 503),
        ("recover_run", "/api/agent-runs/run-1/recover", {}, AgentRunNotFoundError("x"), 404),
        ("recover_run", "/api/agent-runs/run-1/recover", {}, AgentRunNotResumableError("x"), 409),
        ("recover_run", "/api/agent-runs/run-1/recover", {}, AgentRuntimeDisabledError("x"), 503),
        ("recover_run", "/api/agent-runs/run-1/recover", {}, AgentRuntimeUnavailableError("x"), 503),
    )
    for method, path, payload, error, expected_status in cases:
        service = MagicMock()
        getattr(service, method).side_effect = error
        app = _build_app(service)
        response = TestClient(app).post(path, headers=_headers(app), json=payload)
        assert response.status_code == expected_status


def test_owner_read_routes_translate_missing_runs_without_disclosing_state() -> None:
    cases = (
        ("get_run", "/api/agent-runs/run-1"),
        ("list_events", "/api/agent-runs/run-1/events"),
        ("list_plans", "/api/agent-runs/run-1/plans"),
        ("list_artifacts", "/api/agent-runs/run-1/artifacts"),
    )
    for method, path in cases:
        service = MagicMock()
        getattr(service, method).side_effect = AgentRunNotFoundError("run-1")
        app = _build_app(service)
        response = TestClient(app).get(path, headers=_headers(app))
        assert response.status_code == 404
        assert response.json()["detail"] == {"code": "not_found"}
