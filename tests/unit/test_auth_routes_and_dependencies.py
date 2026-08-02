"""HTTP and dependency-injection contract tests for auth routes and shared dependencies."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.api import dependencies as deps
from backend.app.api.dependencies import (
    get_agent_run_service,
    get_current_user,
    get_object_store,
    get_redis,
    require_admin,
)
from backend.app.api.routes import auth as auth_routes
from backend.app.api.routes.auth import _enforce_auth_rate_limit, router as auth_router
from backend.app.db.base import Base
from backend.app.db.models import User, UserRole
from backend.app.services.auth import AuthService
from backend.app.services.rate_limit import (
    RateLimitExceededError,
    RateLimitUnavailableError,
)
from tests.conftest import settings_override


def _build_auth_app(**settings_overrides: Any) -> FastAPI:
    """Build an app with the auth router backed by an in-memory SQLite DB."""
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    app = FastAPI()
    app.state.settings = settings_override(**settings_overrides)
    app.state.session_factory = factory
    app.state.redis = MagicMock()
    app.include_router(auth_router)
    return app


def _build_deps_app() -> FastAPI:
    """Build a minimal app exposing each shared dependency on a test route."""
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    app = FastAPI()
    app.state.settings = settings_override()
    app.state.session_factory = factory
    app.state.redis = MagicMock()
    app.state.object_store = MagicMock()

    @app.get("/_object-store", response_model=None)
    def _object_store_route(store: Any = Depends(get_object_store)) -> dict[str, Any]:
        return {"ok": True}

    @app.get("/_redis", response_model=None)
    def _redis_route(redis: Any = Depends(get_redis)) -> dict[str, Any]:
        return {"ok": True}

    @app.get("/_agent-run-service", response_model=None)
    def _agent_run_service_route(
        svc: Any = Depends(get_agent_run_service),
    ) -> dict[str, Any]:
        return {"ok": True}

    @app.get("/_current-user", response_model=None)
    def _current_user_route(user: User = Depends(get_current_user)) -> dict[str, str]:
        return {"account": user.account}

    @app.get("/_admin", response_model=None)
    def _admin_route(user: User = Depends(require_admin)) -> dict[str, str]:
        return {"account": user.account}

    return app


def _register_user(app: FastAPI, *, account: str, nickname: str) -> str:
    """Register via the auth router and return the issued token."""
    response = TestClient(app).post(
        "/auth/register",
        json={"account": account, "nickname": nickname, "password": "supersecret"},
    )
    assert response.status_code == 200
    return response.json()["token"]


# ---------------------------------------------------------------------------
# Auth route happy-path and error contracts
# ---------------------------------------------------------------------------


def test_register_returns_token_and_profile() -> None:
    """Successful registration issues a JWT and whitelisted profile."""
    app = _build_auth_app()
    response = TestClient(app).post(
        "/auth/register",
        json={
            "account": "alice@example.test",
            "nickname": "Alice",
            "password": "supersecret",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["token"]
    assert body["profile"]["account"] == "alice@example.test"
    assert body["profile"]["role"] == "student"
    assert body["profile"]["last_login_at"]


def test_register_conflict_returns_409() -> None:
    """Re-registering the same account surfaces a 409 conflict."""
    app = _build_auth_app()
    client = TestClient(app)
    payload = {
        "account": "bob@example.test",
        "nickname": "Bob",
        "password": "supersecret",
    }
    assert client.post("/auth/register", json=payload).status_code == 200
    second = client.post("/auth/register", json=payload)
    assert second.status_code == 409
    assert "already exists" in second.json()["detail"] or "已经存在" in second.json()["detail"]


def test_login_returns_token_and_profile() -> None:
    """Successful login issues a fresh JWT and profile snapshot."""
    app = _build_auth_app()
    client = TestClient(app)
    _register_user(app, account="carol@example.test", nickname="Carol")
    response = client.post(
        "/auth/login",
        json={"account": "carol@example.test", "password": "supersecret"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["token"]
    assert body["profile"]["account"] == "carol@example.test"


def test_login_invalid_credentials_returns_401() -> None:
    """Wrong password yields a 401 without leaking which field failed."""
    app = _build_auth_app()
    client = TestClient(app)
    _register_user(app, account="dave@example.test", nickname="Dave")
    response = client.post(
        "/auth/login",
        json={"account": "dave@example.test", "password": "wrongpassword"},
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "账号或密码不正确。"


def test_me_returns_profile() -> None:
    """/me echoes the caller profile from the bearer token."""
    app = _build_auth_app()
    client = TestClient(app)
    token = _register_user(app, account="eve@example.test", nickname="Eve")
    response = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json()["account"] == "eve@example.test"


def test_me_serializes_profile_without_last_login() -> None:
    """serialize_profile emits '' for last_login_at when it is None."""
    app = _build_auth_app()
    with app.state.session_factory() as db:
        db.add(
            User(
                id="nologin-user",
                account="nologin@example.test",
                nickname="NoLogin",
                password_hash="notreal",
                role=UserRole.STUDENT,
                is_active=True,
            )
        )
        db.commit()
    token = AuthService(app.state.settings).issue_user_token(
        SimpleNamespace(id="nologin-user", role=UserRole.STUDENT)
    )
    response = TestClient(app).get(
        "/auth/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    assert response.json()["last_login_at"] == ""


# ---------------------------------------------------------------------------
# Auth rate-limit branch coverage
# ---------------------------------------------------------------------------


def test_register_calls_register_ip_rate_limit() -> None:
    """Register path invokes the register-ip limit with a ceiling of 20."""
    app = _build_auth_app()
    limiter = MagicMock()
    app.state.auth_rate_limiter = limiter
    response = TestClient(app).post(
        "/auth/register",
        json={
            "account": "frank@example.test",
            "nickname": "Frank",
            "password": "supersecret",
        },
    )
    assert response.status_code == 200
    calls = limiter.check.call_args_list
    assert len(calls) == 1
    assert calls[0].kwargs == {
        "action": "register-ip",
        "identity": "testclient",
        "limit": 20,
    }


def test_login_calls_login_ip_and_account_rate_limit() -> None:
    """Login path invokes login-ip then login-account with the casefolded account."""
    app = _build_auth_app()
    client = TestClient(app)
    _register_user(app, account="grace@example.test", nickname="Grace")
    limiter = MagicMock()
    app.state.auth_rate_limiter = limiter
    response = client.post(
        "/auth/login",
        json={"account": "grace@example.test", "password": "supersecret"},
    )
    assert response.status_code == 200
    calls = limiter.check.call_args_list
    assert len(calls) == 2
    assert calls[0].kwargs == {
        "action": "login-ip",
        "identity": "testclient",
        "limit": 120,
    }
    assert calls[1].kwargs == {
        "action": "login-account",
        "identity": "grace@example.test",
        "limit": 8,
    }


def test_rate_limit_exceeded_raises_429() -> None:
    """RateLimitExceededError surfaces as a 429 to the client."""
    app = _build_auth_app()
    limiter = MagicMock()
    limiter.check.side_effect = RateLimitExceededError("too many")
    app.state.auth_rate_limiter = limiter
    response = TestClient(app).post(
        "/auth/register",
        json={
            "account": "henry@example.test",
            "nickname": "Henry",
            "password": "supersecret",
        },
    )
    assert response.status_code == 429


def test_rate_limit_unavailable_raises_503() -> None:
    """RateLimitUnavailableError surfaces as a 503 to the client."""
    app = _build_auth_app()
    limiter = MagicMock()
    limiter.check.side_effect = RateLimitUnavailableError("redis down")
    app.state.auth_rate_limiter = limiter
    response = TestClient(app).post(
        "/auth/login",
        json={"account": "irene@example.test", "password": "supersecret"},
    )
    assert response.status_code == 503


def test_rate_limit_fallback_constructs_redis_limiter_without_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-test env without an injected limiter constructs RedisFixedWindowRateLimiter."""
    app = _build_auth_app(app_env="development")
    spy = MagicMock()
    spy.return_value = MagicMock()
    monkeypatch.setattr(auth_routes, "RedisFixedWindowRateLimiter", spy)
    response = TestClient(app).post(
        "/auth/register",
        json={
            "account": "jack@example.test",
            "nickname": "Jack",
            "password": "supersecret",
        },
    )
    assert response.status_code == 200
    args, kwargs = spy.call_args
    assert args[0] is app.state.redis
    assert kwargs["secret"] is None


