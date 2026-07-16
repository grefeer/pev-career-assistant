from __future__ import annotations

from collections.abc import Iterator, Mapping
import os
from pathlib import Path
import subprocess
import sys

import pytest

from tests.integration.job_sync_gate_safety import (
    DESTRUCTIVE_MYSQL_OPT_IN_ENV,
    MYSQL_TEST_URL_ENV,
    require_dedicated_mysql_test_database,
)


class _UrlReadTrap(Mapping[str, str]):
    """Prove the guard checks opt-in before attempting to load a DB URL."""

    def __getitem__(self, key: str) -> str:
        if key == MYSQL_TEST_URL_ENV:
            raise AssertionError("database URL was read before destructive opt-in")
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return iter(())

    def __len__(self) -> int:
        return 0


def test_database_guard_rejects_missing_opt_in_before_reading_url() -> None:
    with pytest.raises(ValueError, match=DESTRUCTIVE_MYSQL_OPT_IN_ENV):
        require_dedicated_mysql_test_database(_UrlReadTrap())


@pytest.mark.parametrize("opt_in", ["", "0", "true", "yes", " 1 "])
def test_database_guard_requires_exact_destructive_opt_in(opt_in: str) -> None:
    environ = {
        DESTRUCTIVE_MYSQL_OPT_IN_ENV: opt_in,
        MYSQL_TEST_URL_ENV: "mysql+pymysql://root@localhost/career_assistant_test",
    }

    with pytest.raises(ValueError, match=DESTRUCTIVE_MYSQL_OPT_IN_ENV):
        require_dedicated_mysql_test_database(environ)


@pytest.mark.parametrize("database_url", ["", "   "])
def test_database_guard_rejects_empty_mysql_url(database_url: str) -> None:
    environ = {
        DESTRUCTIVE_MYSQL_OPT_IN_ENV: "1",
        MYSQL_TEST_URL_ENV: database_url,
    }

    with pytest.raises(ValueError, match=MYSQL_TEST_URL_ENV):
        require_dedicated_mysql_test_database(environ)


@pytest.mark.parametrize(
    "database_url",
    [
        "sqlite+pysqlite:///career_assistant_test",
        "postgresql+psycopg://root@localhost/career_assistant_test",
        "mysql+pymysql://root@localhost/career_assistant",
        "mysql+pymysql://root@localhost/mysql",
        "mysql+pymysql://root@localhost/",
    ],
)
def test_database_guard_rejects_non_isolated_mysql_database(
    database_url: str,
) -> None:
    environ = {
        DESTRUCTIVE_MYSQL_OPT_IN_ENV: "1",
        MYSQL_TEST_URL_ENV: database_url,
    }

    with pytest.raises(ValueError, match="MySQL.*_test"):
        require_dedicated_mysql_test_database(environ)


def test_database_guard_returns_dedicated_mysql_test_url() -> None:
    database_url = "mysql+pymysql://root@localhost/career_assistant_test"

    guarded_url = require_dedicated_mysql_test_database(
        {
            DESTRUCTIVE_MYSQL_OPT_IN_ENV: "1",
            MYSQL_TEST_URL_ENV: database_url,
        }
    )

    assert guarded_url == database_url


def test_database_guard_remains_fail_closed_under_python_optimization() -> None:
    environment = {
        **os.environ,
        DESTRUCTIVE_MYSQL_OPT_IN_ENV: "1",
        MYSQL_TEST_URL_ENV: "mysql+pymysql://root@localhost/career_assistant",
    }
    script = """
import os
from tests.integration.job_sync_gate_safety import require_dedicated_mysql_test_database

try:
    require_dedicated_mysql_test_database(os.environ)
except ValueError:
    raise SystemExit(0)
raise SystemExit(1)
"""

    completed = subprocess.run(
        [sys.executable, "-O", "-c", script],
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert completed.stdout == ""
    assert completed.stderr == ""
