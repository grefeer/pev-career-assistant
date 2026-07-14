from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from pwdlib import PasswordHash
from sqlalchemy.orm import Session

from backend.app.config import Settings
from backend.app.db.models import User, UserRole
from backend.app.repositories.users import get_by_account, normalize_account


class AccountExistsError(ValueError):
    pass


class AuthService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.password_hash = PasswordHash.recommended()

    def verify_password(self, plaintext: str, encoded: str) -> bool:
        return self.password_hash.verify(plaintext, encoded)

    def register(
        self, db: Session, *, account: str, nickname: str, password: str
    ) -> User:
        normalized = normalize_account(account)
        if get_by_account(db, normalized):
            raise AccountExistsError("该账号已经存在，请直接登录。")
        user = User(
            account=normalized,
            nickname=nickname.strip(),
            password_hash=self.password_hash.hash(password),
            role=UserRole.STUDENT,
            last_login_at=datetime.now(timezone.utc),
        )
        db.add(user)
        db.flush()
        return user

    def authenticate(
        self, db: Session, *, account: str, password: str
    ) -> User | None:
        user = get_by_account(db, account)
        if (
            not user
            or not user.is_active
            or not self.verify_password(password, user.password_hash)
        ):
            return None
        user.last_login_at = datetime.now(timezone.utc)
        db.flush()
        return user

    def issue_user_token(self, user: User) -> str:
        now = datetime.now(timezone.utc)
        payload = {
            "iss": self.settings.jwt_issuer,
            "aud": self.settings.jwt_audience,
            "sub": user.id,
            "role": user.role.value,
            "jti": str(uuid.uuid4()),
            "iat": now,
            "exp": now + timedelta(seconds=self.settings.jwt_ttl_seconds),
        }
        return jwt.encode(
            payload, self.settings.app_auth_secret, algorithm="HS256"
        )

    def decode_user_token(self, token: str) -> dict[str, Any]:
        return jwt.decode(
            token,
            self.settings.app_auth_secret,
            algorithms=["HS256"],
            audience=self.settings.jwt_audience,
            issuer=self.settings.jwt_issuer,
            options={"require": ["exp", "iss", "aud", "sub", "role", "jti"]},
        )