def test_rate_limit_fallback_constructs_redis_limiter_with_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured rate_limit_hmac_secret is passed through to the limiter."""
    app = _build_auth_app(
        app_env="development",
        rate_limit_hmac_secret=SecretStr("a" * 32),
    )
    spy = MagicMock()
    spy.return_value = MagicMock()
    monkeypatch.setattr(auth_routes, "RedisFixedWindowRateLimiter", spy)
    response = TestClient(app).post(
        "/auth/register",
        json={
            "account": "kate@example.test",
            "nickname": "Kate",
            "password": "supersecret",
        },
    )
    assert response.status_code == 200
    _, kwargs = spy.call_args
    assert kwargs["secret"] == "a" * 32


def test_enforce_auth_rate_limit_login_without_account_skips_account_check() -> None:
    """Direct call with a non-register action and account=None skips login-account."""
    limiter = MagicMock()
    app = FastAPI()
    app.state.settings = settings_override(app_env="test")
    app.state.auth_rate_limiter = limiter
    app.state.redis = MagicMock()
    request = MagicMock()
    request.app = app
    request.client = SimpleNamespace(host="127.0.0.1")
    request.headers = {}
    _enforce_auth_rate_limit(request, "login", account=None)
    limiter.check.assert_called_once_with(
        action="login-ip", identity="127.0.0.1", limit=120
    )


def test_enforce_auth_rate_limit_handles_missing_client() -> None:
    """When request.client is None, peer falls back to 'unknown'."""
    limiter = MagicMock()
    app = FastAPI()
    app.state.settings = settings_override(app_env="test")
    app.state.auth_rate_limiter = limiter
    app.state.redis = MagicMock()
    request = MagicMock()
    request.app = app
    request.client = None
    request.headers = {}
    _enforce_auth_rate_limit(request, "register")
    limiter.check.assert_called_once_with(
        action="register-ip", identity="unknown", limit=20
    )


# ---------------------------------------------------------------------------
# Shared dependency contracts (dependencies.py)
# ---------------------------------------------------------------------------


def test_get_object_store_returns_injected_store() -> None:
    """get_object_store returns the app-level object store."""
    app = _build_deps_app()
    response = TestClient(app).get("/_object-store")
    assert response.status_code == 200


def test_get_redis_returns_503_when_missing() -> None:
    """Missing redis client yields a 503 with a rate_limit_unavailable code."""
    app = _build_deps_app()
    app.state.redis = None
    response = TestClient(app).get("/_redis")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "rate_limit_unavailable"


def test_get_redis_returns_redis_when_present() -> None:
    """A present redis client is returned as-is."""
    app = _build_deps_app()
    response = TestClient(app).get("/_redis")
    assert response.status_code == 200


def test_get_agent_run_service_returns_injected_service() -> None:
    """An injected agent_run_service is returned without reconstruction."""
    app = _build_deps_app()
    app.state.agent_run_service = MagicMock()
    response = TestClient(app).get("/_agent-run-service")
    assert response.status_code == 200


def test_get_agent_run_service_falls_back_to_construct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without an injected service, AgentRunService is constructed from settings."""
    app = _build_deps_app()
    spy = MagicMock()
    monkeypatch.setattr(deps, "AgentRunService", spy)
    response = TestClient(app).get("/_agent-run-service")
    assert response.status_code == 200
    args, kwargs = spy.call_args
    assert args[0] is app.state.settings
    assert kwargs["runtime"] is None


