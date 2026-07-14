from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver

from backend.app.config import Settings
from src.checkpointing import checkpointer_context
from src.graph import build_graph


class TrackingConnection(sqlite3.Connection):
    closed = False

    def close(self) -> None:
        self.closed = True
        super().close()


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
