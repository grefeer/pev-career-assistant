from __future__ import annotations

from functools import lru_cache
import base64
import binascii
import ipaddress
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict


ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_APP_AUTH_SECRET = "replace-with-a-random-secret-of-at-least-32-characters"
_TENCENT_TOKEN_ENV_NAMES = ("TENCENT_DOCS_TOKEN", "TEST_TENCENT_DOCS_TOKEN")


def _literal_tencent_dotenv_values(path: Path = ROOT_DIR / ".env") -> dict[str, str]:
    values = dotenv_values(path, interpolate=False)
    result: dict[str, str] = {}
    for env_name in _TENCENT_TOKEN_ENV_NAMES:
        value = values.get(env_name)
        if value:
            result[env_name.lower()] = value
    return result


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT_DIR / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[
        PydanticBaseSettingsSource,
        PydanticBaseSettingsSource,
        PydanticBaseSettingsSource,
        PydanticBaseSettingsSource,
        PydanticBaseSettingsSource,
    ]:
        del settings_cls
        return (
            init_settings,
            env_settings,
            _literal_tencent_dotenv_values,
            dotenv_settings,
            file_secret_settings,
        )

    app_env: Literal["development", "test", "production"] = "development"
    app_auth_secret: str
    jwt_issuer: str = "career-assistant-api"
    jwt_audience: str = "career-assistant-web"
    jwt_ttl_seconds: int = 604800
    database_url: str
    redis_url: str
    readiness_timeout_seconds: int = Field(default=2, ge=1, le=30)
    checkpoint_backend: Literal["sqlite", "redis"] = "sqlite"
    checkpoint_sqlite_path: Path = (
        ROOT_DIR / "checkpoints" / "langgraph_checkpoints.sqlite"
    )
    object_store_endpoint: str = "http://localhost:9000"
    object_store_region: str = "us-east-1"
    object_store_bucket: str = "career-assistant"
    object_store_access_key: str = "minioadmin"
    object_store_secret_key: str = "minioadmin"
    object_encryption_key: str
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    trusted_proxy_cidrs: str = ""
    rate_limit_hmac_secret: SecretStr | None = Field(default=None, repr=False)
    tencent_docs_token: SecretStr | None = Field(default=None, repr=False)
    test_tencent_docs_token: SecretStr | None = Field(default=None, repr=False)

    # Job Discovery Agent settings
    # OCR is pluggable and defaults to needs_manual_review when unavailable.
    job_discovery_enabled: bool = False
    job_discovery_agent_version: str = "1.0.0"
    job_discovery_model: str = "deepseek-v4-flash"
    job_discovery_max_pages_per_task: int = Field(default=20, ge=1, le=100)
    job_discovery_max_candidates_per_task: int = Field(default=10, ge=1, le=50)
    job_discovery_task_timeout_seconds: int = Field(default=600, ge=30, le=3600)
    job_discovery_browser_headless: bool = True
    job_discovery_ocr_enabled: bool = False
    # Planner-Executor-Verifier (PEV) gray-migration switches.  All new
    # execution remains opt-in until a site has a certified CrawlPlan.
    job_discovery_pev_enabled: bool = False
    job_discovery_planner_enabled: bool = False
    job_discovery_legacy_path_c_enabled: bool = True
    job_discovery_planner_max_inspection_pages: int = Field(default=3, ge=1, le=5)
    # Hard wall-clock deadline (seconds) for a SnapshotPlan whose steps run
    # real network fetches (WeChat ``fetch_wechat_article``). When > 0 the
    # worker passes it to SnapshotExecutor; tools in ``hard_timeout_tools``
    # run in a spawned subprocess that is killed at the deadline, yielding a
    # ``needs_manual_review`` / ``task_deadline_exceeded`` result instead of
    # an unbounded hang. 0 = disabled (no deadline enforced).
    job_discovery_snapshot_deadline_seconds: int = Field(default=0, ge=0, le=3600)

    # Strategy Router settings
    job_discovery_strategy_enabled: bool = False
    strategy_degradation_threshold: int = Field(default=3, ge=1, le=10)
    strategy_recovery_threshold: int = Field(default=2, ge=1, le=10)
    trajectory_retention_days: int = Field(default=90, ge=7, le=365)
    strategy_health_check_interval_hours: int = Field(default=24, ge=1, le=168)
    trajectory_annotation_enabled: bool = True

    # Personal mode (single-user application-assistant) settings.
    # When True: registration disabled, require_admin bypassed for the seeded
    # single user, discovered candidates auto-promote to verified JobPostings
    # (admin review skipped), and the student "verified only" gate is relaxed
    # for that user. Multi-tenant logic stays intact when False.
    personal_mode: bool = False
    # LLM used for the cheap relevance-ranker batch (sits upstream of the
    # expensive per-job MatchService). Defaults to the same model family.
    relevance_model: str = "deepseek-v4-flash"
    # Max candidates scored per single LLM batch call. Larger batches save calls
    # but risk output truncation; 30 is a safe default for structured output.
    relevance_batch_size: int = Field(default=30, ge=1, le=100)

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def cors_origin_list(self) -> list[str]:
        return [
            value.strip() for value in self.cors_origins.split(",") if value.strip()
        ]

    @property
    def effective_test_tencent_docs_token(self) -> SecretStr | None:
        return self.test_tencent_docs_token or self.tencent_docs_token

    @field_validator("app_auth_secret")
    @classmethod
    def validate_auth_secret_length(cls, value: str) -> str:
        if len(value) < 32:
            raise ValueError("APP_AUTH_SECRET must contain at least 32 characters")
        return value

    @field_validator("object_encryption_key")
    @classmethod
    def validate_object_encryption_key(cls, value: str) -> str:
        try:
            decoded = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(
                "OBJECT_ENCRYPTION_KEY must be a 32-byte base64 value"
            ) from exc
        if len(decoded) != 32:
            raise ValueError("OBJECT_ENCRYPTION_KEY must be a 32-byte base64 value")
        return value

    @model_validator(mode="after")
    def validate_production_settings(self) -> "Settings":
        if self.is_production and self.app_auth_secret == DEFAULT_APP_AUTH_SECRET:
            raise ValueError("APP_AUTH_SECRET must be replaced in production")
        if self.is_production and self.checkpoint_backend != "redis":
            raise ValueError("production requires CHECKPOINT_BACKEND=redis")
        object_credentials = (
            self.object_store_access_key,
            self.object_store_secret_key,
        )
        if self.is_production and any(
            value.strip().lower()
            in {"minioadmin", "${minio_root_user}", "${minio_root_password}"}
            or value.strip().lower().startswith(
                ("replace-with-", "replace-me", "changeme")
            )
            for value in object_credentials
        ):
            raise ValueError("OBJECT_STORE credentials must be replaced in production")
        if self.is_production:
            if self.rate_limit_hmac_secret is None:
                raise ValueError("RATE_LIMIT_HMAC_SECRET is required in production")
            if len(self.rate_limit_hmac_secret.get_secret_value()) < 32:
                raise ValueError("RATE_LIMIT_HMAC_SECRET must contain at least 32 characters")
            if not self.trusted_proxy_cidrs.strip():
                raise ValueError("TRUSTED_PROXY_CIDRS is required in production")
            for raw_cidr in self.trusted_proxy_cidrs.split(","):
                network = ipaddress.ip_network(raw_cidr.strip(), strict=False)
                if network.prefixlen == 0:
                    raise ValueError("TRUSTED_PROXY_CIDRS must not trust all addresses")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
