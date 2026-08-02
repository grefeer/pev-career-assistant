"""Unit tests for the application-tracking API routes + DI provider.

A self-contained FastAPI app + sync TestClient isolates these from the full
router tree.  The service is mocked so no DB/LLM is invoked; the service,
repository, and generator layers have their own dedicated test modules.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.api.dependencies import get_application_tracking_service
from backend.app.api.routes import application_tracking as routes_module
from backend.app.db.base import Base
from backend.app.db.models import User, UserRole
from backend.app.domain.application_tracking import ApplicationStatus
from backend.app.services.application_tracking.service import (
    ApplicationInputError,
    ApplicationNotFoundError,
    ApplicationTrackingService,
)
from backend.app.services.auth import AuthService
from tests.conftest import settings_override

_NOW = datetime(2026, 7, 29, 12, 0, 0, tzinfo=timezone.utc)


def _settings(*, enabled: bool = True):
    return settings_override(application_tracking_enabled=enabled)


def _fake_record(*, status: ApplicationStatus = ApplicationStatus.saved) -> MagicMock:
    record = MagicMock()
    record.id = "r1"
    record.user_id = "user-1"
    record.target_job_id = None
    record.company_name = "Acme"
    record.title = "Backend Engineer"
    record.apply_url = "https://acme.example"
    record.source = "manual"
    record.status = status
    record.applied_at = None
    record.notes = "referral"
    record.state_version = 0
    record.created_at = _NOW
    record.updated_at = _NOW
    return record


def _fake_event() -> MagicMock:
    event = MagicMock()
    event.id = 7
    event.application_id = "r1"
    event.from_status = "saved"
    event.to_status = "applied"
    event.note = "submitted"
    event.created_at = _NOW
    return event


# ----------------------------------------------------------- app scaffolding


def _build_app(*, enabled: bool, service) -> FastAPI:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

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
    app.state.application_tracking_service = service
    app.include_router(routes_module.router, prefix="/api")
    app._test_engine = engine  # keep alive for the test  # type: ignore[attr-defined]
    return app


def _auth_headers(app: FastAPI) -> dict[str, str]:
    settings = app.state.settings
    user = SimpleNamespace(id="user-1", role=UserRole.STUDENT)
    token = AuthService(settings).issue_user_token(user)  # type: ignore[arg-type]
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------- DI provider


def test_di_provider_returns_injected_service() -> None:
    injected = MagicMock()
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                settings=_settings(),
                application_tracking_service=injected,
            )
        )
    )
    assert get_application_tracking_service(request) is injected


def test_di_provider_constructs_default_service() -> None:
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(settings=_settings()))
    )
    svc = get_application_tracking_service(request)
    assert isinstance(svc, ApplicationTrackingService)


# ----------------------------------------------------------- POST /applications


def test_post_disabled_returns_503() -> None:
    service = MagicMock()
    app = _build_app(enabled=False, service=service)
    client = TestClient(app)
    resp = client.post(
        "/api/applications",
        headers=_auth_headers(app),
        json={"company_name": "Acme", "title": "Eng"},
    )
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "application_tracking_disabled"
    service.create_application.assert_not_called()


def test_post_creates_application() -> None:
    service = MagicMock()
    service.create_application.return_value = _fake_record()
    app = _build_app(enabled=True, service=service)
    client = TestClient(app)
    resp = client.post(
        "/api/applications",
        headers=_auth_headers(app),
        json={"company_name": "Acme", "title": "Backend Engineer"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["id"] == "r1"
    assert body["company_name"] == "Acme"
    assert body["status"] == "saved"
    assert body["state_version"] == 0
    service.create_application.assert_called_once()


def test_post_rejects_unknown_field() -> None:
    service = MagicMock()
    app = _build_app(enabled=True, service=service)
    client = TestClient(app)
    resp = client.post(
        "/api/applications",
        headers=_auth_headers(app),
        json={"company_name": "Acme", "title": "Eng", "bogus": 1},
    )
    assert resp.status_code == 422


def test_post_rejects_empty_required() -> None:
    service = MagicMock()
    app = _build_app(enabled=True, service=service)
    client = TestClient(app)
    resp = client.post(
        "/api/applications",
        headers=_auth_headers(app),
        json={"company_name": "", "title": "Eng"},
    )
    assert resp.status_code == 422


def test_post_requires_auth() -> None:
    service = MagicMock()
    app = _build_app(enabled=True, service=service)
    client = TestClient(app)
    resp = client.post("/api/applications", json={"company_name": "A", "title": "T"})
    assert resp.status_code == 401


# --------------------------------------------------------- GET /applications/{id}


def test_get_one_success() -> None:
    service = MagicMock()
    service.get_application.return_value = _fake_record()
    app = _build_app(enabled=True, service=service)
    client = TestClient(app)
    resp = client.get("/api/applications/r1", headers=_auth_headers(app))
    assert resp.status_code == 200
    assert resp.json()["id"] == "r1"


def test_get_one_not_found() -> None:
    service = MagicMock()
    service.get_application.side_effect = ApplicationNotFoundError("r1")
    app = _build_app(enabled=True, service=service)
    client = TestClient(app)
    resp = client.get("/api/applications/missing", headers=_auth_headers(app))
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "not_found"


def test_get_one_requires_auth() -> None:
    service = MagicMock()
    app = _build_app(enabled=True, service=service)
    client = TestClient(app)
    resp = client.get("/api/applications/r1")
    assert resp.status_code == 401


# ------------------------------------------------------------- GET /applications


def test_list_returns_items_and_total() -> None:
    service = MagicMock()
    service.list_applications.return_value = ([_fake_record()], 5)
    app = _build_app(enabled=True, service=service)
    client = TestClient(app)
    resp = client.get("/api/applications", headers=_auth_headers(app))
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 5
    assert len(body["items"]) == 1
    assert body["items"][0]["id"] == "r1"


def test_list_status_filter_and_pagination() -> None:
    service = MagicMock()
    service.list_applications.return_value = ([_fake_record(status=ApplicationStatus.applied)], 1)
    app = _build_app(enabled=True, service=service)
    client = TestClient(app)
    resp = client.get(
        "/api/applications?status=applied&limit=10&offset=5",
        headers=_auth_headers(app),
    )
    assert resp.status_code == 200
    assert resp.json()["items"][0]["status"] == "applied"
    _, kwargs = service.list_applications.call_args
    assert kwargs["status"] is ApplicationStatus.applied
    assert kwargs["limit"] == 10
    assert kwargs["offset"] == 5


def test_list_rejects_invalid_status() -> None:
    service = MagicMock()
    app = _build_app(enabled=True, service=service)
    client = TestClient(app)
    resp = client.get(
        "/api/applications?status=bogus", headers=_auth_headers(app)
    )
    assert resp.status_code == 422


def test_list_requires_auth() -> None:
    service = MagicMock()
    app = _build_app(enabled=True, service=service)
    client = TestClient(app)
    resp = client.get("/api/applications")
    assert resp.status_code == 401


# --------------------------------------------- POST /applications/{id}/transitions


def test_transition_success() -> None:
    service = MagicMock()
    service.transition.return_value = _fake_record(status=ApplicationStatus.applied)
    app = _build_app(enabled=True, service=service)
    client = TestClient(app)
    resp = client.post(
        "/api/applications/r1/transitions",
        headers=_auth_headers(app),
        json={"to_status": "applied", "note": "submitted"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "applied"


def test_transition_not_found() -> None:
    service = MagicMock()
    service.transition.side_effect = ApplicationNotFoundError("r1")
    app = _build_app(enabled=True, service=service)
    client = TestClient(app)
    resp = client.post(
        "/api/applications/r1/transitions",
        headers=_auth_headers(app),
        json={"to_status": "applied"},
    )
    assert resp.status_code == 404


@pytest.mark.parametrize(
    "code,expected_status",
    [
        ("invalid_transition", 409),
        ("already_terminal", 409),
        ("stale_version", 409),
        ("no_fields", 400),
    ],
)
def test_transition_input_error_mapping(code: str, expected_status: int) -> None:
    service = MagicMock()
    service.transition.side_effect = ApplicationInputError(code, "msg")
    app = _build_app(enabled=True, service=service)
    client = TestClient(app)
    resp = client.post(
        "/api/applications/r1/transitions",
        headers=_auth_headers(app),
        json={"to_status": "applied"},
    )
    assert resp.status_code == expected_status
    assert resp.json()["detail"]["code"] == code


def test_transition_disabled_returns_503() -> None:
    service = MagicMock()
    app = _build_app(enabled=False, service=service)
    client = TestClient(app)
    resp = client.post(
        "/api/applications/r1/transitions",
        headers=_auth_headers(app),
        json={"to_status": "applied"},
    )
    assert resp.status_code == 503
    service.transition.assert_not_called()


def test_transition_rejects_invalid_status() -> None:
    service = MagicMock()
    app = _build_app(enabled=True, service=service)
    client = TestClient(app)
    resp = client.post(
        "/api/applications/r1/transitions",
        headers=_auth_headers(app),
        json={"to_status": "bogus"},
    )
    assert resp.status_code == 422


def test_transition_requires_auth() -> None:
    service = MagicMock()
    app = _build_app(enabled=True, service=service)
    client = TestClient(app)
    resp = client.post(
        "/api/applications/r1/transitions", json={"to_status": "applied"}
    )
    assert resp.status_code == 401


# -------------------------------------------------- PATCH /applications/{id}


def test_patch_update_notes() -> None:
    service = MagicMock()
    service.update_application.return_value = _fake_record()
    app = _build_app(enabled=True, service=service)
    client = TestClient(app)
    resp = client.patch(
        "/api/applications/r1",
        headers=_auth_headers(app),
        json={"notes": "updated"},
    )
    assert resp.status_code == 200
    _, kwargs = service.update_application.call_args
    assert kwargs["notes"] == "updated"


def test_patch_no_fields_returns_400() -> None:
    service = MagicMock()
    service.update_application.side_effect = ApplicationInputError("no_fields", "msg")
    app = _build_app(enabled=True, service=service)
    client = TestClient(app)
    resp = client.patch(
        "/api/applications/r1", headers=_auth_headers(app), json={}
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "no_fields"


def test_patch_not_found() -> None:
    service = MagicMock()
    service.update_application.side_effect = ApplicationNotFoundError("r1")
    app = _build_app(enabled=True, service=service)
    client = TestClient(app)
    resp = client.patch(
        "/api/applications/r1", headers=_auth_headers(app), json={"notes": "x"}
    )
    assert resp.status_code == 404


def test_patch_disabled_returns_503() -> None:
    service = MagicMock()
    app = _build_app(enabled=False, service=service)
    client = TestClient(app)
    resp = client.patch(
        "/api/applications/r1", headers=_auth_headers(app), json={"notes": "x"}
    )
    assert resp.status_code == 503
    service.update_application.assert_not_called()


def test_patch_requires_auth() -> None:
    service = MagicMock()
    app = _build_app(enabled=True, service=service)
    client = TestClient(app)
    resp = client.patch("/api/applications/r1", json={"notes": "x"})
    assert resp.status_code == 401


# ---------------------------------------- GET /applications/{id}/events


def test_events_success() -> None:
    service = MagicMock()
    service.list_events.return_value = [_fake_event()]
    app = _build_app(enabled=True, service=service)
    client = TestClient(app)
    resp = client.get("/api/applications/r1/events", headers=_auth_headers(app))
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["to_status"] == "applied"


def test_events_not_found() -> None:
    service = MagicMock()
    service.list_events.side_effect = ApplicationNotFoundError("r1")
    app = _build_app(enabled=True, service=service)
    client = TestClient(app)
    resp = client.get("/api/applications/r1/events", headers=_auth_headers(app))
    assert resp.status_code == 404


def test_events_requires_auth() -> None:
    service = MagicMock()
    app = _build_app(enabled=True, service=service)
    client = TestClient(app)
    resp = client.get("/api/applications/r1/events")
    assert resp.status_code == 401
