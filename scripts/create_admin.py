from __future__ import annotations

import argparse
import getpass
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy.orm import Session

from backend.app.db.models import User, UserRole
from backend.app.services.auth import AuthService


def create_admin_user(
    db: Session,
    service: AuthService,
    *,
    account: str,
    nickname: str,
    password: str,
) -> User:
    user = service.register(
        db,
        account=account,
        nickname=nickname,
        password=password,
    )
    user.role = UserRole.ADMIN
    db.flush()
    return user


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a controlled admin account")
    parser.add_argument("--account", required=True)
    parser.add_argument("--nickname", required=True)
    args = parser.parse_args()
    password = getpass.getpass("Password: ")

    from backend.app.config import get_settings
    from backend.app.db.session import session_scope

    with session_scope() as db:
        admin = create_admin_user(
            db,
            AuthService(get_settings()),
            account=args.account,
            nickname=args.nickname,
            password=password,
        )
    print(f"Admin account created: {admin.account}")


if __name__ == "__main__":
    main()
