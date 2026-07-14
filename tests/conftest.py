"""Shared pytest configuration for the backend test suite."""

from typing import Any

from backend.app.config import Settings


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
