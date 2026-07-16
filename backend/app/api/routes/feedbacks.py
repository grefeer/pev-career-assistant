from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from backend.app.api.dependencies import _get_db, get_current_user, require_admin
from backend.app.api.feedback_schemas import (
    AdminFeedbackListResponse,
    AdminFeedbackResponse,
    FeedbackCreateRequest,
    FeedbackListResponse,
    FeedbackResponse,
)
from backend.app.db.models import JobFeedback, User
from backend.app.services.feedbacks import (
    FeedbackService,
    IdempotentFeedbackError,
    JobFeedbackNotFoundError,
)
from backend.app.services.rate_limit import (
    RateLimitExceededError,
    RateLimitUnavailableError,
    RedisFixedWindowRateLimiter,
)


router = APIRouter(tags=["feedbacks"])


def _feedback_response(item: JobFeedback) -> FeedbackResponse:
    return FeedbackResponse(
        id=item.id, job_id=item.job_id, category=item.category,
        note=item.note, created_at=item.created_at,
    )


def _admin_response(item: JobFeedback) -> AdminFeedbackResponse:
    return AdminFeedbackResponse(
        id=item.id, job_id=item.job_id, category=item.category,
        note=item.note, created_at=item.created_at,
    )


def _get_feedback_service(request: Request) -> FeedbackService:
    return FeedbackService(
        rate_limiter=RedisFixedWindowRateLimiter(
            redis=request.app.state.redis,
            limit=60,
            window_seconds=60,
        )
    )


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, IdempotentFeedbackError):
        return HTTPException(
            409,
            detail={"code": exc.error_code, "message": "反馈已提交，请勿重复提交。"},
        )
    if isinstance(exc, JobFeedbackNotFoundError):
        return HTTPException(
            404,
            detail={"code": exc.error_code, "message": "反馈不存在。"},
        )
    if isinstance(exc, RateLimitExceededError):
        return HTTPException(
            429,
            detail={"code": "rate_limit_exceeded", "message": "请求过于频繁，请稍后重试。"},
        )
    if isinstance(exc, RateLimitUnavailableError):
        return HTTPException(
            503,
            detail={"code": "rate_limit_unavailable", "message": "频率限制服务暂时不可用。"},
        )
    raise exc


@router.post(
    "/feedbacks",
    response_model=FeedbackResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_feedback(
    body: FeedbackCreateRequest,
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(_get_db)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> FeedbackResponse:
    if not idempotency_key or len(idempotency_key) < 16 or len(idempotency_key) > 128:
        raise HTTPException(
            400,
            detail={
                "code": "invalid_idempotency_key",
                "message": "Idempotency-Key 必须为 16-128 个字符。",
            },
        )
    service = _get_feedback_service(request)
    try:
        item = service.create_feedback(
            db, job_id=body.job_id,
            user=current_user, category=body.category,
            note=body.note, idempotency_key=idempotency_key,
        )
        response = _feedback_response(item)
        db.commit()
        return response
    except (
        IdempotentFeedbackError,
        RateLimitExceededError,
        RateLimitUnavailableError,
    ) as exc:
        db.rollback()
        raise _error(exc) from None


@router.get("/feedbacks", response_model=FeedbackListResponse)
def list_feedbacks(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(_get_db)],
    job_id: Annotated[str | None, Query(alias="job_id")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> FeedbackListResponse:
    service = FeedbackService()
    total, items = service.list_user_feedback(
        db, user_id=current_user.id, job_id=job_id, limit=limit, offset=offset,
    )
    return FeedbackListResponse(
        total=total, feedbacks=[_feedback_response(item) for item in items],
    )


@router.get("/feedbacks/{feedback_id}", response_model=FeedbackResponse)
def get_feedback(
    feedback_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(_get_db)],
) -> FeedbackResponse:
    service = FeedbackService()
    try:
        item = service.get_feedback(db, feedback_id=feedback_id)
    except JobFeedbackNotFoundError as exc:
        raise _error(exc) from None
    if item.user_id != current_user.id:
        raise HTTPException(404, detail={"code": "feedback_not_found", "message": "反馈不存在。"})
    return _feedback_response(item)


@router.get("/admin/feedbacks", response_model=AdminFeedbackListResponse)
def admin_list_feedbacks(
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(_get_db)],
    job_id: Annotated[str | None, Query(alias="job_id")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AdminFeedbackListResponse:
    del admin
    if job_id:
        from backend.app.repositories import feedbacks as feedback_repo
        total, items = feedback_repo.list_by_job(
            db, job_id=job_id, limit=limit, offset=offset,
        )
    else:
        service = FeedbackService()
        total, items = service.list_all_feedback(db, limit=limit, offset=offset)
    return AdminFeedbackListResponse(
        total=total, feedbacks=[_admin_response(item) for item in items],
    )


@router.get("/admin/feedbacks/{feedback_id}", response_model=AdminFeedbackResponse)
def admin_get_feedback(
    feedback_id: str,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(_get_db)],
) -> AdminFeedbackResponse:
    del admin
    service = FeedbackService()
    try:
        item = service.get_feedback(db, feedback_id=feedback_id)
    except JobFeedbackNotFoundError as exc:
        raise _error(exc) from None
    return _admin_response(item)
