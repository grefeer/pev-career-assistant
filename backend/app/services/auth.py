from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from pwdlib import PasswordHash
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.config import Settings
from backend.app.db.models import User, UserRole
from backend.app.repositories.users import get_by_account, normalize_account


class AccountExistsError(ValueError):
    pass


def _is_account_unique_violation(error: IntegrityError) -> bool:
    message = str(error.orig).lower()
    if "unique constraint failed: users.account" in message:
        return True
    args = getattr(error.orig, "args", ())
    mysql_error_code = args[0] if args else None
    return mysql_error_code == 1062 and (
        "ix_users_account" in message or "users.account" in message
    )


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
        try:
            with db.begin_nested():
                db.add(user)
                db.flush()
        except IntegrityError as error:
            if _is_account_unique_violation(error):
                raise AccountExistsError("该账号已经存在，请直接登录。") from None
            raise
        return user

    def authenticate(self, db: Session, *, account: str, password: str) -> User | None:
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
        return jwt.encode(payload, self.settings.app_auth_secret, algorithm="HS256")

    def decode_user_token(self, token: str) -> dict[str, Any]:
        claims = jwt.decode(
            token,
            self.settings.app_auth_secret,
            algorithms=["HS256"],
            audience=self.settings.jwt_audience,
            issuer=self.settings.jwt_issuer,
            options={"require": ["exp", "iss", "aud", "sub", "role", "jti"]},
        )
        exp = claims["exp"]
        if isinstance(exp, bool) or not isinstance(exp, (int, float)):
            raise jwt.InvalidTokenError("JWT claims are invalid")
        for claim_name in ("sub", "jti"):
            value = claims[claim_name]
            if not isinstance(value, str) or not value.strip():
                raise jwt.InvalidTokenError("JWT claims are invalid")
        role = claims["role"]
        if not isinstance(role, str):
            raise jwt.InvalidTokenError("JWT claims are invalid")
        try:
            UserRole(role)
        except ValueError:
            raise jwt.InvalidTokenError("JWT claims are invalid") from None
        return claims
