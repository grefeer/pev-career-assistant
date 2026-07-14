from collections.abc import Iterator
import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.api import dependencies
from backend.app.config import Settings
from backend.app.db.base import Base
from backend.app.db.models import AnalysisSession, User, UserRole

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("APP_AUTH_SECRET", "test-secret-with-at-least-32-characters")
os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")
os.environ.setdefault(
    "OBJECT_ENCRYPTION_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
)

from backend.app.main import create_app


@pytest.fixture
def settings() -> Settings:
    return Settings(
        app_env="test",
        app_auth_secret="test-secret-with-at-least-32-characters",
        object_encryption_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        database_url="sqlite+pysqlite:///:memory:",
        redis_url="redis://localhost:6379/15",
        checkpoint_backend="sqlite",
    )


@pytest.fixture
def client(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    def override_db() -> Iterator[Session]:
        with session_factory() as db:
            yield db

    monkeypatch.setattr(dependencies, "get_settings", lambda: settings)
    app = create_app(settings)
    app.dependency_overrides[dependencies._get_db] = override_db
    with TestClient(app) as test_client:
        test_client.session_factory = session_factory  # type: ignore[attr-defined]
        yield test_client


def register(client: TestClient, account: str = "alice") -> dict[str, object]:
    response = client.post(
        "/api/auth/register",
        json={"account": account, "nickname": account.title(), "password": "secret12"},
    )
    assert response.status_code == 200
    return response.json()


def test_register_returns_compatible_profile_and_creates_student_default_session(
    client: TestClient,
) -> None:
    body = register(client)

    assert body["ok"] is True
    assert body["message"] == "注册成功，已为你创建默认分析空间。"
    assert isinstance(body["token"], str)
    profile = body["profile"]
    assert profile["account"] == "alice"
    assert profile["nickname"] == "Alice"
    assert profile["role"] == "student"
    assert profile["active_thread_id"] == profile["sessions"][0]["thread_id"]
    assert profile["sessions"][0]["label"] == "分析会话 1"
    assert set(profile) == {
        "account",
        "nickname",
        "role",
        "created_at",
        "last_login_at",
        "active_thread_id",
        "sessions",
    }

    session_factory = client.session_factory  # type: ignore[attr-defined]
    with session_factory() as db:
        user = db.scalar(select(User).where(User.account == "alice"))
        assert user is not None and user.role is UserRole.STUDENT
        assert db.scalar(
            select(AnalysisSession).where(AnalysisSession.user_id == user.id)
        ) is not None


def test_public_registration_cannot_choose_admin_role(client: TestClient) -> None:
    response = client.post(
        "/api/auth/register",
        json={
            "account": "alice",
            "nickname": "Alice",
            "password": "secret12",
            "role": "admin",
        },
    )

    assert response.status_code == 200
    assert response.json()["profile"]["role"] == "student"


def test_duplicate_registration_returns_409(client: TestClient) -> None:
    register(client)

    response = client.post(
        "/api/auth/register",
        json={"account": " ALICE ", "nickname": "Other", "password": "secret12"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "该账号已经存在，请直接登录。"


def test_login_and_me_keep_frontend_contract(client: TestClient) -> None:
    register(client)
    response = client.post(
        "/api/auth/login", json={"account": "ALICE", "password": "secret12"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["message"] == "登录成功。"
    me = client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {body['token']}"}
    )
    assert me.status_code == 200
    assert me.json()["account"] == "alice"
    assert me.json()["role"] == "student"


@pytest.mark.parametrize(
    "payload",
    [
        {"account": "missing", "password": "secret12"},
        {"account": "alice", "password": "incorrect"},
    ],
)
def test_bad_account_or_password_returns_401(
    client: TestClient, payload: dict[str, str]
) -> None:
    register(client)

    response = client.post("/api/auth/login", json=payload)

    assert response.status_code == 401
    assert response.json()["detail"] == "账号或密码不正确。"


@pytest.mark.parametrize("token", [None, "invalid-token"])
def test_me_rejects_missing_or_invalid_token(client: TestClient, token: str | None) -> None:
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    response = client.get("/api/auth/me", headers=headers)

    assert response.status_code == 401


def test_factory_settings_are_used_for_issuing_and_decoding_tokens() -> None:
    custom_settings = Settings(
        app_env="test",
        app_auth_secret="different-factory-secret-at-least-32-characters",
        object_encryption_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        database_url="sqlite+pysqlite:///:memory:",
        redis_url="redis://localhost:6379/15",
        checkpoint_backend="sqlite",
    )
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    def override_db() -> Iterator[Session]:
        with session_factory() as db:
            yield db

    app = create_app(custom_settings)
    app.dependency_overrides[dependencies._get_db] = override_db
    with TestClient(app) as custom_client:
        registered = custom_client.post(
            "/api/auth/register",
            json={
                "account": "factory-user",
                "nickname": "Factory",
                "password": "secret12",
            },
        )
        assert registered.status_code == 200
        token = registered.json()["token"]

        assert custom_client.get(
            "/api/auth/me", headers={"Authorization": f"Bearer {token}"}
        ).status_code == 200
        assert custom_client.get(
            "/api/sessions", headers={"Authorization": f"Bearer {token}"}
        ).status_code == 200
        assert custom_client.post(
            "/api/analysis/run",
            headers={"Authorization": f"Bearer {token}"},
            data={"thread_id": "not-owned"},
        ).status_code == 404

        logged_in = custom_client.post(
            "/api/auth/login",
            json={"account": "factory-user", "password": "secret12"},
        )
        assert logged_in.status_code == 200
        login_token = logged_in.json()["token"]
        assert custom_client.get(
            "/api/auth/me", headers={"Authorization": f"Bearer {login_token}"}
        ).status_code == 200
        assert custom_client.get(
            "/api/sessions", headers={"Authorization": f"Bearer {login_token}"}
        ).status_code == 200
