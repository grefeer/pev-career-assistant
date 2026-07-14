from __future__ import annotations

# This script supports direct file execution, so the project root must be added
# before importing project modules below.
# ruff: noqa: E402

import argparse
from contextlib import AbstractContextManager
import getpass
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from pydantic import ValidationError

from backend.app.config import Settings
from backend.app.db.models import User, UserRole
from backend.app.repositories.users import get_by_account
from backend.app.services.auth import AccountExistsError, AuthService


class AdminAccountConflictError(ValueError):
    pass


def _return_admin_or_raise(existing: User) -> User:
    if existing.role is UserRole.ADMIN:
        return existing
    raise AdminAccountConflictError("账号已存在且不是管理员。")


def create_admin_user(
    db: Session,
    service: AuthService,
    *,
    account: str,
    nickname: str,
    password: str,
) -> User:
    existing = get_by_account(db, account)
    if existing is not None:
        return _return_admin_or_raise(existing)
    try:
        user = service.register(
            db,
            account=account,
            nickname=nickname,
            password=password,
        )
    except AccountExistsError:
        existing = get_by_account(db, account)
        if existing is None:
            raise
        return _return_admin_or_raise(existing)
    user.role = UserRole.ADMIN
    db.flush()
    return user


def _get_settings() -> Settings:
    from backend.app.config import get_settings

    return get_settings()


def _session_scope() -> AbstractContextManager[Session]:
    from backend.app.db.session import session_scope

    return session_scope()


def _print_failure() -> None:
    print("管理员账号创建失败。", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create a controlled admin account")
    parser.add_argument("--account", required=True)
    parser.add_argument("--nickname", required=True)
    args = parser.parse_args(argv)
    try:
        password = getpass.getpass("Password: ")
    except (EOFError, KeyboardInterrupt):
        _print_failure()
        return 1

    try:
        settings = _get_settings()
    except ValidationError:
        _print_failure()
        return 1

    try:
        with _session_scope() as db:
            admin = create_admin_user(
                db,
                AuthService(settings),
                account=args.account,
                nickname=args.nickname,
                password=password,
            )
    except (AdminAccountConflictError, AccountExistsError, SQLAlchemyError):
        _print_failure()
        return 1
    print(f"Admin account created: {admin.account}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