def test_get_current_user_rejects_missing_credentials() -> None:
    """No Authorization header yields a 401."""
    app = _build_deps_app()
    response = TestClient(app).get("/_current-user")
    assert response.status_code == 401


def test_get_current_user_rejects_malformed_jwt() -> None:
    """A malformed token yields a 401 via the PyJWTError catch."""
    app = _build_deps_app()
    response = TestClient(app).get(
        "/_current-user", headers={"Authorization": "Bearer not.a.jwt"}
    )
    assert response.status_code == 401


def test_get_current_user_rejects_non_str_sub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-str sub claim is rejected even if decode succeeded."""
    app = _build_deps_app()
    fake_auth = MagicMock()
    fake_auth.return_value.decode_user_token.return_value = {"sub": 12345}
    monkeypatch.setattr(deps, "AuthService", fake_auth)
    response = TestClient(app).get(
        "/_current-user", headers={"Authorization": "Bearer faketoken"}
    )
    assert response.status_code == 401


def test_get_current_user_rejects_empty_string_sub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty-string sub claim is rejected after decode."""
    app = _build_deps_app()
    fake_auth = MagicMock()
    fake_auth.return_value.decode_user_token.return_value = {"sub": ""}
    monkeypatch.setattr(deps, "AuthService", fake_auth)
    response = TestClient(app).get(
        "/_current-user", headers={"Authorization": "Bearer faketoken"}
    )
    assert response.status_code == 401


