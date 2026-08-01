"""Smoke test: company-research skill end-to-end (real Playwright).

Serves a fixture careers page on a local HTTP server and runs the REAL
``CompanyResearchRuntime`` (which spawns the bundled ``browse.py`` via
subprocess) and the REAL API route (real service -> real runtime).  Verifies
the full fetch -> parse -> result path lands without any mocking.

Usage (Windows):
    set RUN_COMPANY_RESEARCH_SMOKE=1
    .\\.venv\\Scripts\\python.exe -m pytest tests/integration/test_company_research_smoke.py -v

Skips unless ``RUN_COMPANY_RESEARCH_SMOKE=1`` so it never runs in the default
unit suite (it spawns a real Chromium browser).
"""

from __future__ import annotations

import http.server
import os
import socketserver
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.api.routes import company_research as routes_module
from backend.app.config import Settings
from backend.app.db.base import Base
from backend.app.db.models import User, UserRole
from backend.app.services.auth import AuthService
from backend.app.services.company_research.runtime import CompanyResearchRuntime

_SMOKE = pytest.mark.skipif(
    not os.environ.get("RUN_COMPANY_RESEARCH_SMOKE"),
    reason="set RUN_COMPANY_RESEARCH_SMOKE=1 to run the company-research smoke test (spawns a real browser)",
)


# A fixture careers page. The ``=== PUBLIC JOB N ===`` blocks are visible text
# (inside ``<pre>``) so ``page.inner_text("body")`` captures them and the
# runtime's public-JSON parser turns them into structured openings.
_FIXTURE_HTML = b"""<!doctype html>
<html><head><title>Acme Careers</title></head><body>
<h1>Acme Careers</h1>
<p>Acme builds world-class engineering teams across Beijing, Shanghai, and
Shenzhen offices. We hire interns and full-time engineers year round.</p>
<pre>=== PUBLIC JOB 1 ===
{"title": "Backend Engineer", "responsibilities": "Build and maintain scalable APIs and services", "location": "Beijing", "company_name": "Acme"}
=== PUBLIC JOB 2 ===
{"title": "Frontend Engineer", "responsibilities": "Build delightful web UI with Vue and TypeScript", "location": "Shanghai", "company_name": "Acme"}
</pre>
</body></html>
"""


class _FixtureHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - http.server API
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(_FIXTURE_HTML)))
        self.end_headers()
        self.wfile.write(_FIXTURE_HTML)

    def log_message(self, *args: object) -> None:  # silence test output
        pass


@pytest.fixture()
def local_server() -> str:
    """Serve the fixture page on an ephemeral local port; return its URL."""
    httpd = socketserver.TCPServer(("127.0.0.1", 0), _FixtureHandler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}/"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _settings() -> Settings:
    return Settings(
        app_env="test",
        app_auth_secret="test-secret-with-at-least-32-characters",
        object_encryption_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        database_url="sqlite+pysqlite:///:memory:",
        redis_url="redis://localhost:6379/15",
        checkpoint_backend="sqlite",
        company_research_enabled=True,
        company_research_agent_version="1.0.0",
    )


def _assert_research_payload(result) -> None:
    assert result.succeeded, (
        f"expected succeeded, got {result.status}: "
        f"block={result.block_reason} err={result.last_error}"
    )
    assert result.profile is not None
    assert result.profile["company_name"] == "Acme"
    assert result.profile["description"]
    assert result.profile["opening_count"] == 2
    assert len(result.openings) == 2
    assert sorted(o["title"] for o in result.openings) == [
        "Backend Engineer",
        "Frontend Engineer",
    ]
    assert "Beijing" in result.profile["locations"]
    assert "Shanghai" in result.profile["locations"]
    assert result.summary.startswith("researched Acme")


# ---------------------------------------------------------------------------
# 1. Runtime-level smoke: real browse.py subprocess + real Playwright.
# ---------------------------------------------------------------------------


@_SMOKE
def test_runtime_runs_real_browse_and_parses(tmp_path: Path, local_server: str) -> None:
    runtime = CompanyResearchRuntime(
        _settings(), artifact_root=tmp_path, object_store=None
    )
    result = runtime.run(
        report_id="smoke",
        company_name="Acme",
        source_url=local_server,
    )
    _assert_research_payload(result)


# ---------------------------------------------------------------------------
# 2. API-level smoke: real FastAPI route -> real service -> real runtime.
# ---------------------------------------------------------------------------


@_SMOKE
def test_api_end_to_end_creates_and_runs(local_server: str) -> None:
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

    settings = _settings()
    app = FastAPI()
    app.state.settings = settings
    app.state.session_factory = session_factory
    # No company_research_service on state -> DI provider builds a real one.
    app.include_router(routes_module.router, prefix="/api")
    token = AuthService(settings).issue_user_token(
        SimpleNamespace(id="user-1", role=UserRole.STUDENT)  # type: ignore[arg-type]
    )
    headers = {"Authorization": f"Bearer {token}"}

    try:
        with TestClient(app) as client:
            resp = client.post(
                "/api/company-research",
                json={"company_name": "Acme", "source_url": local_server},
                headers=headers,
            )
    finally:
        engine.dispose()

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "succeeded"
    assert body["company_name"] == "Acme"
    assert body["profile"]["company_name"] == "Acme"
    assert body["profile"]["opening_count"] == 2
    assert len(body["openings"]) == 2
    assert sorted(o["title"] for o in body["openings"]) == [
        "Backend Engineer",
        "Frontend Engineer",
    ]
    assert body["summary"].startswith("researched Acme")
