from __future__ import annotations

from collections.abc import Iterator
import logging
from typing import Annotated, cast

import jwt
from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from backend.app.config import get_settings
from backend.app.db.models import Device, User, UserRole
from backend.app.repositories.users import get_by_id
from backend.app.services.auth import AuthService
from backend.app.services.company_research.service import CompanyResearchService
from backend.app.services.application_tracking.service import (
    ApplicationTrackingService,
)
from backend.app.services.interview_prep.service import InterviewPrepService
from backend.app.services.job_sync import JobSyncService
from backend.app.services.storage import EncryptedObjectStore
from backend.app.services.tencent_smartsheet import TencentSmartsheetGateway


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


def get_job_sync_service(request: Request) -> JobSyncService:
    injected = getattr(request.app.state, "job_sync_service", None)
    if injected is not None:
        return cast(JobSyncService, injected)
    settings = request.app.state.settings
    secret = settings.tencent_docs_token
    token = secret.get_secret_value() if secret is not None else None
    return JobSyncService(
        TencentSmartsheetGateway(token=token),
        discovery_enabled=settings.job_discovery_enabled,
        discovery_agent_version=settings.job_discovery_agent_version,
    )


def get_company_research_service(request: Request) -> CompanyResearchService:
    injected = getattr(request.app.state, "company_research_service", None)
    if injected is not None:
        return cast(CompanyResearchService, injected)
    settings = request.app.state.settings
    object_store = getattr(request.app.state, "object_store", None)
    return CompanyResearchService(settings, object_store=object_store)


def get_interview_prep_service(request: Request) -> InterviewPrepService:
    """Resolve the interview-prep service.

    Prefers a lifespan-injected service (which carries the LLM generator when
    a key is available). Falls back to a generator-less service so the app
    still boots - kits then finalize as ``failed`` with
    ``interview_prep_generator_unavailable`` until a key is configured.
    """
    injected = getattr(request.app.state, "interview_prep_service", None)
    if injected is not None:
        return cast(InterviewPrepService, injected)
    settings = request.app.state.settings
    return InterviewPrepService(settings)


def get_application_tracking_service(request: Request) -> ApplicationTrackingService:
    """Resolve the application-tracking service.

    No LLM / object store is needed, so the service is cheap to build per
    request.  A lifespan-injected instance is still preferred when present.
    """
    injected = getattr(request.app.state, "application_tracking_service", None)
    if injected is not None:
        return cast(ApplicationTrackingService, injected)
    settings = request.app.state.settings
    return ApplicationTrackingService(settings)


def get_device_service(request: Request, redis_client=Depends(get_redis)):
    from backend.app.services.devices import DeviceService

    return DeviceService(
        redis_client, lease_secret=request.app.state.settings.app_auth_secret
    )


def _require_task_scope(
    *,
    db: Session,
    device: Device,
    service,
    task_id: str | None,
    task_lease: str | None,
    required_scope: str,
) -> Device:
    from backend.app.services.devices import InvalidTaskLeaseError

    if not task_id or not task_lease:
        raise HTTPException(status_code=401, detail="任务租约无效。")
    try:
        service.verify_task_lease(
            db,
            task_lease,
            device=device,
            task_id=task_id,
            required_scope=required_scope,
        )
    except InvalidTaskLeaseError:
        raise HTTPException(status_code=401, detail="任务租约无效。") from None
    return device


def require_task_progress_lease(
    db: Annotated[Session, Depends(_get_db)],
    device: Annotated[Device, Depends(get_current_device)],
    service=Depends(get_device_service),
    task_id: Annotated[str | None, Header(alias="X-Task-ID")] = None,
    task_lease: Annotated[str | None, Header(alias="X-Task-Lease")] = None,
) -> Device:
    return _require_task_scope(
        db=db,
        device=device,
        service=service,
        task_id=task_id,
        task_lease=task_lease,
        required_scope="task:progress",
    )


def require_task_result_lease(
    db: Annotated[Session, Depends(_get_db)],
    device: Annotated[Device, Depends(get_current_device)],
    service=Depends(get_device_service),
    task_id: Annotated[str | None, Header(alias="X-Task-ID")] = None,
    task_lease: Annotated[str | None, Header(alias="X-Task-Lease")] = None,
) -> Device:
    return _require_task_scope(
        db=db,
        device=device,
        service=service,
        task_id=task_id,
        task_lease=task_lease,
        required_scope="task:result",
    )


require_task_lease = require_task_progress_lease


def get_current_device(
    db: Annotated[Session, Depends(_get_db)],
    service=Depends(get_device_service),
    device_token: Annotated[str | None, Header(alias="X-Device-Token")] = None,
) -> Device:
    device = service.authenticate(db, device_token) if device_token else None
    if device is None:
        logger.warning("device authentication rejected")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="设备令牌无效。",
        )
    return device


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
    if current_user.role is not UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要管理员权限。",
        )
    return current_user
