import os
import subprocess
import sys
from pathlib import Path

import pytest
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Engine, create_engine, inspect


BUSINESS_TABLES = {
    "users",
    "analysis_sessions",
    "devices",
    "application_tasks",
    "application_events",
    "audit_events",
}
ALEMBIC_TABLES = {"alembic_version"}
HEAD_REVISION = "20260714_0001"


def _alembic_env(database_url: str) -> dict[str, str]:
    return {
        **os.environ,
        "APP_AUTH_SECRET": "test-secret-with-at-least-32-characters",
        "DATABASE_URL": database_url,
        "OBJECT_ENCRYPTION_KEY": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        "REDIS_URL": "redis://localhost:6379/15",
    }


def _run_alembic(*args: str, env: dict[str, str]) -> None:
    subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        check=True,
        env=env,
        capture_output=True,
        text=True,
    )


def _tables(engine: Engine) -> set[str]:
    return set(inspect(engine).get_table_names())


def _current_revision(engine: Engine) -> str | None:
    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def test_alembic_offline_accepts_percent_encoded_database_url() -> None:
    env = _alembic_env("mysql+pymysql://migration_user:p%40ss@127.0.0.1/migration_test")
    _run_alembic("upgrade", "head", "--sql", env=env)


def test_alembic_online_accepts_percent_encoded_database_url(
    tmp_path: Path,
) -> None:
    database_path = (tmp_path / "encoded%40.db").as_posix()
    env = _alembic_env(f"sqlite+pysqlite:///{database_path}")
    _run_alembic("current", env=env)


@pytest.mark.skipif(
    "TEST_MYSQL_URL" not in os.environ, reason="requires TEST_MYSQL_URL"
)
def test_mysql_migration_upgrade_and_downgrade() -> None:
    env = _alembic_env(os.environ["TEST_MYSQL_URL"])
    engine = create_engine(env["DATABASE_URL"])

    try:
        _run_alembic("downgrade", "base", env=env)
        assert _tables(engine) <= ALEMBIC_TABLES
        assert _current_revision(engine) is None

        try:
            _run_alembic("upgrade", "head", env=env)
            assert _tables(engine) == BUSINESS_TABLES | ALEMBIC_TABLES
            assert _current_revision(engine) == HEAD_REVISION
        finally:
            _run_alembic("downgrade", "base", env=env)
            assert _tables(engine) <= ALEMBIC_TABLES
            assert _current_revision(engine) is None
    finally:
        engine.dispose()
