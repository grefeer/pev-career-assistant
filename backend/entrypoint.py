from __future__ import annotations

from collections.abc import Callable, MutableMapping, Sequence
import os
import sys
from typing import Any
from urllib.parse import quote


class CredentialConfigurationError(RuntimeError):
    pass


def _contains_control_character(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def run(
    argv: Sequence[str],
    environment: MutableMapping[str, str],
    execvp: Callable[[str, list[str]], Any],
) -> None:
    database_password = environment.get("DB_PASSWORD")
    redis_password = environment.get("REDIS_PASSWORD")
    explicit_database_url = environment.get("DATABASE_URL")
    explicit_redis_url = environment.get("REDIS_URL")
    if (
        (not explicit_database_url and not database_password)
        or (not explicit_redis_url and not redis_password)
        or (
            database_password is not None
            and _contains_control_character(database_password)
        )
        or (
            redis_password is not None
            and _contains_control_character(redis_password)
        )
        or (
            explicit_database_url is not None
            and _contains_control_character(explicit_database_url)
        )
        or (
            explicit_redis_url is not None
            and _contains_control_character(explicit_redis_url)
        )
    ):
        raise CredentialConfigurationError(
            "required service credentials are not configured"
        )
    if not argv:
        raise RuntimeError("container command is not configured")

    database_host = environment.get("DB_HOST", "mysql")
    database_port = environment.get("DB_PORT", "3306")
    database_name = environment.get("DB_NAME", "career_assistant")
    redis_host = environment.get("REDIS_HOST", "redis")
    redis_port = environment.get("REDIS_PORT", "6379")
    redis_database = environment.get("REDIS_DB", "0")
    if not explicit_database_url:
        environment["DATABASE_URL"] = (
            "mysql+pymysql://root:"
            f"{quote(database_password or '', safe='')}@{database_host}:{database_port}/"
            f"{quote(database_name, safe='')}?charset=utf8mb4"
        )
    if not explicit_redis_url:
        environment["REDIS_URL"] = (
            f"redis://default:{quote(redis_password or '', safe='')}@"
            f"{redis_host}:{redis_port}/{redis_database}"
        )
    execvp(argv[0], list(argv))


def main() -> int:
    try:
        run(sys.argv[1:], os.environ, os.execvp)
    except (CredentialConfigurationError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 78
    return 0


if __name__ == "__main__":  # pragma: no cover - script entrypoint; unreachable when imported by pytest
    raise SystemExit(main())
