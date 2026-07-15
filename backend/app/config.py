from __future__ import annotations

from functools import lru_cache
import base64
import binascii
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_APP_AUTH_SECRET = "replace-with-a-random-secret-of-at-least-32-characters"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT_DIR / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
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

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def cors_origin_list(self) -> list[str]:
        return [
            value.strip() for value in self.cors_origins.split(",") if value.strip()
        ]

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
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
