from __future__ import annotations

import os
from typing import Any

from fastapi.testclient import TestClient

os.environ.setdefault("APP_AUTH_SECRET", "test-secret-with-at-least-32-characters")
os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault(
    "OBJECT_ENCRYPTION_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
)

from backend.app.config import Settings
from backend.app.main import create_app


class TrackingRedis:
    def close(self) -> None:
        return None


class TrackingS3Client:
    def head_bucket(self, **kwargs: Any) -> None:
        return None

    def close(self) -> None:
        return None


def _settings(**values: Any) -> Settings:
    defaults = {
        "app_env": "development",
        "app_auth_secret": "test-secret-with-at-least-32-characters",
        "object_encryption_key": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        "database_url": "sqlite+pysqlite:///:memory:",
        "redis_url": "redis://localhost:6379/0",
    }
    defaults.update(values)
    return Settings(**defaults)


def test_lifespan_dependency_clients_use_configured_short_timeouts(
    monkeypatch: Any,
) -> None:
    redis_options: dict[str, Any] = {}
    s3_options: dict[str, Any] = {}

    def build_redis(*args: Any, **kwargs: Any) -> TrackingRedis:
        redis_options.update(kwargs)
        return TrackingRedis()

    def build_s3(*args: Any, **kwargs: Any) -> TrackingS3Client:
        s3_options.update(kwargs)
        return TrackingS3Client()

    monkeypatch.setattr("backend.app.main.redis.Redis.from_url", build_redis)
    monkeypatch.setattr("backend.app.main.boto3.client", build_s3)
    app = create_app(_settings(readiness_timeout_seconds=3))

    with TestClient(app):
        pass

    assert redis_options["socket_connect_timeout"] == 3
    assert redis_options["socket_timeout"] == 3
    client_config = s3_options["config"]
    assert client_config.connect_timeout == 3
    assert client_config.read_timeout == 3
    assert client_config.retries["total_max_attempts"] <= 2


def test_only_readiness_mysql_engine_uses_short_timeouts(monkeypatch: Any) -> None:
    from backend.app.db import session as session_module

    calls: list[dict[str, Any]] = []

    def capture_engine(url: str, **kwargs: Any) -> object:
        calls.append(kwargs)
        return object()

    monkeypatch.setattr(session_module, "create_engine", capture_engine)

    session_module.build_engine(
        _settings(
            database_url="mysql+pymysql://root:test@mysql/career_assistant",
            readiness_timeout_seconds=4,
        )
    )
    session_module.build_readiness_engine(
        _settings(
            database_url="mysql+pymysql://root:test@mysql/career_assistant",
            readiness_timeout_seconds=4,
        )
    )
    session_module.build_engine(_settings())
    session_module.build_readiness_engine(_settings())

    assert "connect_args" not in calls[0]
    assert calls[1]["connect_args"] == {
        "connect_timeout": 4,
        "read_timeout": 4,
        "write_timeout": 4,
    }
    assert "connect_args" not in calls[2]
    assert "connect_args" not in calls[3]
