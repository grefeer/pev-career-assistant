from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys

import jwt
import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.config import Settings
from backend.app.db.base import Base
from backend.app.db.models import UserRole
from backend.app.api import dependencies
from backend.app.services.auth import AccountExistsError, AuthService
from scripts.create_admin import create_admin_user


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "app_env": "test",
        "app_auth_secret": "test-secret-with-at-least-32-characters",
        "object_encryption_key": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        "database_url": "sqlite+pysqlite:///:memory:",
        "redis_url": "redis://localhost:6379/15",
        "checkpoint_backend": "sqlite",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_register_hashes_password_and_token_carries_student_role() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    settings = make_settings()
    service = AuthService(settings)
    with Session(engine) as db:
        user = service.register(
            db, account=" Alice ", nickname="Alice", password="secret12"
        )
        db.commit()
        token = service.issue_user_token(user)
        claims = service.decode_user_token(token)
    assert user.account == "alice"
    assert user.role is UserRole.STUDENT
    assert user.password_hash != "secret12"
    assert service.verify_password("secret12", user.password_hash)
    assert claims["sub"] == user.id
    assert claims["role"] == "student"
    assert {"exp", "iss", "aud", "sub", "role", "jti"} <= claims.keys()


def test_duplicate_account_raises_stable_error() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    service = AuthService(make_settings())
    with Session(engine) as db:
        service.register(db, account="Alice", nickname="Alice", password="secret12")
        with pytest.raises(
            AccountExistsError, match="该账号已经存在，请直接登录。"
        ):
            service.register(
                db, account=" alice ", nickname="Other", password="secret34"
            )


def test_authenticate_rejects_bad_password_and_inactive_user() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    service = AuthService(make_settings())
    with Session(engine) as db:
        user = service.register(
            db, account="Alice", nickname="Alice", password="secret12"
        )
        assert (
            service.authenticate(db, account="alice", password="not-the-password")
            is None
        )
        user.is_active = False
        db.flush()
        assert service.authenticate(db, account="alice", password="secret12") is None


def test_decode_rejects_missing_required_claim() -> None:
    settings = make_settings()
    service = AuthService(settings)
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "iss": settings.jwt_issuer,
            "aud": settings.jwt_audience,
            "sub": "user-id",
            "jti": "token-id",
            "iat": now,
            "exp": now + timedelta(minutes=5),
        },
        settings.app_auth_secret,
        algorithm="HS256",
    )
    with pytest.raises(jwt.MissingRequiredClaimError):
        service.decode_user_token(token)


@pytest.mark.parametrize(
    ("settings_overrides", "payload_overrides", "secret", "error_type"),
    [
        ({}, {}, "different-secret-with-at-least-32-chars", jwt.InvalidSignatureError),
        ({}, {"iss": "wrong-issuer"}, None, jwt.InvalidIssuerError),
        ({}, {"aud": "wrong-audience"}, None, jwt.InvalidAudienceError),
        (
            {},
            {"exp": datetime.now(timezone.utc) - timedelta(seconds=1)},
            None,
            jwt.ExpiredSignatureError,
        ),
    ],
)
def test_decode_rejects_invalid_tokens(
    settings_overrides: dict[str, object],
    payload_overrides: dict[str, object],
    secret: str | None,
    error_type: type[Exception],
) -> None:
    settings = make_settings(**settings_overrides)
    service = AuthService(settings)
    now = datetime.now(timezone.utc)
    payload: dict[str, object] = {
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "sub": "user-id",
        "role": "student",
        "jti": "token-id",
        "iat": now,
        "exp": now + timedelta(minutes=5),
    }
    payload.update(payload_overrides)
    token = jwt.encode(
        payload, secret or settings.app_auth_secret, algorithm="HS256"
    )
    with pytest.raises(error_type):
        service.decode_user_token(token)


def test_get_current_user_requires_valid_token_for_active_database_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    settings = make_settings()
    service = AuthService(settings)
    monkeypatch.setattr(dependencies, "get_settings", lambda: settings)
    with Session(engine) as db:
        user = service.register(
            db, account="Alice", nickname="Alice", password="secret12"
        )
        token = service.issue_user_token(user)
        credentials = HTTPAuthorizationCredentials(
            scheme="Bearer", credentials=token
        )
        assert dependencies.get_current_user(credentials, db) is user

        user.is_active = False
        db.flush()
        with pytest.raises(HTTPException) as exc_info:
            dependencies.get_current_user(credentials, db)
    assert exc_info.value.status_code == 401


@pytest.mark.parametrize("token", [None, "not-a-jwt"])
def test_get_current_user_rejects_missing_or_invalid_token(
    monkeypatch: pytest.MonkeyPatch, token: str | None
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    settings = make_settings()
    monkeypatch.setattr(dependencies, "get_settings", lambda: settings)
    credentials = (
        HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        if token is not None
        else None
    )
    with Session(engine) as db, pytest.raises(HTTPException) as exc_info:
        dependencies.get_current_user(credentials, db)
    assert exc_info.value.status_code == 401


def test_student_token_cannot_pass_admin_dependency() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    service = AuthService(make_settings())
    with Session(engine) as db:
        student = service.register(
            db, account="Alice", nickname="Alice", password="secret12"
        )
        service.decode_user_token(service.issue_user_token(student))
        with pytest.raises(HTTPException) as exc_info:
            dependencies.require_admin(student)
    assert exc_info.value.status_code == 403


def test_controlled_admin_creation_uses_argon2_and_admin_role() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    service = AuthService(make_settings())
    with Session(engine) as db:
        admin = create_admin_user(
            db,
            service,
            account=" Admin ",
            nickname="Administrator",
            password="secret12",
        )
        db.commit()
        assert admin.account == "admin"
        assert admin.role is UserRole.ADMIN
        assert admin.password_hash.startswith("$argon2")
        assert service.verify_password("secret12", admin.password_hash)
        assert dependencies.require_admin(admin) is admin


def test_create_admin_script_can_run_directly() -> None:
    project_root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, "scripts/create_admin.py", "--help"],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--account" in result.stdout
    assert "--nickname" in result.stdout
