from __future__ import annotations

import os
from typing import Any

from fastapi.testclient import TestClient

os.environ.setdefault("APP_AUTH_SECRET", "test-secret-with-at-least-32-characters")
os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")
os.environ.setdefault(
    "OBJECT_ENCRYPTION_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
)

from backend.app.main import create_app
from tests.conftest import settings_override


class FailingSession:
    def __enter__(self) -> "FailingSession":
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def execute(self, statement: Any) -> None:
        raise RuntimeError("mysql://root:password@private-db:3306")


class FailingRedis:
    def ping(self) -> None:
        raise RuntimeError("redis://:password@private-redis:6379")


class FailingS3Client:
    def head_bucket(self, **kwargs: Any) -> None:
        raise RuntimeError("secret access key at http://private-minio:9000")


class FailingBlobStore:
    _client = FailingS3Client()
    _bucket = "private-bucket"


class TrackingBlobStore:
    def __init__(self) -> None:
        self.ensure_calls = 0
        self.close_calls = 0

    def ensure_bucket(self) -> None:
        self.ensure_calls += 1

    def close(self) -> None:
        self.close_calls += 1


class HealthySession(FailingSession):
    def execute(self, statement: Any) -> None:
        return None


class HealthyRedis:
    def ping(self) -> bool:
        return True


class HealthyS3Client:
    def head_bucket(self, **kwargs: Any) -> None:
        return None


class HealthyBlobStore:
    _client = HealthyS3Client()
    _bucket = "career-assistant"


def _client_with_dependencies(
    *, session_factory: Any, redis_client: Any, blob_store: Any
) -> TestClient:
    app = create_app(settings_override(), graph=object())
    app.state.session_factory = session_factory
    app.state.redis = redis_client
    app.state.blob_store = blob_store
    return TestClient(app)


def test_live_does_not_depend_on_external_services() -> None:
    client = _client_with_dependencies(
        session_factory=lambda: FailingSession(),
        redis_client=FailingRedis(),
        blob_store=FailingBlobStore(),
    )

    response = client.get("/api/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_reports_each_dependency_without_secrets() -> None:
    client = _client_with_dependencies(
        session_factory=lambda: FailingSession(),
        redis_client=FailingRedis(),
        blob_store=FailingBlobStore(),
    )

    response = client.get("/api/health/ready")

    assert response.status_code == 503
    assert response.json()["detail"] == {
        "status": "not_ready",
        "dependencies": {"mysql": "down", "redis": "down", "object_store": "down"},
    }
    response_text = response.text.lower()
    for forbidden in ("password", "private-db", "root", "secret", "private-minio"):
        assert forbidden not in response_text


def test_lifespan_ensures_injected_bucket_without_taking_ownership() -> None:
    blob_store = TrackingBlobStore()
    app = create_app(settings_override(), graph=object(), blob_store=blob_store)
    app.state.redis = FailingRedis()

    with TestClient(app):
        assert blob_store.ensure_calls == 1
        assert app.state.blob_store is blob_store

    assert app.state.blob_store is blob_store
    assert blob_store.close_calls == 0


def test_ready_reports_all_dependencies_up() -> None:
    client = _client_with_dependencies(
        session_factory=lambda: HealthySession(),
        redis_client=HealthyRedis(),
        blob_store=HealthyBlobStore(),
    )

    response = client.get("/api/health/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "dependencies": {"mysql": "up", "redis": "up", "object_store": "up"},
    }
