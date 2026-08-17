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

    job_discovery_ocr_enabled: bool = False
    # When on, fetch-public-job-pages falls back to a headless-Chromium render
    # when the plain requests path fails or returns an empty SPA/login shell.
    # Off by default so unit suites never launch a browser.
    job_discovery_playwright_fallback_enabled: bool = False
    # Optional, explicitly provisioned Playwright storage state. The runtime
    # only reads this file; it never saves cookies or attempts to solve a
    # challenge. Keep it outside the repository and treat it as a credential.
    job_discovery_browser_storage_state_path: str | None = None
    # A1 (docs/findjobs-optimization-plan.zh-CN.md §4.1): when on, URLs owned
    # by a certified public-JSON adapter host (didi/netease/baidu, gated by
    # skill/job-discovery/scripts/adapters/endpoint_allowlist.json with
    # review_status == "reviewed") are fetched adapter-first and never fall
    # back to browse (adapter failure is an explicit blocked terminal).
    # Reviewed 2026-08-08 (allowlist review_status=reviewed, reviewed_by
    # recorded) + live smoke of all three companies passed, so default on.
    use_public_api_adapters: bool = True


    # Adaptive Planner–Executor–Verifier runtime. The personal assistant's
    # default execution path is the adaptive PEV harness. Deployments may
    # explicitly disable it for maintenance; a missing model key then degrades
    # safely to ``agent_harness_unavailable``.
    # The limits are hard operational ceilings; Agents, rather than Settings,
    # choose their plans, Skills, retries and verifier decisions.
    agent_harness_enabled: bool = True
    agent_harness_model: str = "deepseek-v4-flash"
    agent_harness_max_agent_turns: int = Field(default=12, ge=1, le=100)
    agent_harness_max_tool_calls: int = Field(default=24, ge=1, le=200)
    agent_harness_max_replans: int = Field(default=2, ge=0, le=10)
    agent_harness_max_model_requests: int = Field(default=128, ge=1, le=500)
    agent_harness_max_input_tokens: int = Field(default=1_000_000, ge=1_000, le=2_000_000)
    agent_harness_max_output_tokens: int = Field(default=200_000, ge=1_000, le=500_000)
    # Provider-side per-request ceiling. The run-level output budget above is
    # still shared across Planner, Executor and Verifier.
    agent_harness_model_max_output_tokens: int = Field(default=4_096, ge=256, le=32_000)
    # Hard wall-clock ceiling per run (seconds). Unlike turn/tool budgets this
    # one is a transport/resource pause: exhausting it degrades to a recoverable
    # ``waiting_user`` (the clock window refreshes on resume) rather than a hard
    # failure. Defaults to 300s; raise for I/O-bound work (e.g. many Playwright
    # renders of SPA career sites).
    agent_harness_max_wall_clock_seconds: int = Field(default=300, ge=10, le=3_600)
    agent_harness_max_event_payload_bytes: int = Field(
        default=16_384, ge=1_024, le=262_144
    )
    agent_harness_catalog_in_system_prompt: bool = False

    # Personal mode (single-user application-assistant) settings.
    # When True: registration disabled, require_admin bypassed for the seeded
    # single user, discovered candidates auto-promote to verified JobPostings
    # (admin review skipped), and the student "verified only" gate is relaxed
    # for that user. Multi-tenant logic stays intact when False.
    personal_mode: bool = False

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
        if self.is_production and self.database_url.startswith("sqlite"):
            raise ValueError("SQLite database_url is not permitted in production")
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
