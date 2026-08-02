"""Unit tests for the company-research API routes + DI provider.

A self-contained FastAPI app + sync TestClient isolates these from the full
router tree.  The service is mocked so no browser is spawned; the service and
runtime layers have their own dedicated test modules.
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

from backend.app.api.dependencies import get_company_research_service
from backend.app.api.routes import company_research as routes_module
from backend.app.db.base import Base
from backend.app.db.models import User, UserRole
from backend.app.domain.company_research import (
    CompanyResearchBlockReason,
    CompanyResearchStatus,
)
from backend.app.services.auth import AuthService
from backend.app.services.company_research.service import (
    CompanyResearchNotFoundError,
    CompanyResearchService,
)
from tests.conftest import settings_override


_NOW = datetime(2026, 7, 29, 12, 0, 0, tzinfo=timezone.utc)


def _settings(*, enabled: bool = True):
    return settings_override(
        company_research_enabled=enabled,
        company_research_agent_version="1.0.0",
    )


def _fake_report(
    *,
    status: CompanyResearchStatus = CompanyResearchStatus.succeeded,
    block_reason: CompanyResearchBlockReason | None = None,
    with_payload: bool = True,
) -> MagicMock:
    """A MagicMock quacking like a CompanyResearchReport ORM row."""
    report = MagicMock()
    report.id = "r1"
    report.user_id = "u1"
    report.company_name = "Acme"
    report.source_url = "https://careers.acme.example"
    report.agent_version = "1.0.0"
    report.status = status
    report.block_reason = block_reason
    report.profile_json = {"company_name": "Acme", "opening_count": 2} if with_payload else None
    report.openings_json = [{"title": "Engineer", "locations": ["Beijing"]}] if with_payload else None
    report.evidence_refs_json = [{"evidence_type": "page_text"}] if with_payload else None
    report.summary = "2 openings found" if with_payload else None
    report.last_error = None
    report.created_at = _NOW
    report.updated_at = _NOW
    report.started_at = _NOW
    report.finished_at = _NOW
    return report


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
    app.state.object_store = MagicMock()
    app.state.company_research_service = service
    app.include_router(routes_module.router, prefix="/api")
    app._test_engine = engine  # keep alive for the test  # type: ignore[attr-defined]
    return app


def _auth_headers(app: FastAPI) -> dict[str, str]:
    settings = app.state.settings
    # Re-read the seeded user id; AuthService only needs id + role.
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
                object_store=MagicMock(),
                company_research_service=injected,
            )
        )
    )
    assert get_company_research_service(request) is injected


def test_di_provider_constructs_default_service() -> None:
    # No injected service -> provider builds a real CompanyResearchService
    # (runtime construction is cheap; no browser is spawned until run()).
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                settings=_settings(),
                object_store=MagicMock(),
            )
        )
    )
    svc = get_company_research_service(request)
    assert isinstance(svc, CompanyResearchService)


# ---------------------------------------------------------------------------
# POST /api/company-research
# ---------------------------------------------------------------------------


def test_post_disabled_returns_503() -> None:
    service = MagicMock()
    app = _build_app(enabled=False, service=service)
    with TestClient(app) as client:
        resp = client.post(
            "/api/company-research",
            json={"company_name": "Acme", "source_url": "https://careers.acme.example"},
            headers=_auth_headers(app),
        )
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "company_research_disabled"
    service.create_report.assert_not_called()


def test_post_success_returns_201_with_payload() -> None:
    service = MagicMock()
    service.create_report.return_value = SimpleNamespace(id="r1")
    service.run_report.return_value = _fake_report()
    app = _build_app(enabled=True, service=service)
    with TestClient(app) as client:
        resp = client.post(
            "/api/company-research",
            json={"company_name": "Acme", "source_url": "https://careers.acme.example"},
            headers=_auth_headers(app),
        )
    assert resp.status_code == 201
    body = resp.json()
    assert body["id"] == "r1"
    assert body["status"] == "succeeded"
    assert body["company_name"] == "Acme"
    assert body["profile"]["opening_count"] == 2
    assert body["openings"] == [{"title": "Engineer", "locations": ["Beijing"]}]
    assert body["evidence_refs"] == [{"evidence_type": "page_text"}]
    assert body["block_reason"] is None
    service.create_report.assert_called_once()
    service.run_report.assert_called_once()


def test_post_success_maps_block_reason_value() -> None:
    service = MagicMock()
    service.create_report.return_value = SimpleNamespace(id="r1")
    service.run_report.return_value = _fake_report(
        status=CompanyResearchStatus.needs_manual_review,
        block_reason=CompanyResearchBlockReason.anti_bot,
    )
    app = _build_app(enabled=True, service=service)
    with TestClient(app) as client:
        resp = client.post(
            "/api/company-research",
            json={"company_name": "Acme", "source_url": "https://careers.acme.example"},
            headers=_auth_headers(app),
        )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "needs_manual_review"
    assert body["block_reason"] == "anti_bot"


def test_post_success_with_empty_payload() -> None:
    # A blocked/empty report has None JSON columns -> response defaults to
    # empty list / None rather than crashing.
    service = MagicMock()
    service.create_report.return_value = SimpleNamespace(id="r1")
    service.run_report.return_value = _fake_report(
        status=CompanyResearchStatus.needs_manual_review, with_payload=False
    )
    app = _build_app(enabled=True, service=service)
    with TestClient(app) as client:
        resp = client.post(
            "/api/company-research",
            json={"company_name": "Acme", "source_url": "https://careers.acme.example"},
            headers=_auth_headers(app),
        )
    assert resp.status_code == 201
    body = resp.json()
    assert body["profile"] is None
    assert body["openings"] == []
    assert body["evidence_refs"] == []


def test_post_invalid_input_returns_422() -> None:
    service = MagicMock()
    app = _build_app(enabled=True, service=service)
    with TestClient(app) as client:
        # Bad URL scheme -> DTO rejects before the handler runs.
        resp = client.post(
            "/api/company-research",
            json={"company_name": "Acme", "source_url": "ftp://nope.example"},
            headers=_auth_headers(app),
        )
    assert resp.status_code == 422
    service.create_report.assert_not_called()


def test_post_rejects_unknown_field() -> None:
    service = MagicMock()
    app = _build_app(enabled=True, service=service)
    with TestClient(app) as client:
        resp = client.post(
            "/api/company-research",
            json={
                "company_name": "Acme",
                "source_url": "https://careers.acme.example",
                "extra": "boom",
            },
            headers=_auth_headers(app),
        )
    assert resp.status_code == 422


def test_post_unauthenticated_returns_401() -> None:
    service = MagicMock()
    app = _build_app(enabled=True, service=service)
    with TestClient(app) as client:
        resp = client.post(
            "/api/company-research",
            json={"company_name": "Acme", "source_url": "https://careers.acme.example"},
        )
    assert resp.status_code == 401
    service.create_report.assert_not_called()


# ---------------------------------------------------------------------------
# GET /api/company-research/{id}
# ---------------------------------------------------------------------------


def test_get_report_success() -> None:
    service = MagicMock()
    service.get_report.return_value = _fake_report()
    app = _build_app(enabled=True, service=service)
    with TestClient(app) as client:
        resp = client.get("/api/company-research/r1", headers=_auth_headers(app))
    assert resp.status_code == 200
    assert resp.json()["id"] == "r1"
    service.get_report.assert_called_once()


def test_get_report_not_found_returns_404() -> None:
    service = MagicMock()
    service.get_report.side_effect = CompanyResearchNotFoundError("missing")
    app = _build_app(enabled=True, service=service)
    with TestClient(app) as client:
        resp = client.get("/api/company-research/missing", headers=_auth_headers(app))
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "not_found"


def test_get_report_unauthenticated_returns_401() -> None:
    service = MagicMock()
    app = _build_app(enabled=True, service=service)
    with TestClient(app) as client:
        resp = client.get("/api/company-research/r1")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /api/company-research
# ---------------------------------------------------------------------------


def test_list_reports_returns_items_and_total() -> None:
    service = MagicMock()
    service.list_reports.return_value = [_fake_report(), _fake_report()]
    app = _build_app(enabled=True, service=service)
    with TestClient(app) as client:
        resp = client.get("/api/company-research", headers=_auth_headers(app))
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert len(body["items"]) == 2
    assert body["items"][0]["id"] == "r1"


def test_list_reports_empty() -> None:
    service = MagicMock()
    service.list_reports.return_value = []
    app = _build_app(enabled=True, service=service)
    with TestClient(app) as client:
        resp = client.get("/api/company-research", headers=_auth_headers(app))
    assert resp.status_code == 200
    assert resp.json() == {"items": [], "total": 0}


def test_list_reports_respects_pagination_query() -> None:
    service = MagicMock()
    service.list_reports.return_value = []
    app = _build_app(enabled=True, service=service)
    with TestClient(app) as client:
        resp = client.get(
            "/api/company-research?limit=10&offset=5", headers=_auth_headers(app)
        )
    assert resp.status_code == 200
    _, kwargs = service.list_reports.call_args
    assert kwargs["limit"] == 10
    assert kwargs["offset"] == 5
