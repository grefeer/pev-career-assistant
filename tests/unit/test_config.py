from pydantic import ValidationError
import pytest

from backend.app.config import Settings
from tests.conftest import settings_override


def test_production_rejects_default_secrets() -> None:
    with pytest.raises(ValidationError, match="app_auth_secret|APP_AUTH_SECRET"):
        Settings(
            app_env="production",
            app_auth_secret="replace-with-your-own-secret",
            object_encryption_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
            database_url="mysql+pymysql://app:app@mysql:3306/career_assistant",
            redis_url="redis://redis:6379/0",
            checkpoint_backend="redis",
        )


def test_production_rejects_env_example_auth_secret() -> None:
    with pytest.raises(ValidationError, match="app_auth_secret|APP_AUTH_SECRET"):
        Settings(
            app_env="production",
            app_auth_secret="replace-with-a-random-secret-of-at-least-32-characters",
            object_encryption_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
            database_url="mysql+pymysql://app:app@mysql:3306/career_assistant",
            redis_url="redis://redis:6379/0",
            checkpoint_backend="redis",
        )


def test_test_environment_accepts_sqlite_and_memory_dependencies() -> None:
    settings = Settings(
        app_env="test",
        app_auth_secret="test-secret-with-at-least-32-characters",
        object_encryption_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        database_url="sqlite+pysqlite:///:memory:",
        redis_url="redis://localhost:6379/15",
        checkpoint_backend="sqlite",
    )
    assert settings.is_production is False
    assert settings.jwt_audience == "career-assistant-web"


@pytest.mark.parametrize("value", ["${MINIO_ROOT_USER}", "${MINIO_ROOT_PASSWORD}", "replace-with-real-key", "changeme-now"])
def test_production_rejects_object_credential_templates(value: str) -> None:
    with pytest.raises(ValidationError, match="OBJECT_STORE"):
        Settings(
            app_env="production", app_auth_secret="x" * 32,
            object_encryption_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
            database_url="mysql+pymysql://root@mysql/db", redis_url="redis://redis/0",
            checkpoint_backend="redis",
            rate_limit_hmac_secret="rate-limit-secret-with-at-least-32-chars",
            trusted_proxy_cidrs="172.16.0.0/12",
            object_store_access_key=value,
            object_store_secret_key="safe-production-object-secret",
        )


def test_production_requires_rate_limit_hmac_secret() -> None:
    with pytest.raises(ValidationError, match="RATE_LIMIT_HMAC_SECRET"):
        Settings(
            app_env="production",
            app_auth_secret="x" * 32,
            object_encryption_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
            database_url="mysql+pymysql://root@mysql/db",
            redis_url="redis://redis/0",
            checkpoint_backend="redis",
            trusted_proxy_cidrs="172.16.0.0/12",
            object_store_access_key="safe-production-object-key",
            object_store_secret_key="safe-production-object-secret",
        )


def test_production_rejects_trust_all_proxy_cidr() -> None:
    with pytest.raises(ValidationError, match="TRUSTED_PROXY_CIDRS"):
        Settings(
            app_env="production",
            app_auth_secret="x" * 32,
            object_encryption_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
            database_url="mysql+pymysql://root@mysql/db",
            redis_url="redis://redis/0",
            checkpoint_backend="redis",
            rate_limit_hmac_secret="rate-limit-secret-with-at-least-32-chars",
            trusted_proxy_cidrs="0.0.0.0/0",
            object_store_access_key="safe-production-object-key",
            object_store_secret_key="safe-production-object-secret",
        )


def test_settings_override_uses_test_defaults_and_applies_values() -> None:
    settings = settings_override(jwt_audience="test-client")

    assert settings.app_env == "test"
    assert settings.database_url == "sqlite+pysqlite:///:memory:"
    assert settings.checkpoint_backend == "sqlite"
    assert settings.jwt_audience == "test-client"


