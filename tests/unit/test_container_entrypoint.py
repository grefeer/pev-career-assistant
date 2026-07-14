from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any
from urllib.parse import quote

import pytest

from backend.entrypoint import CredentialConfigurationError, run


ROOT = Path(__file__).resolve().parents[2]


def test_entrypoint_derives_encoded_service_urls_and_executes_argv() -> None:
    database_password = "db p@ss:/?#%"
    redis_password = "redis p@ss:/?#%"
    environment = {
        "DB_PASSWORD": database_password,
        "REDIS_PASSWORD": redis_password,
        "DB_HOST": "mysql-internal",
        "DB_NAME": "career_test",
        "REDIS_HOST": "redis-internal",
        "REDIS_DB": "0",
    }
    executed: list[tuple[str, list[str]]] = []

    run(
        ["alembic", "upgrade", "head"],
        environment,
        lambda executable, argv: executed.append((executable, argv)),
    )

    assert quote(database_password, safe="") in environment["DATABASE_URL"]
    assert quote(redis_password, safe="") in environment["REDIS_URL"]
    assert database_password not in environment["DATABASE_URL"]
    assert redis_password not in environment["REDIS_URL"]
    assert executed == [("alembic", ["alembic", "upgrade", "head"])]


@pytest.mark.parametrize("missing_name", ["DB_PASSWORD", "REDIS_PASSWORD"])
def test_entrypoint_missing_credential_has_fixed_redacted_error(
    missing_name: str,
) -> None:
    environment = {"DB_PASSWORD": "db-value", "REDIS_PASSWORD": "redis-value"}
    environment.pop(missing_name)

    with pytest.raises(CredentialConfigurationError) as exc_info:
        run(["true"], environment, lambda executable, argv: None)

    assert str(exc_info.value) == "required service credentials are not configured"
    assert missing_name not in str(exc_info.value)


def test_compose_resolved_command_never_contains_redis_password() -> None:
    if shutil.which("docker") is None:
        pytest.skip("docker compose is not available")
    redis_password = "compose-review p@ss:/?#%"
    environment = {
        **os.environ,
        "DB_PASSWORD": "compose-db p@ss:/?#%",
        "REDIS_PASSWORD": redis_password,
        "APP_AUTH_SECRET": "a" * 32,
        "OBJECT_ENCRYPTION_KEY": "A" * 43 + "=",
    }

    completed = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    config: dict[str, Any] = json.loads(completed.stdout)
    redis_command = config["services"]["redis"]["command"]
    backend_environment = config["services"]["backend"]["environment"]

    assert redis_password not in " ".join(redis_command)
    assert "$$REDIS_PASSWORD" in " ".join(redis_command)
    assert "DB_PASSWORD_URLENCODED" not in backend_environment
    assert "REDIS_PASSWORD_URLENCODED" not in backend_environment
    assert "DATABASE_URL" not in backend_environment
    assert "REDIS_URL" not in backend_environment
    assert config["services"]["migrate"]["image"] == config["services"]["backend"][
        "image"
    ]


def test_frontend_dockerfile_uses_locked_reproducible_install() -> None:
    dockerfile = (ROOT / "frontend" / "Dockerfile").read_text(encoding="utf-8")

    assert "COPY frontend/package*.json ./" in dockerfile
    assert "RUN npm ci" in dockerfile
    assert "RUN npm install" not in dockerfile
    assert dockerfile.index("COPY frontend/package*.json ./") < dockerfile.index(
        "COPY frontend/ ./"
    )
