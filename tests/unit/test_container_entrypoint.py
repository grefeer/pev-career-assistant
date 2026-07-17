from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import pytest

from backend.entrypoint import CredentialConfigurationError, run


def test_runbook_admin_command_uses_entrypoint_wrapper() -> None:
    runbook = Path("docs/runbooks/platform-foundation.md").read_text(encoding="utf-8")
    assert "docker compose run --rm backend python scripts/create_admin.py" in runbook
    assert "docker compose exec backend python scripts/create_admin.py" not in runbook


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


@pytest.mark.parametrize("credential_name", ["DB_PASSWORD", "REDIS_PASSWORD"])
@pytest.mark.parametrize("control_character", ["\r", "\n"])
def test_entrypoint_rejects_line_breaks_with_fixed_redacted_error(
    credential_name: str, control_character: str
) -> None:
    environment = {"DB_PASSWORD": "db-value", "REDIS_PASSWORD": "redis-value"}
    environment[credential_name] += control_character + "not-disclosed"

    with pytest.raises(CredentialConfigurationError) as exc_info:
        run(["true"], environment, lambda executable, argv: None)

    assert str(exc_info.value) == "required service credentials are not configured"
    assert "not-disclosed" not in str(exc_info.value)


def test_compose_resolved_command_never_contains_redis_password() -> None:
    if shutil.which("docker") is None:
        pytest.skip("docker compose is not available")
    redis_password = "compose-review p@ss:/?#%"
    tencent_token = f"compose-tencent-{uuid4().hex}"
    environment = {
        **os.environ,
        "DB_PASSWORD": "compose-db p@ss:/?#%",
        "REDIS_PASSWORD": redis_password,
        "MINIO_ROOT_USER": "compose-minio-user-not-public",
        "MINIO_ROOT_PASSWORD": "compose-minio-password-not-public",
        "APP_AUTH_SECRET": "a" * 32,
        "OBJECT_ENCRYPTION_KEY": "A" * 43 + "=",
        "TENCENT_DOCS_TOKEN": tencent_token,
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
    assert "/usr/local/bin/start-password-redis" in " ".join(redis_command)
    assert "DB_PASSWORD_URLENCODED" not in backend_environment
    assert "REDIS_PASSWORD_URLENCODED" not in backend_environment
    assert "DATABASE_URL" not in backend_environment
    assert "REDIS_URL" not in backend_environment
    assert (
        backend_environment["OBJECT_STORE_ACCESS_KEY"] == environment["MINIO_ROOT_USER"]
    )
    assert (
        backend_environment["OBJECT_STORE_SECRET_KEY"]
        == environment["MINIO_ROOT_PASSWORD"]
    )
    assert backend_environment["TENCENT_DOCS_TOKEN"] == tencent_token
    assert all(
        "TENCENT_DOCS_TOKEN" not in service.get("environment", {})
        for service_name, service in config["services"].items()
        if service_name != "backend"
    )
    assert (
        config["services"]["migrate"]["image"] == config["services"]["backend"]["image"]
    )
    redis_script = (ROOT / "docker" / "redis" / "start.sh").read_text(encoding="utf-8")
    assert "carriage_return" in redis_script
    assert "newline" in redis_script
    assert "chmod 600" in redis_script


def test_compose_requires_minio_credentials_without_public_defaults() -> None:
    if shutil.which("docker") is None:
        pytest.skip("docker compose is not available")
    environment = {
        **os.environ,
        "DB_PASSWORD": "compose-db-not-public",
        "REDIS_PASSWORD": "compose-redis-not-public",
        "APP_AUTH_SECRET": "a" * 32,
        "OBJECT_ENCRYPTION_KEY": "A" * 43 + "=",
    }
    environment.pop("MINIO_ROOT_USER", None)
    environment.pop("MINIO_ROOT_PASSWORD", None)

    completed = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "minioadmin" not in completed.stdout
    assert "minioadmin" not in completed.stderr


def test_compose_declares_revision_and_respects_configured_host_ports() -> None:
    if shutil.which("docker") is None:
        pytest.skip("docker compose is not available")
    environment = {
        **os.environ,
        "DB_PASSWORD": "compose-db-not-public",
        "REDIS_PASSWORD": "compose-redis-not-public",
        "MINIO_ROOT_USER": "compose-minio-user-not-public",
        "MINIO_ROOT_PASSWORD": "compose-minio-password-not-public",
        "APP_AUTH_SECRET": "a" * 32,
        "OBJECT_ENCRYPTION_KEY": "A" * 43 + "=",
        "MYSQL_HOST_PORT": "13306",
        "REDIS_HOST_PORT": "16379",
        "MINIO_HOST_PORT": "19000",
        "MINIO_CONSOLE_HOST_PORT": "19001",
        "BACKEND_HOST_PORT": "18000",
        "FRONTEND_HOST_PORT": "15173",
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

    assert config["services"]["migrate"]["labels"] == {
        "com.career-assistant.schema-revision": "20260718_0008"
    }
    expected_ports = {
        "mysql": ("13306", 3306),
        "redis": ("16379", 6379),
        "minio": ("19000", 9000),
        "backend": ("18000", 8000),
        "frontend": ("15173", 80),
    }
    for service_name, (published, target) in expected_ports.items():
        ports = config["services"][service_name]["ports"]
        assert any(
            str(port["published"]) == published and port["target"] == target
            for port in ports
        )
    assert any(
        str(port["published"]) == "19001" and port["target"] == 9001
        for port in config["services"]["minio"]["ports"]
    )


def test_backend_image_contract_includes_admin_script() -> None:
    dockerfile = (ROOT / "backend" / "Dockerfile").read_text(encoding="utf-8")

    assert "COPY scripts ./scripts" in dockerfile
    assert "COPY requirements.txt ./requirements.txt" in dockerfile


def test_frontend_dockerfile_uses_locked_reproducible_install() -> None:
    dockerfile = (ROOT / "frontend" / "Dockerfile").read_text(encoding="utf-8")

    assert "COPY frontend/package*.json ./" in dockerfile
    assert "RUN npm ci" in dockerfile
    assert "RUN npm install" not in dockerfile
    assert dockerfile.index("COPY frontend/package*.json ./") < dockerfile.index(
        "COPY frontend/ ./"
    )


def test_redis_shell_helper_accepts_quotes_and_backslashes() -> None:
    if shutil.which("docker") is None:
        pytest.skip("docker is not available")
    password = "review 'quote' \"double\" \\ slash"
    container_name = f"task9-redis-shell-{uuid4().hex}"
    script = ROOT / "docker" / "redis" / "start.sh"
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            container_name,
            "-e",
            f"REDIS_PASSWORD={password}",
            "-v",
            f"{script}:/redis-start:ro",
            "redis:8.0-alpine",
            "sh",
            "/redis-start",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            ping = subprocess.run(
                [
                    "docker",
                    "exec",
                    "-e",
                    f"REDISCLI_AUTH={password}",
                    container_name,
                    "redis-cli",
                    "ping",
                ],
                capture_output=True,
                text=True,
            )
            if ping.returncode == 0:
                break
            time.sleep(0.5)
        assert ping.stdout.strip() == "PONG"
        mode = subprocess.run(
            ["docker", "exec", container_name, "stat", "-c", "%a", "/tmp/redis.conf"],
            check=True,
            capture_output=True,
            text=True,
        )
        assert mode.stdout.strip() == "600"
    finally:
        subprocess.run(
            ["docker", "rm", "-f", container_name], capture_output=True, check=False
        )


@pytest.mark.parametrize("control_character", ["\r", "\n"])
def test_redis_shell_helper_rejects_line_breaks_without_echoing_value(
    control_character: str,
) -> None:
    if shutil.which("docker") is None:
        pytest.skip("docker is not available")
    password = f"invalid{control_character}not-disclosed"
    script = ROOT / "docker" / "redis" / "start.sh"

    completed = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-e",
            f"REDIS_PASSWORD={password}",
            "-v",
            f"{script}:/redis-start:ro",
            "redis:8.0-alpine",
            "sh",
            "/redis-start",
        ],
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 78
    assert completed.stderr.strip() == "invalid redis credential configuration"
    assert "not-disclosed" not in completed.stderr
