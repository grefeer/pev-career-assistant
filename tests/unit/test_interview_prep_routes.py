"""Unit tests for the interview-prep API routes + DI provider.

A self-contained FastAPI app + sync TestClient isolates these from the full
router tree. The service is mocked so no LLM is invoked; the service and
generator layers have their own dedicated test modules.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.api.dependencies import get_interview_prep_service
from backend.app.api.routes import interview_prep as routes_module
from backend.app.db.base import Base
from backend.app.db.models import User, UserRole
from backend.app.domain.interview_prep import InterviewPrepKitStatus
from backend.app.services.auth import AuthService
from backend.app.services.interview_prep.service import (
    InterviewPrepInputError,
    InterviewPrepNotFoundError,
    InterviewPrepService,
)
from tests.conftest import settings_override

_NOW = datetime(2026, 7, 29, 12, 0, 0, tzinfo=timezone.utc)


def _settings(*, enabled: bool = True):
    return settings_override(
        interview_prep_enabled=enabled,
        interview_prep_agent_version="1.0.0",
    )


def _fake_kit(
    *,
    status: InterviewPrepKitStatus = InterviewPrepKitStatus.ready,
    with_content: bool = True,
) -> MagicMock:
    """A MagicMock quacking like an InterviewPrepKit ORM row."""
    kit = MagicMock()
    kit.id = "k1"
    kit.user_id = "u1"
    kit.target_job_id = "job-1"
    kit.profile_version_id = "cv-1"
    kit.agent_version = "1.0.0"
    kit.status = status
    kit.content_json = (
        {"technical_questions": ["q1"], "behavioral_questions": []} if with_content else None
    )
    kit.preferences_summary_json = {"desired_roles": ["Backend"]} if with_content else None
    kit.match_analysis_json = {"strengths": [{"area": "Python"}]} if with_content else None
    kit.error_code = None if status == InterviewPrepKitStatus.ready else "interview_prep_generation_interrupted"
    kit.last_error = None
    kit.created_at = _NOW
    kit.updated_at = _NOW
    kit.started_at = _NOW
    kit.finished_at = _NOW
    return kit


# ---------------------------------------------------------------------------
# App / client scaffolding
# ---------------------------------------------------------------------------


def _build_app(*, enabled: bool, service) -> FastAPI:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    # Seed a real user so get_current_user (JWT -> DB lookup) succeeds.
    seed = Session(engine)
    user = User(
        id="user-1",
        account="alice",
        nickname="alice",
        password_hash="x",
        role=UserRole.STUDENT,
    )
    seed.add(user)
    seed.commit()
    seed.close()

    settings = _settings(enabled=enabled)
    app = FastAPI()
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.interview_prep_service = service
    app.include_router(routes_module.router, prefix="/api")
    app._test_engine = engine  # keep alive for the test  # type: ignore[attr-defined]
    return app


def _auth_headers(app: FastAPI) -> dict[str, str]:
    settings = app.state.settings
    user = SimpleNamespace(id="user-1", role=UserRole.STUDENT)
    token = AuthService(settings).issue_user_token(user)  # type: ignore[arg-type]
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# DI provider
# ---------------------------------------------------------------------------


def test_di_provider_returns_injected_service() -> None:
    injected = MagicMock()
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                settings=_settings(),
                interview_prep_service=injected,
            )
        )
    )
    assert get_interview_prep_service(request) is injected


def test_di_provider_constructs_default_service() -> None:
    # No injected service -> provider builds a generator-less service so the
    # app still boots; kits finalize as failed at runtime.
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                settings=_settings(),
            )
        )
    )
    svc = get_interview_prep_service(request)
    assert isinstance(svc, InterviewPrepService)
    assert svc.generator is None


# ---------------------------------------------------------------------------
# POST /api/interview-prep
# ---------------------------------------------------------------------------


def test_post_disabled_returns_503() -> None:
    service = MagicMock()
    app = _build_app(enabled=False, service=service)
    with TestClient(app) as client:
        resp = client.post(
            "/api/interview-prep",
            json={"match_report_id": "mr-1"},
            headers=_auth_headers(app),
        )
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "interview_prep_disabled"
    service.create_kit.assert_not_called()


def test_post_success_returns_201_with_payload() -> None:
    service = MagicMock()
    service.create_kit.return_value = _fake_kit()
    app = _build_app(enabled=True, service=service)
    with TestClient(app) as client:
        resp = client.post(
            "/api/interview-prep",
            json={"match_report_id": "mr-1"},
            headers=_auth_headers(app),
        )
    assert resp.status_code == 201
    body = resp.json()
    assert body["id"] == "k1"
    assert body["status"] == "ready"
    assert body["target_job_id"] == "job-1"
    assert body["profile_version_id"] == "cv-1"
    assert body["content"]["technical_questions"] == ["q1"]
    assert body["preferences"]["desired_roles"] == ["Backend"]
    assert body["match_analysis"]["strengths"] == [{"area": "Python"}]
    assert body["error_code"] is None
    service.create_kit.assert_called_once()
    _, kwargs = service.create_kit.call_args
    assert kwargs["match_report_id"] == "mr-1"


def test_post_failed_kit_maps_status_and_error() -> None:
    service = MagicMock()
    service.create_kit.return_value = _fake_kit(
        status=InterviewPrepKitStatus.failed, with_content=False
    )
    app = _build_app(enabled=True, service=service)
    with TestClient(app) as client:
        resp = client.post(
            "/api/interview-prep",
            json={"match_report_id": "mr-1"},
            headers=_auth_headers(app),
        )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "failed"
    assert body["content"] is None
    assert body["error_code"] == "interview_prep_generation_interrupted"


def test_post_input_not_found_returns_404() -> None:
    service = MagicMock()
    service.create_kit.side_effect = InterviewPrepInputError("not_found", "x")
    app = _build_app(enabled=True, service=service)
    with TestClient(app) as client:
        resp = client.post(
            "/api/interview-prep",
            json={"match_report_id": "mr-1"},
            headers=_auth_headers(app),
        )
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "not_found"


def test_post_match_not_completed_returns_409() -> None:
    service = MagicMock()
    service.create_kit.side_effect = InterviewPrepInputError("match_not_completed", "x")
    app = _build_app(enabled=True, service=service)
    with TestClient(app) as client:
        resp = client.post(
            "/api/interview-prep",
            json={"match_report_id": "mr-1"},
            headers=_auth_headers(app),
        )
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "match_not_completed"


def test_post_match_failed_returns_409() -> None:
    service = MagicMock()
    service.create_kit.side_effect = InterviewPrepInputError("match_failed", "x")
    app = _build_app(enabled=True, service=service)
    with TestClient(app) as client:
        resp = client.post(
            "/api/interview-prep",
            json={"match_report_id": "mr-1"},
            headers=_auth_headers(app),
        )
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "match_failed"


def test_post_unknown_input_code_returns_400() -> None:
    service = MagicMock()
    service.create_kit.side_effect = InterviewPrepInputError("unexpected_code", "x")
    app = _build_app(enabled=True, service=service)
    with TestClient(app) as client:
        resp = client.post(
            "/api/interview-prep",
            json={"match_report_id": "mr-1"},
            headers=_auth_headers(app),
        )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "unexpected_code"


def test_post_rejects_unknown_field() -> None:
    service = MagicMock()
    app = _build_app(enabled=True, service=service)
    with TestClient(app) as client:
        resp = client.post(
            "/api/interview-prep",
            json={"match_report_id": "mr-1", "extra": "boom"},
            headers=_auth_headers(app),
        )
    assert resp.status_code == 422
    service.create_kit.assert_not_called()


def test_post_rejects_empty_match_report_id() -> None:
    service = MagicMock()
    app = _build_app(enabled=True, service=service)
    with TestClient(app) as client:
        resp = client.post(
            "/api/interview-prep",
            json={"match_report_id": "   "},
            headers=_auth_headers(app),
        )
    assert resp.status_code == 422


def test_post_unauthenticated_returns_401() -> None:
    service = MagicMock()
    app = _build_app(enabled=True, service=service)
    with TestClient(app) as client:
        resp = client.post("/api/interview-prep", json={"match_report_id": "mr-1"})
    assert resp.status_code == 401
    service.create_kit.assert_not_called()


# ---------------------------------------------------------------------------
# GET /api/interview-prep/{kit_id}
# ---------------------------------------------------------------------------


def test_get_kit_success() -> None:
    service = MagicMock()
    service.get_kit.return_value = _fake_kit()
    app = _build_app(enabled=True, service=service)
    with TestClient(app) as client:
        resp = client.get("/api/interview-prep/k1", headers=_auth_headers(app))
    assert resp.status_code == 200
    assert resp.json()["id"] == "k1"
    service.get_kit.assert_called_once()


def test_get_kit_not_found_returns_404() -> None:
    service = MagicMock()
    service.get_kit.side_effect = InterviewPrepNotFoundError("missing")
    app = _build_app(enabled=True, service=service)
    with TestClient(app) as client:
        resp = client.get("/api/interview-prep/missing", headers=_auth_headers(app))
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "not_found"


def test_get_kit_unauthenticated_returns_401() -> None:
    service = MagicMock()
    app = _build_app(enabled=True, service=service)
    with TestClient(app) as client:
        resp = client.get("/api/interview-prep/k1")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /api/interview-prep
# ---------------------------------------------------------------------------


def test_list_kits_returns_items_and_total() -> None:
    service = MagicMock()
    service.list_kits.return_value = [_fake_kit(), _fake_kit()]
    app = _build_app(enabled=True, service=service)
    with TestClient(app) as client:
        resp = client.get("/api/interview-prep", headers=_auth_headers(app))
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert len(body["items"]) == 2
    assert body["items"][0]["id"] == "k1"


def test_list_kits_empty() -> None:
    service = MagicMock()
    service.list_kits.return_value = []
    app = _build_app(enabled=True, service=service)
    with TestClient(app) as client:
        resp = client.get("/api/interview-prep", headers=_auth_headers(app))
    assert resp.status_code == 200
    assert resp.json() == {"items": [], "total": 0}


def test_list_kits_respects_pagination_query() -> None:
    service = MagicMock()
    service.list_kits.return_value = []
    app = _build_app(enabled=True, service=service)
    with TestClient(app) as client:
        resp = client.get(
            "/api/interview-prep?limit=10&offset=5", headers=_auth_headers(app)
        )
    assert resp.status_code == 200
    _, kwargs = service.list_kits.call_args
    assert kwargs["limit"] == 10
    assert kwargs["offset"] == 5
