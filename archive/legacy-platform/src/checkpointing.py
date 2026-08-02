from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.redis import RedisSaver
from langgraph.checkpoint.sqlite import SqliteSaver

from backend.app.config import Settings


@contextmanager
def checkpointer_context(settings: Settings) -> Iterator[BaseCheckpointSaver]:
    if settings.checkpoint_backend == "redis":
        connection_args: dict[str, str] = {}
        password = os.environ.get("REDIS_PASSWORD")
        if password:
            connection_args["password"] = password
        with RedisSaver.from_conn_string(
            settings.redis_url,
            connection_args=connection_args or None,
        ) as saver:
            saver.setup()
            yield saver
        return

    settings.checkpoint_sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        settings.checkpoint_sqlite_path,
        check_same_thread=False,
    )
    try:
        yield SqliteSaver(connection)
    finally:
        connection.close()
