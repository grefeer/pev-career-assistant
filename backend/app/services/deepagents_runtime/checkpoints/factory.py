"""Checkpointer factory: Redis for execution, in-memory for unit tests.

``redis`` uses RedisSaver (AOF-persistent; the production backend, enforced
by Settings.validate_production_settings).  ``sqlite`` maps to
InMemorySaver so unit suites run without infrastructure (spec §6.1).
"""

from __future__ import annotations

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.redis import RedisSaver

from backend.app.config import Settings


def create_checkpointer(settings: Settings) -> BaseCheckpointSaver:
    """Return the run-state saver for the configured checkpoint backend."""
    if settings.checkpoint_backend == "redis":
        saver = RedisSaver(redis_url=settings.redis_url)
        saver.setup()
        return saver
    return InMemorySaver()
