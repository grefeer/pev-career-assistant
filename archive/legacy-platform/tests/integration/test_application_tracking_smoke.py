"""End-to-end smoke test for the application-tracking skill (投递进度跟踪).

Exercises the full stack through the real FastAPI router + real
``ApplicationTrackingService`` + real in-memory SQLite (FK ON) + real JWT auth:
create -> transition through the whole pipeline -> terminal guard -> event
log -> patch -> list.  No LLM and no network are required, so this always runs
(it doubles as the skill's smoke test).
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.api.routes import application_tracking as routes_module
from backend.app.db.base import Base
from backend.app.db.models import User, UserRole
from backend.app.services.application_tracking.service import ApplicationTrackingService
from backend.app.services.auth import AuthService
from tests.conftest import settings_override


def _build_app() -> FastAPI:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    seed = Session(engine)
    seed.add(
        User(
            id="user-1",
            account="alice",
            nickname="alice",
            password_hash="x",
            role=UserRole.STUDENT,
        )
    )
    seed.commit()
    seed.close()

    settings = settings_override(application_tracking_enabled=True)
    app = FastAPI()
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.application_tracking_service = ApplicationTrackingService(settings)
    app.include_router(routes_module.router, prefix="/api")
    app._test_engine = engine  # keep alive  # type: ignore[attr-defined]
    return app


def _auth_headers(app: FastAPI) -> dict[str, str]:
    from types import SimpleNamespace

    user = SimpleNamespace(id="user-1", role=UserRole.STUDENT)
    token = AuthService(app.state.settings).issue_user_token(user)  # type: ignore[arg-type]
    return {"Authorization": f"Bearer {token}"}


def test_application_tracking_full_lifecycle() -> None:
    app = _build_app()
    client = TestClient(app)
    headers = _auth_headers(app)

    # 1. Create a tracked application (starts in saved).
    resp = client.post(
        "/api/applications",
        headers=headers,
        json={
            "company_name": "Acme",
            "title": "Backend Engineer",
            "apply_url": "https://acme.example/apply",
            "source": "manual",
            "notes": "referral",
        },
    )
    assert resp.status_code == 201, resp.text
    record = resp.json()
    app_id = record["id"]
    assert record["status"] == "saved"
    assert record["state_version"] == 0
    assert record["applied_at"] is None

    # 2. Advance through the whole pipeline, stamping applied_at on the way in.
    transitions = [
        ("applied", "submitted via portal"),
        ("screening", None),
        ("interview", "onsite scheduled"),
        ("offer", "verbal offer received"),
    ]
    for idx, (to_status, note) in enumerate(transitions, start=1):
        resp = client.post(
            f"/api/applications/{app_id}/transitions",
            headers=headers,
            json={"to_status": to_status, "note": note},
        )
        assert resp.status_code == 200, (to_status, resp.text)
        body = resp.json()
        assert body["status"] == to_status
        assert body["state_version"] == idx
        if to_status == "applied":
            assert body["applied_at"] is not None
            applied_at = body["applied_at"]
        else:
            assert body["applied_at"] == applied_at  # stamped only once

    # 3. Terminal state: offer admits no further transition.
    resp = client.post(
        f"/api/applications/{app_id}/transitions",
        headers=headers,
        json={"to_status": "applied"},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "already_terminal"

    # 4. Event log is append-only and oldest-first, with notes preserved.
    resp = client.get(f"/api/applications/{app_id}/events", headers=headers)
    assert resp.status_code == 200, resp.text
    events = resp.json()["items"]
    assert [e["to_status"] for e in events] == [
        "applied",
        "screening",
        "interview",
        "offer",
    ]
    assert [e["from_status"] for e in events] == [
        "saved",
        "applied",
        "screening",
        "interview",
    ]
    assert events[0]["note"] == "submitted via portal"
    assert events[1]["note"] is None
    assert events[2]["note"] == "onsite scheduled"
    assert events[3]["note"] == "verbal offer received"

    # 5. Patch notes on the terminal record (editable fields are independent
    #    of the state machine).
    resp = client.patch(
        f"/api/applications/{app_id}",
        headers=headers,
        json={"notes": "accepted offer - starting Aug"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["notes"] == "accepted offer - starting Aug"

    # 6. Get one + list reflect the final state.
    resp = client.get(f"/api/applications/{app_id}", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "offer"

    resp = client.get("/api/applications", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["status"] == "offer"


def test_application_tracking_rejected_and_withdrawn_paths() -> None:
    app = _build_app()
    client = TestClient(app)
    headers = _auth_headers(app)

    # Record A: applied -> rejected (terminal).
    resp = client.post(
        "/api/applications", headers=headers, json={"company_name": "B", "title": "T"}
    )
    a_id = resp.json()["id"]
    client.post(
        f"/api/applications/{a_id}/transitions",
        headers=headers,
        json={"to_status": "applied"},
    )
    resp = client.post(
        f"/api/applications/{a_id}/transitions",
        headers=headers,
        json={"to_status": "rejected"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"

    # Record B: saved -> withdrawn (terminal, no applied step needed).
    resp = client.post(
        "/api/applications", headers=headers, json={"company_name": "C", "title": "T"}
    )
    b_id = resp.json()["id"]
    resp = client.post(
        f"/api/applications/{b_id}/transitions",
        headers=headers,
        json={"to_status": "withdrawn"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "withdrawn"

    # List with a status filter narrows correctly.
    resp = client.get(
        "/api/applications?status=rejected", headers=headers
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == a_id


def test_application_tracking_invalid_transition_and_stale_version() -> None:
    app = _build_app()
    client = TestClient(app)
    headers = _auth_headers(app)

    resp = client.post(
        "/api/applications", headers=headers, json={"company_name": "D", "title": "T"}
    )
    app_id = resp.json()["id"]

    # Illegal skip: saved -> offer.
    resp = client.post(
        f"/api/applications/{app_id}/transitions",
        headers=headers,
        json={"to_status": "offer"},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "invalid_transition"

    # Stale version: expected_version does not match state_version (0).
    resp = client.post(
        f"/api/applications/{app_id}/transitions",
        headers=headers,
        json={"to_status": "applied", "expected_version": 99},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "stale_version"

    # Correct version advances.
    resp = client.post(
        f"/api/applications/{app_id}/transitions",
        headers=headers,
        json={"to_status": "applied", "expected_version": 0},
    )
    assert resp.status_code == 200
    assert resp.json()["state_version"] == 1


def test_application_tracking_owner_scoped() -> None:
    """A second user cannot read or transition another user's application."""
    app = _build_app()
    client = TestClient(app)
    headers_a = _auth_headers(app)

    resp = client.post(
        "/api/applications", headers=headers_a, json={"company_name": "E", "title": "T"}
    )
    app_id = resp.json()["id"]

    # Seed a second user and mint their token.
    from types import SimpleNamespace

    from sqlalchemy.orm import Session

    seed = Session(app._test_engine)  # type: ignore[attr-defined]
    from backend.app.db.models import User, UserRole

    seed.add(
        User(
            id="user-2",
            account="bob",
            nickname="bob",
            password_hash="x",
            role=UserRole.STUDENT,
        )
    )
    seed.commit()
    seed.close()
    bob = SimpleNamespace(id="user-2", role=UserRole.STUDENT)
    headers_b = {
        "Authorization": f"Bearer {AuthService(app.state.settings).issue_user_token(bob)}"  # type: ignore[arg-type]
    }

    assert client.get(f"/api/applications/{app_id}", headers=headers_b).status_code == 404
    assert (
        client.post(
            f"/api/applications/{app_id}/transitions",
            headers=headers_b,
            json={"to_status": "applied"},
        ).status_code
        == 404
    )
    assert (
        client.get(f"/api/applications/{app_id}/events", headers=headers_b).status_code
        == 404
    )
    # Bob's own list is empty.
    assert client.get("/api/applications", headers=headers_b).json()["total"] == 0


def test_application_tracking_disabled_returns_503() -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    seed = Session(engine)
    seed.add(
        User(
            id="user-1",
            account="alice",
            nickname="alice",
            password_hash="x",
            role=UserRole.STUDENT,
        )
    )
    seed.commit()
    seed.close()

    settings = settings_override(application_tracking_enabled=False)
    app = FastAPI()
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.application_tracking_service = ApplicationTrackingService(settings)
    app.include_router(routes_module.router, prefix="/api")
    app._test_engine = engine  # type: ignore[attr-defined]

    client = TestClient(app)
    headers = _auth_headers(app)
    # Mutating endpoints are disabled; reads still work but find nothing.
    assert (
        client.post(
            "/api/applications", headers=headers, json={"company_name": "A", "title": "T"}
        ).status_code
        == 503
    )
