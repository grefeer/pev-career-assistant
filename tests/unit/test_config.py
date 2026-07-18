from pydantic import ValidationError
import pytest

from backend.app.config import Settings, _literal_tencent_dotenv_values
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


def test_test_tencent_token_falls_back_to_runtime_token() -> None:
    settings = Settings(
        app_env="test",
        app_auth_secret="test-secret-with-at-least-32-characters",
        object_encryption_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        database_url="sqlite+pysqlite:///:memory:",
        redis_url="redis://localhost:6379/15",
        checkpoint_backend="sqlite",
        tencent_docs_token="runtime-token",
    )

    assert settings.effective_test_tencent_docs_token.get_secret_value() == "runtime-token"


def test_job_discovery_defaults() -> None:
    settings = Settings(
        app_env="test",
        app_auth_secret="test-secret-with-at-least-32-characters",
        object_encryption_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        database_url="sqlite+pysqlite:///:memory:",
        redis_url="redis://localhost:6379/15",
        checkpoint_backend="sqlite",
    )
    assert settings.job_discovery_enabled is False
    assert settings.job_discovery_agent_version == "1.0.0"
    assert settings.job_discovery_model == "deepseek-v4-flash"
    assert settings.job_discovery_max_pages_per_task == 20
    assert settings.job_discovery_max_candidates_per_task == 10
    assert settings.job_discovery_task_timeout_seconds == 600
    assert settings.job_discovery_browser_headless is True
    assert settings.job_discovery_ocr_enabled is False


class TestJobDiscoveryBounds:
    def test_disabled_by_default(self) -> None:
        settings = Settings(
            app_env="test",
            app_auth_secret="test-secret-with-at-least-32-characters",
            object_encryption_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
            database_url="sqlite+pysqlite:///:memory:",
            redis_url="redis://localhost:6379/15",
            checkpoint_backend="sqlite",
        )
        assert settings.job_discovery_enabled is False

    def test_max_pages_accepts_lower_bound(self) -> None:
        settings = Settings(
            app_env="test",
            app_auth_secret="test-secret-with-at-least-32-characters",
            object_encryption_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
            database_url="sqlite+pysqlite:///:memory:",
            redis_url="redis://localhost:6379/15",
            checkpoint_backend="sqlite",
            job_discovery_max_pages_per_task=1,
        )
        assert settings.job_discovery_max_pages_per_task == 1

    def test_max_pages_accepts_upper_bound(self) -> None:
        settings = Settings(
            app_env="test",
            app_auth_secret="test-secret-with-at-least-32-characters",
            object_encryption_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
            database_url="sqlite+pysqlite:///:memory:",
            redis_url="redis://localhost:6379/15",
            checkpoint_backend="sqlite",
            job_discovery_max_pages_per_task=100,
        )
        assert settings.job_discovery_max_pages_per_task == 100

    def test_max_pages_rejects_below_one(self) -> None:
        with pytest.raises(ValidationError, match="job_discovery_max_pages_per_task"):
            Settings(
                app_env="test",
                app_auth_secret="test-secret-with-at-least-32-characters",
                object_encryption_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
                database_url="sqlite+pysqlite:///:memory:",
                redis_url="redis://localhost:6379/15",
                checkpoint_backend="sqlite",
                job_discovery_max_pages_per_task=0,
            )

    def test_max_pages_rejects_above_one_hundred(self) -> None:
        with pytest.raises(ValidationError, match="job_discovery_max_pages_per_task"):
            Settings(
                app_env="test",
                app_auth_secret="test-secret-with-at-least-32-characters",
                object_encryption_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
                database_url="sqlite+pysqlite:///:memory:",
                redis_url="redis://localhost:6379/15",
                checkpoint_backend="sqlite",
                job_discovery_max_pages_per_task=101,
            )


def test_tencent_dotenv_tokens_are_read_without_interpolation(tmp_path) -> None:
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(
        "TENCENT_DOCS_TOKEN='${literal-token}'\n"
        "TEST_TENCENT_DOCS_TOKEN='${literal-test-token}'\n",
        encoding="utf-8",
    )

    assert _literal_tencent_dotenv_values(dotenv_path) == {
        "tencent_docs_token": "${literal-token}",
        "test_tencent_docs_token": "${literal-test-token}",
    }
