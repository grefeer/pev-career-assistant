"""Shared pytest configuration for the backend test suite."""

import os
from typing import Any

import pytest

from backend.app.config import Settings
from tests.integration.job_sync_gate_safety import (
    DESTRUCTIVE_MYSQL_OPT_IN_ENV,
    MYSQL_TEST_URL_ENV,
    require_dedicated_mysql_test_database,
)


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
    """Build test settings with deterministic, service-free defaults."""
    settings_values: dict[str, Any] = {
        "app_env": "test",
        "app_auth_secret": "test-secret-with-at-least-32-characters",
        "object_encryption_key": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        "database_url": "sqlite+pysqlite:///:memory:",
        "redis_url": "redis://localhost:6379/15",
        "checkpoint_backend": "sqlite",
    }
    settings_values.update(values)
    return Settings(**settings_values)
