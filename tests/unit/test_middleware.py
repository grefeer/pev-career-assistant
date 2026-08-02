"""Correlation ID middleware injects and echoes a request trace identifier."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from backend.app.middleware import CorrelationIdMiddleware


def test_middleware_passes_through_inbound_correlation_id() -> None:
    """An X-Correlation-ID header from the proxy is echoed unchanged."""
    app = FastAPI()

    @app.get("/ping")
    def _ping() -> dict[str, bool]:
        return {"ok": True}

    app.add_middleware(CorrelationIdMiddleware)
    response = TestClient(app).get("/ping", headers={"X-Correlation-ID": "trace-123"})
    assert response.status_code == 200
    assert response.headers["X-Correlation-ID"] == "trace-123"


def test_middleware_generates_a_correlation_id_when_absent() -> None:
    """Without an inbound header a fresh UUID is attached to state and response."""
    app = FastAPI()
    captured: dict[str, str] = {}

    @app.get("/ping")
    def _ping(request: Request) -> dict[str, bool]:
        captured["corr_id"] = request.state.correlation_id
        return {"ok": True}

    app.add_middleware(CorrelationIdMiddleware)
    response = TestClient(app).get("/ping")
    assert response.status_code == 200
    assert response.headers["X-Correlation-ID"] == captured["corr_id"]
    assert captured["corr_id"]
