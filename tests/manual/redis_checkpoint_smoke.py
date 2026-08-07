"""Live smoke: langgraph-checkpoint-redis 0.5.0 round-trip (spec section 6.3.2).

Confirms RedisSaver round-trips a checkpoint through the real Redis (AOF on).
Skips (exit 0) when Redis is unreachable; exits 1 when the round-trip fails.
Run with the docker-compose stack up:

    .venv/Scripts/python.exe -m tests.manual.redis_checkpoint_smoke
"""

from __future__ import annotations

import asyncio
import sys

from langgraph.checkpoint.redis import RedisSaver

from backend.app.config import get_settings


def _checkpoint(thread_id: str) -> tuple[dict, dict]:
    checkpoint = {
        "v": 1,
        "ts": 1723000000.0,
        "id": f"smoke-{thread_id}",
        "channel_values": {"run_status": "running", "budget": {"max_agent_turns": 12}},
        "channel_versions": {},
        "versions_seen": {},
        "pending_sends": [],
    }
    # 0.5.0 requires checkpoint_ns in the configurable (langgraph-checkpoint
    # 4.1.1's put pops it); thread cleanup uses delete_thread, not put(None).
    config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    return checkpoint, config


def _sync_round_trip(saver) -> bool:
    checkpoint, config = _checkpoint("smoke-thread")
    saver.put(config, checkpoint, {}, {})
    loaded = saver.get_tuple(config)
    saver.delete_thread("smoke-thread")  # remove the smoke thread
    return (
        loaded is not None
        and loaded.checkpoint["channel_values"]["run_status"] == "running"
    )


async def _async_round_trip(saver) -> bool:
    checkpoint, config = _checkpoint("smoke-async")
    await saver.aput(config, checkpoint, {}, {})
    loaded = await saver.aget_tuple(config)
    await saver.adelete_thread("smoke-async")  # remove the smoke thread
    return (
        loaded is not None
        and loaded.checkpoint["channel_values"]["run_status"] == "running"
    )


def main() -> int:
    settings = get_settings()
    try:
        saver = RedisSaver(redis_url=settings.redis_url)
    except Exception as exc:  # noqa: BLE001 - Redis may simply be down
        print(
            "SKIP: Redis unreachable (%s); start the docker-compose stack first"
            % type(exc).__name__
        )
        return 0
    try:
        ok = _sync_round_trip(saver)
        api = "sync put/get_tuple"
    except (AttributeError, TypeError, NotImplementedError):
        # A saver without the sync API -> async aput/aget_tuple still works
        ok = asyncio.run(_async_round_trip(saver))
        api = "async aput/aget_tuple"
    except Exception as exc:  # noqa: BLE001 - a broken round-trip is a FAIL
        print("FAIL: RedisSaver round-trip error (%s)" % type(exc).__name__)
        return 1
    print(
        "PASS: RedisSaver %s round-trip OK" % api
        if ok
        else "FAIL: RedisSaver %s round-trip mismatch" % api
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
