from __future__ import annotations

import os
from contextlib import contextmanager
from collections.abc import Iterator
from typing import Any

from fastapi.testclient import TestClient
import pytest

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
    def check_bucket(self) -> None:
        FailingS3Client().head_bucket(Bucket="private-bucket")


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
    def check_bucket(self) -> None:
        HealthyS3Client().head_bucket(Bucket="career-assistant")


class ClosingRedis(HealthyRedis):
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class FailingEnsureS3Client:
    def __init__(self) -> None:
        self.close_calls = 0

    def head_bucket(self, **kwargs: Any) -> None:
        raise RuntimeError("object store unavailable")

    def close(self) -> None:
        self.close_calls += 1


class InjectedFailingBlobStore:
    def __init__(self) -> None:
        self.ensure_calls = 0
        self.close_calls = 0

    def ensure_bucket(self) -> None:
        self.ensure_calls += 1
        raise RuntimeError("object store unavailable")

    def close(self) -> None:
        self.close_calls += 1


class TrackingReadinessEngine:
    def __init__(self) -> None:
        self.dispose_calls = 0

    def dispose(self) -> None:
        self.dispose_calls += 1


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


def test_ensure_bucket_failure_closes_all_lifespan_owned_resources(
    monkeypatch: Any,
) -> None:
    redis_client = ClosingRedis()
    s3_client = FailingEnsureS3Client()
    readiness_engine = TrackingReadinessEngine()
    checkpointer_closes: list[bool] = []

    @contextmanager
    def tracking_checkpointer(settings: Any) -> Iterator[object]:
        try:
            yield object()
        finally:
            checkpointer_closes.append(True)

    monkeypatch.setattr(
        "backend.app.main.checkpointer_context", tracking_checkpointer
    )
    monkeypatch.setattr("backend.app.main.build_graph", lambda checkpointer: object())
    monkeypatch.setattr(
        "backend.app.main.redis.Redis.from_url", lambda *args, **kwargs: redis_client
    )
    monkeypatch.setattr(
        "backend.app.main.boto3.client", lambda *args, **kwargs: s3_client
    )
    monkeypatch.setattr(
        "backend.app.db.session.build_readiness_engine",
        lambda settings: readiness_engine,
    )
    app = create_app(settings_override(app_env="development"))

    with pytest.raises(RuntimeError, match="object store unavailable"):
        with TestClient(app):
            pass

    assert redis_client.close_calls == 1
    assert s3_client.close_calls == 1
    assert readiness_engine.dispose_calls == 1
    assert checkpointer_closes == [True]
    assert not hasattr(app.state, "graph")
    assert not hasattr(app.state, "redis")
    assert not hasattr(app.state, "blob_store")
    assert not hasattr(app.state, "session_factory")


def test_ensure_bucket_failure_does_not_close_preinjected_resources(
    monkeypatch: Any,
) -> None:
    graph = object()
    redis_client = ClosingRedis()
    blob_store = InjectedFailingBlobStore()
    app = create_app(settings_override(), graph=graph, blob_store=blob_store)
    app.state.redis = redis_client
    injected_session_factory = object()
    app.state.session_factory = injected_session_factory
    monkeypatch.setattr(
        "backend.app.db.session.build_readiness_engine",
        lambda settings: (_ for _ in ()).throw(
            AssertionError("injected session factory must be preserved")
        ),
    )

    with pytest.raises(RuntimeError, match="object store unavailable"):
        with TestClient(app):
            pass

    assert blob_store.ensure_calls == 1
    assert blob_store.close_calls == 0
    assert redis_client.close_calls == 0
    assert app.state.graph is graph
    assert app.state.redis is redis_client
    assert app.state.blob_store is blob_store
    assert app.state.session_factory is injected_session_factory
