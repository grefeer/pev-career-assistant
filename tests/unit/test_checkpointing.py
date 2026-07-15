from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from backend.app.config import Settings
from src.checkpointing import checkpointer_context
from src.graph import build_graph


class TrackingConnection(sqlite3.Connection):
    closed = False

    def close(self) -> None:
        self.closed = True
        super().close()


class TrackingRedis:
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


def _settings(path: Path) -> Settings:
    return Settings(
        app_env="test",
        app_auth_secret="test-secret-with-at-least-32-characters",
        object_encryption_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        database_url="sqlite+pysqlite:///:memory:",
        redis_url="redis://localhost:6379/15",
        checkpoint_backend="sqlite",
        checkpoint_sqlite_path=path,
    )


def _load_create_app(monkeypatch: Any) -> Any:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("APP_AUTH_SECRET", "test-secret-with-at-least-32-characters")
    monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:///:memory:")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/15")
    monkeypatch.setenv(
        "OBJECT_ENCRYPTION_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
    )
    from backend.app.main import create_app

    return create_app


def test_build_graph_uses_injected_checkpointer() -> None:
    saver = InMemorySaver()

    graph = build_graph(checkpointer=saver)

    assert graph.checkpointer is saver


def test_sqlite_checkpoint_context_closes_connection(
    tmp_path: Path, monkeypatch: Any
) -> None:
    settings = _settings(tmp_path / "nested" / "cp.sqlite")
    connection: TrackingConnection | None = None
    real_connect = sqlite3.connect

    def connect(*args: Any, **kwargs: Any) -> TrackingConnection:
        nonlocal connection
        kwargs["factory"] = TrackingConnection
        connection = real_connect(*args, **kwargs)
        return connection

    monkeypatch.setattr("src.checkpointing.sqlite3.connect", connect)

    with checkpointer_context(settings) as saver:
        saver.get_tuple({"configurable": {"thread_id": "t1", "checkpoint_ns": ""}})
        assert connection is not None
        assert not connection.closed

    assert settings.checkpoint_sqlite_path.exists()
    assert connection is not None
    assert connection.closed


def test_lifespan_recreates_owned_graph_and_redis_on_each_startup(
    tmp_path: Path, monkeypatch: Any
) -> None:
    settings = _settings(tmp_path / "lifespan.sqlite")
    redis_clients: list[TrackingRedis] = []

    def build_redis(*args: Any, **kwargs: Any) -> TrackingRedis:
        client = TrackingRedis()
        redis_clients.append(client)
        return client

    create_app = _load_create_app(monkeypatch)
    monkeypatch.setattr("backend.app.main.redis.Redis.from_url", build_redis)
    monkeypatch.setattr(
        "src.graph.supervisor_decide",
        lambda payload: {"next_step": "finish", "reason": "test"},
    )
    app = create_app(settings)
    graphs: list[Any] = []

    for _ in range(2):
        with TestClient(app):
            graph = app.state.graph
            graphs.append(graph)
            config = {"configurable": {"thread_id": f"lifespan-{uuid4()}"}}
            assert graph.get_state(config).values == {}
            result = graph.invoke({"final_report": "already complete"}, config=config)
            assert result["final_report"] == "already complete"

    assert graphs[0] is not graphs[1]
    assert graphs[0].checkpointer is not graphs[1].checkpointer
    assert [client.close_calls for client in redis_clients] == [1, 1]
    assert not hasattr(app.state, "graph")
    assert not hasattr(app.state, "redis")


def test_lifespan_preserves_injected_graph_and_redis(
    tmp_path: Path, monkeypatch: Any
) -> None:
    graph = object()
    injected_redis = TrackingRedis()
    create_app = _load_create_app(monkeypatch)
    app = create_app(_settings(tmp_path / "injected.sqlite"), graph=graph)
    app.state.redis = injected_redis

    def unexpected_redis(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("lifespan must not replace an injected Redis client")

    monkeypatch.setattr("backend.app.main.redis.Redis.from_url", unexpected_redis)

    with TestClient(app):
        assert app.state.graph is graph
        assert app.state.redis is injected_redis

    assert app.state.graph is graph
    assert app.state.redis is injected_redis
    assert injected_redis.close_calls == 0
