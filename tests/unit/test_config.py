from pydantic import SecretStr, ValidationError
import pytest

from backend.app.config import Settings
from tests.conftest import settings_override


def _production_base(**overrides):
    """Return a fully valid production Settings payload for one-field-invalid tests."""
    base = dict(
        app_env="production",
        app_auth_secret="x" * 32,
        object_encryption_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        database_url="mysql+pymysql://root@mysql/db",
        redis_url="redis://redis/0",
        checkpoint_backend="redis",
        rate_limit_hmac_secret="rate-limit-secret-with-at-least-32-chars",
        trusted_proxy_cidrs="172.16.0.0/12",
        object_store_access_key="safe-production-object-key",
        object_store_secret_key="safe-production-object-secret",
    )
    base.update(overrides)
    return base


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


def test_production_accepts_a_scoped_trusted_proxy_cidr() -> None:
    """A fully-valid production payload with a non-/0 CIDR passes validation.

    Exercises the CIDR-validation loop's normal completion path: the scoped
    network (prefixlen != 0) skips the raise (271->269) and the loop exits
    cleanly (269->273).
    """
    settings = Settings(**_production_base())
    assert settings.is_production is True
    assert settings.trusted_proxy_cidrs == "172.16.0.0/12"


def test_settings_override_uses_test_defaults_and_applies_values() -> None:
    settings = settings_override(jwt_audience="test-client")

    assert settings.app_env == "test"
    assert settings.database_url == "sqlite+pysqlite:///:memory:"
    assert settings.checkpoint_backend == "sqlite"
    assert settings.jwt_audience == "test-client"


def test_effective_test_token_falls_back_to_production_token() -> None:
    """When no test token is set the production token is used.

    Explicit ``test_tencent_docs_token=None`` is required: ``Settings`` reads the
    project ``.env`` and ``os.environ`` via the dotenv/env sources, and an earlier
    test may populate ``TEST_TENCENT_DOCS_TOKEN`` into the process environment.
    Init kwargs have the highest priority, so this pins the field to ``None``
    instead of letting env pollution fill it.
    """
    only_prod = settings_override(
        tencent_docs_token=SecretStr("prod-tok"),
        test_tencent_docs_token=None,
    )
    assert only_prod.effective_test_tencent_docs_token == SecretStr("prod-tok")
    override = settings_override(
        tencent_docs_token=SecretStr("prod-tok"),
        test_tencent_docs_token=SecretStr("test-tok"),
    )
    assert override.effective_test_tencent_docs_token == SecretStr("test-tok")


def test_object_encryption_key_rejects_non_base64_input() -> None:
    """A key that is not valid base64 is rejected before it can be used for AES."""
    with pytest.raises(ValidationError, match="OBJECT_ENCRYPTION_KEY"):
        settings_override(object_encryption_key="not-valid-base64!!!")


def test_object_encryption_key_rejects_wrong_byte_length() -> None:
    """Valid base64 that does not decode to 32 bytes is rejected."""
    with pytest.raises(ValidationError, match="OBJECT_ENCRYPTION_KEY"):
        settings_override(object_encryption_key="AAAA")  # decodes to 3 bytes


def test_production_requires_redis_checkpoint_backend() -> None:
    """Production must not run on the sqlite checkpoint backend."""
    with pytest.raises(ValidationError, match="CHECKPOINT_BACKEND"):
        Settings(**_production_base(checkpoint_backend="sqlite"))


def test_production_rejects_sqlite_database_url() -> None:
    """SQLite must never be the production authority store (MySQL is source of truth)."""
    with pytest.raises(ValidationError, match="SQLite database_url"):
        Settings(**_production_base(database_url="sqlite+pysqlite:///:memory:"))


def test_production_rejects_short_rate_limit_hmac_secret() -> None:
    """A present but too-short HMAC secret is rejected in production."""
    with pytest.raises(ValidationError, match="RATE_LIMIT_HMAC_SECRET"):
        Settings(**_production_base(rate_limit_hmac_secret="short"))


def test_production_requires_trusted_proxy_cidrs() -> None:
    """Blank trusted-proxy CIDRs would trust no proxy and must be rejected."""
    with pytest.raises(ValidationError, match="TRUSTED_PROXY_CIDRS"):
        Settings(**_production_base(trusted_proxy_cidrs="   "))