def test_get_current_user_rejects_missing_user() -> None:
    """A valid JWT for a non-existent user yields a 401."""
    app = _build_deps_app()
    token = AuthService(app.state.settings).issue_user_token(
        SimpleNamespace(id="nonexistent-user", role=UserRole.STUDENT)
    )
    response = TestClient(app).get(
        "/_current-user", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 401


def test_get_current_user_rejects_inactive_user() -> None:
    """A valid JWT for an inactive user yields a 401."""
    app = _build_deps_app()
    with app.state.session_factory() as db:
        db.add(
            User(
                id="inactive-user",
                account="inactive@example.test",
                nickname="Inactive",
                password_hash="notreal",
                role=UserRole.STUDENT,
                is_active=False,
            )
        )
        db.commit()
    token = AuthService(app.state.settings).issue_user_token(
        SimpleNamespace(id="inactive-user", role=UserRole.STUDENT)
    )
    response = TestClient(app).get(
        "/_current-user", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 401


def test_get_current_user_returns_active_user() -> None:
    """A valid JWT for an active user returns the user."""
    app = _build_deps_app()
    with app.state.session_factory() as db:
        db.add(
            User(
                id="active-user",
                account="active@example.test",
                nickname="Active",
                password_hash="notreal",
                role=UserRole.STUDENT,
                is_active=True,
            )
        )
        db.commit()
    token = AuthService(app.state.settings).issue_user_token(
        SimpleNamespace(id="active-user", role=UserRole.STUDENT)
    )
    response = TestClient(app).get(
        "/_current-user", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    assert response.json()["account"] == "active@example.test"


def test_require_admin_rejects_non_admin() -> None:
    """A student role yields a 403 on the admin guard."""
    app = _build_deps_app()
    with app.state.session_factory() as db:
        db.add(
            User(
                id="student-user",
                account="student@example.test",
                nickname="Student",
                password_hash="notreal",
                role=UserRole.STUDENT,
                is_active=True,
            )
        )
        db.commit()
    token = AuthService(app.state.settings).issue_user_token(
        SimpleNamespace(id="student-user", role=UserRole.STUDENT)
    )
    response = TestClient(app).get(
        "/_admin", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 403


def test_require_admin_allows_admin() -> None:
    """An admin role passes the admin guard."""
    app = _build_deps_app()
    with app.state.session_factory() as db:
        db.add(
            User(
                id="admin-user",
                account="admin@example.test",
                nickname="Admin",
                password_hash="notreal",
                role=UserRole.ADMIN,
                is_active=True,
            )
        )
        db.commit()
    token = AuthService(app.state.settings).issue_user_token(
        SimpleNamespace(id="admin-user", role=UserRole.ADMIN)
    )
    response = TestClient(app).get(
        "/_admin", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200