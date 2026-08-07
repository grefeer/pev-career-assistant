from __future__ import annotations

from langgraph.checkpoint.memory import InMemorySaver

from backend.app.services.deepagents_runtime.checkpoints.factory import (
    create_checkpointer,
)
from tests.conftest import settings_override


def test_sqlite_backend_maps_to_inmemory_saver() -> None:
    settings = settings_override(checkpoint_backend="sqlite")
    assert isinstance(create_checkpointer(settings), InMemorySaver)


def test_redis_backend_constructs_redis_saver(monkeypatch) -> None:
    created = {}

    class FakeRedisSaver:
        def __init__(self, redis_url: str) -> None:
            created["url"] = redis_url
            self.setup_called = False

        def setup(self) -> None:
            self.setup_called = True

    monkeypatch.setattr(
        "backend.app.services.deepagents_runtime.checkpoints.factory.RedisSaver",
        FakeRedisSaver,
    )
    settings = settings_override(
        checkpoint_backend="redis", redis_url="redis://localhost:6379/0"
    )
    saver = create_checkpointer(settings)
    assert isinstance(saver, FakeRedisSaver)
    assert saver.setup_called
    assert created["url"] == "redis://localhost:6379/0"
