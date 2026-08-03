"""HTTP contract tests for health and metrics routes."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.api.routes import health as health_routes
from backend.app.api.routes import metrics as metrics_routes
from tests.conftest import settings_override


def _build_app(*, redis: Any = None, session_factory: Any = None,
               blob_store: Any = None) -> FastAPI:
    app = FastAPI()
    app.state.settings = settings_override()
    app.state.redis = redis
    app.state.session_factory = session_factory
    app.state.blob_store = blob_store
    app.include_router(health_routes.router, prefix="/api")
    app.include_router(metrics_routes.router, prefix="/api")
    return app


def test_health_and_live_return_ok() -> None:
    app = _build_app()
    client = TestClient(app)

    assert client.get("/api/health").json() == {"status": "ok"}
    assert client.get("/api/health/live").json() == {"status": "ok"}


def test_ready_returns_ok_when_all_dependencies_up() -> None:
    redis = MagicMock()
    redis.ping.return_value = True
    blob_store = MagicMock()

    session_factory = MagicMock()
    db = MagicMock()
    session_factory.return_value.__enter__ = MagicMock(return_value=db)
    session_factory.return_value.__exit__ = MagicMock(return_value=False)

    app = _build_app(redis=redis, session_factory=session_factory, blob_store=blob_store)
    response = TestClient(app).get("/api/health/ready")

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "status": "ready",
        "dependencies": {
            "mysql": "up",
            "redis": "up",
            "object_store": "up",
        },
    }


def test_ready_returns_503_when_mysql_down() -> None:
    redis = MagicMock()
    redis.ping.return_value = True
    blob_store = MagicMock()

    def boom() -> Any:
        raise RuntimeError("no session factory")

    # session_factory is None -> _mysql_is_up returns False
    app = _build_app(redis=redis, session_factory=None, blob_store=blob_store)
    response = TestClient(app).get("/api/health/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["detail"]["status"] == "not_ready"
    assert body["detail"]["dependencies"]["mysql"] == "down"
    assert body["detail"]["dependencies"]["redis"] == "up"
    assert body["detail"]["dependencies"]["object_store"] == "up"


def test_ready_returns_503_when_redis_down() -> None:
    redis = MagicMock()
    redis.ping.side_effect = RuntimeError("redis down")
    blob_store = MagicMock()

    session_factory = MagicMock()
    db = MagicMock()
    session_factory.return_value.__enter__ = MagicMock(return_value=db)
    session_factory.return_value.__exit__ = MagicMock(return_value=False)

    app = _build_app(redis=redis, session_factory=session_factory, blob_store=blob_store)
    response = TestClient(app).get("/api/health/ready")

    assert response.status_code == 503
    assert response.json()["detail"]["dependencies"]["redis"] == "down"


def test_ready_returns_503_when_object_store_down() -> None:
    redis = MagicMock()
    redis.ping.return_value = True
    blob_store = MagicMock()
    blob_store.check_bucket.side_effect = RuntimeError("minio down")

    session_factory = MagicMock()
    db = MagicMock()
    session_factory.return_value.__enter__ = MagicMock(return_value=db)
    session_factory.return_value.__exit__ = MagicMock(return_value=False)

    app = _build_app(redis=redis, session_factory=session_factory, blob_store=blob_store)
    response = TestClient(app).get("/api/health/ready")

    assert response.status_code == 503
    assert response.json()["detail"]["dependencies"]["object_store"] == "down"


def test_metrics_reports_ready_when_all_dependencies_up() -> None:
    redis = MagicMock()
    redis.ping.return_value = True
    blob_store = MagicMock()

    session_factory = MagicMock()
    db = MagicMock()
    session_factory.return_value.__enter__ = MagicMock(return_value=db)
    session_factory.return_value.__exit__ = MagicMock(return_value=False)

    app = _build_app(redis=redis, session_factory=session_factory, blob_store=blob_store)
    response = TestClient(app).get("/api/metrics")

    assert response.status_code == 200
    body = response.text
    assert "career_assistant_app_info" in body
    assert 'app_env="test"' in body
    assert "career_assistant_ready 1" in body
    assert 'dependency="mysql"} 1' in body
    assert 'dependency="redis"} 1' in body
    assert 'dependency="object_store"} 1' in body


def test_metrics_reports_not_ready_when_dependencies_down() -> None:
    app = _build_app()  # all dependencies missing -> all "down"
    response = TestClient(app).get("/api/metrics")

    assert response.status_code == 200
    body = response.text
    assert "career_assistant_ready 0" in body
    assert 'dependency="mysql"} 0' in body


def test_ready_returns_503_when_mysql_query_raises() -> None:
    """A non-None session factory whose execute() raises -> mysql reported down."""
    redis = MagicMock()
    redis.ping.return_value = True
    blob_store = MagicMock()

    session_factory = MagicMock()
    db = MagicMock()
    db.execute.side_effect = RuntimeError("mysql connection lost")
    session_factory.return_value.__enter__ = MagicMock(return_value=db)
    session_factory.return_value.__exit__ = MagicMock(return_value=False)

    app = _build_app(redis=redis, session_factory=session_factory, blob_store=blob_store)
    response = TestClient(app).get("/api/health/ready")

    assert response.status_code == 503
    assert response.json()["detail"]["dependencies"]["mysql"] == "down"


def test_metrics_falls_back_to_unknown_when_health_probes_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the imported health probes raise, metrics keeps the 'unknown' defaults."""
    redis = MagicMock()
    redis.ping.return_value = True
    blob_store = MagicMock()

    session_factory = MagicMock()
    db = MagicMock()
    session_factory.return_value.__enter__ = MagicMock(return_value=db)
    session_factory.return_value.__exit__ = MagicMock(return_value=False)

    def _boom(_: Any) -> bool:
        raise RuntimeError("probe crashed")

    monkeypatch.setattr(health_routes, "_mysql_is_up", _boom)
    monkeypatch.setattr(health_routes, "_redis_is_up", _boom)
    monkeypatch.setattr(health_routes, "_object_store_is_up", _boom)

    app = _build_app(redis=redis, session_factory=session_factory, blob_store=blob_store)
    response = TestClient(app).get("/api/metrics")

    assert response.status_code == 200
    body = response.text
    # Defaults stayed "unknown" -> ready gauge is 0, dependencies report 0.
    assert "career_assistant_ready 0" in body
    assert 'dependency="mysql"} 0' in body


def test_metrics_escapes_label_values() -> None:
    app = _build_app()
    # Override settings with values needing escaping.
    app.state.settings = MagicMock()
    app.state.settings.app_env = 'pro"d\nuct\\ion'
    response = TestClient(app).get("/api/metrics")

    body = response.text
    assert 'app_env="pro\\"d\\nuct\\\\ion"' in body
