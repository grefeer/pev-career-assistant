from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from backend.app.config import get_settings
from backend.app.db.models import User, UserRole
from backend.app.repositories.users import get_by_id
from backend.app.services.auth import AuthService


bearer_scheme = HTTPBearer(auto_error=False)


def _get_db() -> Iterator[Session]:
    # Keep engine creation lazy so importing auth dependencies does not require a
    # live database configuration (notably in isolated unit tests).
    from backend.app.db.session import get_db

    yield from get_db()


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="无法验证身份。",
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_current_user(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(bearer_scheme)
    ],
    db: Annotated[Session, Depends(_get_db)],
    request: Request = None,
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
    if current_user.role is not UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要管理员权限。",
        )
    return current_user
