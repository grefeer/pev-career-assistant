from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys
import threading

import jwt
import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.config import Settings, get_settings
from backend.app.db.base import Base
from backend.app.db.models import User, UserRole
from backend.app.api import dependencies
from backend.app.repositories.users import get_by_account
from backend.app.services.auth import AccountExistsError, AuthService
from scripts import create_admin as create_admin_module
from scripts.create_admin import AdminAccountConflictError, create_admin_user


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
    assert isinstance(claims["iat"], int) and not isinstance(claims["iat"], bool)
    assert {"exp", "iss", "aud", "sub", "role", "jti"} <= claims.keys()


def test_duplicate_account_raises_stable_error() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    service = AuthService(make_settings())
    with Session(engine) as db:
        service.register(db, account="Alice", nickname="Alice", password="secret12")
        with pytest.raises(AccountExistsError, match="该账号已经存在，请直接登录。"):
            service.register(
                db, account=" alice ", nickname="Other", password="secret34"
            )


def test_concurrent_duplicate_registration_is_stable_and_session_remains_usable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "auth-race.sqlite"
    engine = create_engine(f"sqlite+pysqlite:///{database_path}")
    Base.metadata.create_all(engine)
    service = AuthService(make_settings())
    barrier = threading.Barrier(2)
    original_lookup = get_by_account

    def synchronized_lookup(db: Session, account: str):  # type: ignore[no-untyped-def]
        result = original_lookup(db, account)
        barrier.wait(timeout=10)
        return result

    monkeypatch.setattr("backend.app.services.auth.get_by_account", synchronized_lookup)
    outcomes: list[str] = []

    def register() -> None:
        with Session(engine) as db:
            try:
                service.register(
                    db, account="race", nickname="Race", password="secret12"
                )
                db.commit()
                outcomes.append("created")
            except AccountExistsError:
                count = db.scalar(select(func.count()).select_from(User))
                outcomes.append(f"duplicate-usable-{count}")

    threads = [threading.Thread(target=register) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert not any(thread.is_alive() for thread in threads)
    assert sorted(outcomes) == ["created", "duplicate-usable-1"]


def test_register_does_not_translate_unrelated_integrity_error() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    service = AuthService(make_settings())
    with Session(engine) as db:
        db.execute(
            text(
                """
                CREATE TRIGGER reject_blocked_nickname
                BEFORE INSERT ON users
                WHEN NEW.nickname = 'blocked'
                BEGIN
                    SELECT RAISE(ABORT, 'nickname blocked');
                END
                """
            )
        )
        with pytest.raises(IntegrityError, match="nickname blocked"):
            service.register(
                db, account="alice", nickname="blocked", password="secret12"
            )
        assert db.scalar(select(func.count()).select_from(User)) == 0


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
    "claim_overrides",
    [
        {"exp": "9999999999"},
        {"exp": True},
        {"role": 1},
        {"role": "superadmin"},
        {"sub": ""},
        {"jti": ""},
    ],
)
def test_decode_rejects_semantically_invalid_claims(
    claim_overrides: dict[str, object],
) -> None:
    settings = make_settings()
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
    payload.update(claim_overrides)
    token = jwt.encode(payload, settings.app_auth_secret, algorithm="HS256")

    with pytest.raises(jwt.InvalidTokenError):
        service.decode_user_token(token)


@pytest.mark.parametrize("exp", [float("inf"), float("-inf")])
def test_decode_rejects_non_finite_exp_as_invalid_token(exp: float) -> None:
    settings = make_settings()
    service = AuthService(settings)
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "iss": settings.jwt_issuer,
            "aud": settings.jwt_audience,
            "sub": "user-id",
            "role": "student",
            "jti": "token-id",
            "iat": now,
            "exp": exp,
        },
        settings.app_auth_secret,
        algorithm="HS256",
    )

    with pytest.raises(jwt.InvalidTokenError):
        service.decode_user_token(token)


