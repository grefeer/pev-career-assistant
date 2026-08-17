"""Shared pytest configuration for the backend test suite."""

import os
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.config import Settings
from backend.app.db.base import Base
from tests.integration.job_sync_gate_safety import (
    DESTRUCTIVE_MYSQL_OPT_IN_ENV,
    MYSQL_TEST_URL_ENV,
    require_dedicated_mysql_test_database,
)


@pytest.fixture()
def db_session() -> Session:
    """Fresh in-memory SQLite session with all ORM tables created.

    Used by personalized-discovery model / repository / service unit tests.
    ``Base.metadata.create_all`` mirrors the models without needing Alembic.
    """
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        # SQLite's defaults are too lax for the unique/RESTRICT semantics the
        # personalized-discovery tests assert; enable foreign keys + a modern
        # isolation so IntegrityError surfaces on a real constraint violation.
        connect_args={"check_same_thread": False},
    )

    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _enable_sqlite_fk(dbapi_conn, _record):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(engine)
    session = Session(engine)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture(scope="session")

def destructive_mysql_url() -> str:
    """Load a destructive-test URL only after explicit, fail-closed opt-in."""
    if DESTRUCTIVE_MYSQL_OPT_IN_ENV not in os.environ:
        pytest.skip(f"requires {DESTRUCTIVE_MYSQL_OPT_IN_ENV}=1")
    if os.environ.get(DESTRUCTIVE_MYSQL_OPT_IN_ENV) != "1":
        pytest.fail(
            f"{DESTRUCTIVE_MYSQL_OPT_IN_ENV} must equal 1 for destructive tests",
            pytrace=False,
        )
    if not os.environ.get(MYSQL_TEST_URL_ENV, "").strip():
        pytest.skip(f"requires non-empty {MYSQL_TEST_URL_ENV}")
    try:
        return require_dedicated_mysql_test_database(os.environ)
    except ValueError as error:
        pytest.fail(str(error), pytrace=False)


def settings_override(**values: Any) -> Settings:
    """Build test settings with deterministic, service-free defaults.

    Hermetic by construction: ``_env_file=None`` silences the project ``.env``
    file source, and the PEV budgets are pinned to schema defaults so they
    survive ``os.environ`` pollution. ``main.py`` imports call
    ``load_project_env()`` -> ``load_dotenv()``, which copies every ``.env``
    var (including ``AGENT_HARNESS_MAX_*``) into ``os.environ``; the
    ``env_settings`` source reads that and is NOT disabled by ``_env_file``.
    Init kwargs outrank every source, so pinning here wins regardless.
    """
    settings_values: dict[str, Any] = {
        "app_env": "test",
        "app_auth_secret": "test-secret-with-at-least-32-characters",
        "object_encryption_key": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        "database_url": "sqlite+pysqlite:///:memory:",
        "redis_url": "redis://localhost:6379/15",
        # Pin PEV budgets to schema defaults so route/service assertions stay
        # deterministic even when a prior test imported ``main`` and polluted
        # ``os.environ`` with a developer's loosened ``.env`` values.
        "agent_harness_max_agent_turns": 12,
        "agent_harness_max_tool_calls": 24,
        "agent_harness_max_replans": 2,
        "agent_harness_max_wall_clock_seconds": 300,
    }
    settings_values.update(values)
    return Settings(_env_file=None, **settings_values)
