from __future__ import annotations

from collections.abc import Iterator
import logging
from typing import Annotated, cast

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from backend.app.config import get_settings
from backend.app.db.models import User, UserRole
from backend.app.repositories.users import get_by_id
from backend.app.services.auth import AuthService
from backend.app.services.agent_runtime.service import AgentRunService
from backend.app.services.storage import EncryptedObjectStore


bearer_scheme = HTTPBearer(auto_error=False)
logger = logging.getLogger(__name__)


def _get_db(request: Request) -> Iterator[Session]:
    with request.app.state.session_factory() as db:
        yield db


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="无法验证身份。",
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_object_store(request: Request) -> EncryptedObjectStore:
    return cast(EncryptedObjectStore, request.app.state.object_store)


def get_redis(request: Request):
    redis_client = getattr(request.app.state, "redis", None)
    if redis_client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "rate_limit_unavailable",
                "message": "频率限制服务暂时不可用。",
            },
        )
    return redis_client


def get_agent_run_service(request: Request) -> AgentRunService:
    """Resolve the app-scoped adaptive PEV service without rebuilding models."""
    injected = getattr(request.app.state, "agent_run_service", None)
    if injected is not None:
        return cast(AgentRunService, injected)
    return AgentRunService(
        request.app.state.settings,
        runtime=getattr(request.app.state, "agent_runtime", None),
    )


def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    db: Annotated[Session, Depends(_get_db)],
    request: Request = cast(Request, None),
) -> User:
    if credentials is None:
        raise _unauthorized()

    try:
        settings = request.app.state.settings if request is not None else get_settings()
        claims = AuthService(settings).decode_user_token(credentials.credentials)
        user_id = claims["sub"]
        if not isinstance(user_id, str) or not user_id:
            raise _unauthorized()
    except (jwt.PyJWTError, KeyError, TypeError):
        raise _unauthorized() from None

    user = get_by_id(db, user_id)
    if user is None or not user.is_active:
        raise _unauthorized()
    return user


def require_admin(current_user: Annotated[User, Depends(get_current_user)]) -> User:
    """Keep the generic role guard for future protected operations."""
    if current_user.role is not UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要管理员权限。",
        )
    return current_user

