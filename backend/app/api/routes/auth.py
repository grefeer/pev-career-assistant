from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from backend.app.api.dependencies import _get_db, get_current_user
from backend.app.db.models import User
from backend.app.schemas import AuthRequest, AuthResponse, RegisterRequest, UserProfile
from backend.app.services.auth import AccountExistsError, AuthService
from backend.app.services.rate_limit import (
    RateLimitExceededError,
    RateLimitUnavailableError,
    RedisFixedWindowRateLimiter,
    resolve_client_ip,
)


router = APIRouter(prefix="/auth", tags=["auth"])
logger = logging.getLogger(__name__)


def _enforce_auth_rate_limit(
    request: Request, action: str, *, account: str | None = None
) -> None:
    if request.app.state.settings.app_env == "test" and not hasattr(
        request.app.state, "auth_rate_limiter"
    ):
        return
    limiter = getattr(
        request.app.state,
        "auth_rate_limiter",
        RedisFixedWindowRateLimiter(
            request.app.state.redis,
            secret=(
                request.app.state.settings.rate_limit_hmac_secret.get_secret_value()
                if request.app.state.settings.rate_limit_hmac_secret
                else None
            ),
        ),
    )
    peer = request.client.host if request.client else "unknown"
    identity = resolve_client_ip(
        peer,
        request.headers.get("X-Real-IP"),
        request.app.state.settings.trusted_proxy_cidrs,
    )
    try:
        if action == "register":
            limiter.check(action="register-ip", identity=identity, limit=20)
        else:
            limiter.check(action="login-ip", identity=identity, limit=120)
            if account is not None:
                limiter.check(
                    action="login-account",
                    identity=account.strip().casefold(),
                    limit=8,
                )
    except RateLimitExceededError:
        raise HTTPException(
            status_code=429, detail="请求过于频繁，请稍后重试。"
        ) from None
    except RateLimitUnavailableError:
        raise HTTPException(status_code=503, detail="认证保护服务暂不可用。") from None


def serialize_profile(user: User) -> dict[str, object]:
    """Return account metadata without recreating a legacy graph session."""
    return {
        "account": user.account,
        "nickname": user.nickname,
        "role": user.role.value,
        "created_at": user.created_at.isoformat(),
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else "",
    }


@router.post("/register", response_model=AuthResponse)
def register(
    payload: RegisterRequest,
    request: Request,
    db: Annotated[Session, Depends(_get_db)],
) -> AuthResponse:
    _enforce_auth_rate_limit(request, "register")
    service = AuthService(request.app.state.settings)
    try:
        user = service.register(
            db,
            account=payload.account,
            nickname=payload.nickname,
            password=payload.password,
        )
        db.commit()
    except AccountExistsError as error:
        db.rollback()
        logger.warning("registration rejected")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(error)
        ) from None

    return AuthResponse(
        ok=True,
        message="注册成功。",
        token=service.issue_user_token(user),
        profile=UserProfile(**serialize_profile(user)),
    )


@router.post("/login", response_model=AuthResponse)
def login(
    payload: AuthRequest,
    request: Request,
    db: Annotated[Session, Depends(_get_db)],
) -> AuthResponse:
    _enforce_auth_rate_limit(request, "login", account=payload.account)
    service = AuthService(request.app.state.settings)
    user = service.authenticate(db, account=payload.account, password=payload.password)
    if user is None:
        db.rollback()
        logger.warning("login rejected")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="账号或密码不正确。",
        )
    db.commit()
    return AuthResponse(
        ok=True,
        message="登录成功。",
        token=service.issue_user_token(user),
        profile=UserProfile(**serialize_profile(user)),
    )


@router.get("/me", response_model=UserProfile)
def me(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(_get_db)],
) -> UserProfile:
    return UserProfile(
        **serialize_profile(current_user)
    )
