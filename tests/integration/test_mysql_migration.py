import os
import subprocess

import pytest
from sqlalchemy import create_engine, inspect


@pytest.mark.skipif(
    "TEST_MYSQL_URL" not in os.environ, reason="requires TEST_MYSQL_URL"
)
def test_mysql_migration_upgrade_and_downgrade() -> None:
    env = {**os.environ, "DATABASE_URL": os.environ["TEST_MYSQL_URL"]}
    subprocess.run(["alembic", "upgrade", "head"], check=True, env=env)
    tables = set(inspect(create_engine(env["DATABASE_URL"])).get_table_names())
    assert {
        "users",
        "analysis_sessions",
        "devices",
        "application_tasks",
        "application_events",
        "audit_events",
    } <= tables
    subprocess.run(["alembic", "downgrade", "base"], check=True, env=env)
