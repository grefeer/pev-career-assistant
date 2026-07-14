from __future__ import annotations

import os
from typing import TypedDict
from uuid import uuid4

import pytest
import redis
from langgraph.graph import END, START, StateGraph

from backend.app.config import Settings
from src.checkpointing import checkpointer_context


class CounterState(TypedDict):
    count: int


def _build_counter_graph(checkpointer: object):
    builder = StateGraph(CounterState)
    builder.add_node("increment", lambda state: {"count": state["count"] + 1})
    builder.add_edge(START, "increment")
    builder.add_edge("increment", END)
    return builder.compile(checkpointer=checkpointer)


def test_redis_checkpoint_survives_across_graph_instances() -> None:
    redis_url = os.environ.get("TEST_REDIS_URL")
    if not redis_url:
        pytest.skip("TEST_REDIS_URL is required for the Redis 8 integration test")

    client = redis.Redis.from_url(redis_url, decode_responses=True)
    thread_id = f"task8-{uuid4()}"
    config = {"configurable": {"thread_id": thread_id}}
    settings = Settings(
        app_env="test",
        app_auth_secret="test-secret-with-at-least-32-characters",
        object_encryption_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        database_url="sqlite+pysqlite:///:memory:",
        redis_url=redis_url,
        checkpoint_backend="redis",
    )

    try:
        assert client.execute_command("JSON.SET", f"{thread_id}:probe", "$", "{}") == "OK"
        assert isinstance(client.execute_command("FT._LIST"), list)

        with checkpointer_context(settings) as saver:
            first_graph = _build_counter_graph(saver)
            assert first_graph.invoke({"count": 0}, config=config)["count"] == 1

            second_graph = _build_counter_graph(saver)
            restored = second_graph.get_state(config)
            assert restored.values["count"] == 1
            assert second_graph.invoke(restored.values, config=config)["count"] == 2

        checkpoint_keys = list(client.scan_iter(match=f"*{thread_id}*"))
        assert checkpoint_keys
        assert all(client.ttl(key) == -1 for key in checkpoint_keys)
    finally:
        owned_keys = list(client.scan_iter(match=f"*{thread_id}*"))
        if owned_keys:
            client.delete(*owned_keys)
        client.close()