def test_non_finite_exp_is_unauthorized_in_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    settings = make_settings()
    monkeypatch.setattr(dependencies, "get_settings", lambda: settings)
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "iss": settings.jwt_issuer,
            "aud": settings.jwt_audience,
            "sub": "user-id",
            "role": "student",
            "jti": "token-id",
            "iat": now,
            "exp": float("inf"),
        },
        settings.app_auth_secret,
        algorithm="HS256",
    )
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)

    with Session(engine) as db, pytest.raises(HTTPException) as exc_info:
        dependencies.get_current_user(credentials, db)

    assert exc_info.value.status_code == 401


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
    token = jwt.encode(payload, secret or settings.app_auth_secret, algorithm="HS256")
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
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
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


def test_admin_claim_cannot_elevate_database_student(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    settings = make_settings()
    service = AuthService(settings)
    monkeypatch.setattr(dependencies, "get_settings", lambda: settings)
    with Session(engine) as db:
        student = service.register(
            db, account="alice", nickname="Alice", password="secret12"
        )
        now = datetime.now(timezone.utc)
        token = jwt.encode(
            {
                "iss": settings.jwt_issuer,
                "aud": settings.jwt_audience,
                "sub": student.id,
                "role": "admin",
                "jti": "forged-role",
                "iat": now,
                "exp": now + timedelta(minutes=5),
            },
            settings.app_auth_secret,
            algorithm="HS256",
        )
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        current_user = dependencies.get_current_user(credentials, db)
        with pytest.raises(HTTPException) as exc_info:
            dependencies.require_admin(current_user)
    assert exc_info.value.status_code == 403


def test_admin_token_loses_privilege_after_database_demotion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    settings = make_settings()
    service = AuthService(settings)
    monkeypatch.setattr(dependencies, "get_settings", lambda: settings)
    with Session(engine) as db:
        admin = create_admin_user(
            db,
            service,
            account="admin",
            nickname="Admin",
            password="secret12",
        )
        token = service.issue_user_token(admin)
        admin.role = UserRole.STUDENT
        db.flush()
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        current_user = dependencies.get_current_user(credentials, db)
        with pytest.raises(HTTPException) as exc_info:
            dependencies.require_admin(current_user)
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


def test_controlled_admin_creation_is_idempotent_without_resetting_password() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    service = AuthService(make_settings())
    with Session(engine) as db:
        original = create_admin_user(
            db,
            service,
            account="admin",
            nickname="Original",
            password="original-secret",
        )
        original_hash = original.password_hash
        repeated = create_admin_user(
            db,
            service,
            account=" ADMIN ",
            nickname="Changed",
            password="replacement-secret",
        )
        assert repeated is original
        assert repeated.nickname == "Original"
        assert repeated.password_hash == original_hash
        assert service.verify_password("original-secret", repeated.password_hash)
        assert not service.verify_password("replacement-secret", repeated.password_hash)


def test_controlled_admin_creation_never_promotes_existing_student() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    service = AuthService(make_settings())
    with Session(engine) as db:
        student = service.register(
            db, account="alice", nickname="Alice", password="student-secret"
        )
        original_hash = student.password_hash
        with pytest.raises(AdminAccountConflictError, match="账号已存在且不是管理员"):
            create_admin_user(
                db,
                service,
                account=" ALICE ",
                nickname="Attacker",
                password="replacement-secret",
            )
        assert student.role is UserRole.STUDENT
        assert student.password_hash == original_hash


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


def test_create_admin_main_is_idempotent_and_never_prints_password(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    settings = make_settings()
    service = AuthService(settings)
    with Session(engine) as db:
        admin = create_admin_user(
            db,
            service,
            account="admin",
            nickname="Original",
            password="original-secret",
        )
        original_hash = admin.password_hash

        @contextmanager
        def fake_session_scope():  # type: ignore[no-untyped-def]
            yield db

        monkeypatch.setattr(create_admin_module, "_session_scope", fake_session_scope)
        monkeypatch.setattr(create_admin_module, "_get_settings", lambda: settings)
        monkeypatch.setattr(
            create_admin_module.getpass,
            "getpass",
            lambda _prompt: "replacement-secret",
        )
        exit_code = create_admin_module.main(
            ["--account", "ADMIN", "--nickname", "Changed"]
        )

        captured = capsys.readouterr()
        assert exit_code == 0
        assert "admin" in captured.out
        assert "replacement-secret" not in captured.out + captured.err
        assert admin.password_hash == original_hash


@pytest.mark.parametrize(
    "business_error",
    [
        AccountExistsError("该账号已经存在，请直接登录。"),
        IntegrityError("insert", {}, RuntimeError("database detail")),
    ],
)
def test_create_admin_main_handles_expected_errors_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    business_error: Exception,
) -> None:
    settings = make_settings()

    @contextmanager
    def fake_session_scope():  # type: ignore[no-untyped-def]
        yield object()

    def fail_creation(*args: object, **kwargs: object) -> None:
        raise business_error

    monkeypatch.setattr(create_admin_module, "_session_scope", fake_session_scope)
    monkeypatch.setattr(create_admin_module, "_get_settings", lambda: settings)
    monkeypatch.setattr(create_admin_module, "create_admin_user", fail_creation)
    monkeypatch.setattr(
        create_admin_module.getpass,
        "getpass",
        lambda _prompt: "input-secret",
    )

    exit_code = create_admin_module.main(["--account", "admin", "--nickname", "Admin"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "管理员账号创建失败" in captured.err
    assert "Traceback" not in captured.err
    assert "input-secret" not in captured.out + captured.err


def test_create_admin_main_rejects_existing_student_without_promotion(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    settings = make_settings()
    service = AuthService(settings)
    with Session(engine) as db:
        student = service.register(
            db, account="alice", nickname="Alice", password="student-secret"
        )

        @contextmanager
        def fake_session_scope():  # type: ignore[no-untyped-def]
            yield db

        monkeypatch.setattr(create_admin_module, "_session_scope", fake_session_scope)
        monkeypatch.setattr(create_admin_module, "_get_settings", lambda: settings)
        monkeypatch.setattr(
            create_admin_module.getpass, "getpass", lambda _prompt: "input-secret"
        )
        exit_code = create_admin_module.main(
            ["--account", "alice", "--nickname", "Attacker"]
        )

        captured = capsys.readouterr()
        assert exit_code == 1
        assert "管理员账号创建失败" in captured.err
        assert "input-secret" not in captured.out + captured.err
        assert student.role is UserRole.STUDENT


def test_create_admin_main_redacts_settings_validation_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    visible_secret = "visible-test-secret"
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("APP_AUTH_SECRET", visible_secret)
    monkeypatch.setenv(
        "OBJECT_ENCRYPTION_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
    )
    monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:///:memory:")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/15")
    monkeypatch.setenv("CHECKPOINT_BACKEND", "sqlite")
    get_settings.cache_clear()

    @contextmanager
    def fake_session_scope():  # type: ignore[no-untyped-def]
        yield object()

    monkeypatch.setattr(create_admin_module, "_session_scope", fake_session_scope)
    monkeypatch.setattr(
        create_admin_module.getpass, "getpass", lambda _prompt: "input-password"
    )

    exit_code = create_admin_module.main(["--account", "admin", "--nickname", "Admin"])
    captured = capsys.readouterr()
    get_settings.cache_clear()

    assert exit_code == 1
    assert captured.err.strip() == "管理员账号创建失败。"
    assert visible_secret not in captured.out + captured.err
    assert "input-password" not in captured.out + captured.err
    assert "Traceback" not in captured.err
    assert "input_value" not in captured.err


@pytest.mark.parametrize("terminal_error", [EOFError(), KeyboardInterrupt()])
def test_create_admin_main_redacts_terminal_input_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    terminal_error: BaseException,
) -> None:
    def fail_password_read(_prompt: str) -> str:
        raise terminal_error

    monkeypatch.setattr(create_admin_module.getpass, "getpass", fail_password_read)

    exit_code = create_admin_module.main(["--account", "admin", "--nickname", "Admin"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.err.strip() == "管理员账号创建失败。"
    assert "Traceback" not in captured.err
