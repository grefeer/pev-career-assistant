from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.api.dependencies import _get_db, get_current_user, get_redis, require_admin
from backend.app.api.job_feedback_schemas import (
    AdminFeedbackAggregate,
    AdminFeedbackDecisionRequest,
    AdminFeedbackDetail,
    AdminFeedbackQueueResponse,
    FeedbackMutationRequest,
    FeedbackMutationResponse,
    StudentFeedbackItem,
    StudentFeedbackListResponse,
)
from backend.app.db.models import User, UserRole
from backend.app.domain.job_feedback import (
    IDEMPOTENCY_KEY_PATTERN,
    JobFeedbackCategory,
    JobFeedbackStatus,
)
from backend.app.repositories import job_feedback as repository
from backend.app.repositories import jobs as jobs_repository
from backend.app.services.job_feedback import (
    FeedbackJobNotFoundError,
    FeedbackNotFoundError,
    IdempotencyKeyConflictError,
    InvalidFeedbackNoteError,
    InvalidFeedbackTransitionError,
    JobFeedbackService,
    StaleFeedbackError,
)
from backend.app.services.rate_limit import (
    RateLimitExceededError,
    RateLimitUnavailableError,
    RedisFixedWindowRateLimiter,
)


router = APIRouter(tags=["job-feedback"])
logger = logging.getLogger(__name__)


def _error(status_code: int, error_code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"error_code": error_code, "message": message},
    )


def _validated_key(raw: str | None) -> str:
    if raw is None or IDEMPOTENCY_KEY_PATTERN.fullmatch(raw) is None:
        raise _error(422, "invalid_idempotency_key", "Idempotency-Key 格式无效。")
    return raw


def _require_student(user: User) -> None:
    if user.role is not UserRole.STUDENT:
        raise _error(403, "student_role_required", "只有学生账号可以提交职位反馈。")


def _enforce_write_limit(
    request: Request, redis_client: Any, *, user_id: str, limit: int
) -> None:
    limiter = getattr(
        request.app.state,
        "job_feedback_rate_limiter",
        RedisFixedWindowRateLimiter(
            redis_client,
            secret=(
                request.app.state.settings.rate_limit_hmac_secret.get_secret_value()
                if request.app.state.settings.rate_limit_hmac_secret
                else None
            ),
        ),
    )
    try:
        limiter.check(action="job-feedback-write", identity=user_id, limit=limit)
    except RateLimitExceededError:
        raise _error(429, "feedback_rate_limited", "反馈操作过于频繁，请稍后重试。") from None
    except RateLimitUnavailableError:
        raise _error(503, "feedback_rate_limit_unavailable", "反馈保护服务暂不可用。") from None


def _service_error(error: Exception) -> HTTPException:
    if isinstance(error, FeedbackJobNotFoundError):
        return _error(404, "feedback_job_not_found", "职位不存在。")
    if isinstance(error, FeedbackNotFoundError):
        return _error(404, "feedback_not_found", "反馈不存在。")
    if isinstance(error, StaleFeedbackError):
        return _error(409, "stale_job_feedback", "反馈版本已变化，请重新加载。")
    if isinstance(error, IdempotencyKeyConflictError):
        return _error(409, "idempotency_key_reused", "该 Idempotency-Key 已用于不同请求。")
    if isinstance(error, InvalidFeedbackTransitionError):
        return _error(409, "invalid_feedback_transition", "当前反馈状态不允许此操作。")
    if isinstance(error, InvalidFeedbackNoteError):
        return _error(422, "invalid_feedback_note", "反馈说明超过长度限制。")
    if isinstance(error, IntegrityError):
        return _error(409, "feedback_write_conflict", "反馈已变化，请重新加载。")
    raise error


@router.get("/jobs/{job_id}/feedback", response_model=StudentFeedbackListResponse)
def list_my_job_feedback(
    job_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(_get_db)],
) -> StudentFeedbackListResponse:
    _require_student(current_user)
    if jobs_repository.get_public_posting(db, job_id) is None:
        raise _error(404, "feedback_job_not_found", "职位不存在。")
    rows = repository.list_user_feedback(db, user_id=current_user.id, job_id=job_id)
    return StudentFeedbackListResponse(
        feedback=[StudentFeedbackItem.model_validate(row, from_attributes=True) for row in rows]
    )


@router.post("/jobs/{job_id}/feedback", response_model=FeedbackMutationResponse)
def mutate_my_job_feedback(
    job_id: str,
    payload: FeedbackMutationRequest,
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(_get_db)],
    redis_client: Annotated[Any, Depends(get_redis)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> FeedbackMutationResponse:
    _require_student(current_user)
    key = _validated_key(idempotency_key)
    _enforce_write_limit(request, redis_client, user_id=current_user.id, limit=20)
    try:
        result = JobFeedbackService().mutate_student(
            db, job_id=job_id, actor_user_id=current_user.id,
            idempotency_key=key, action=payload.action, category=payload.category,
            expected_version=payload.expected_version, note=payload.note,
        )
        db.commit()
    except Exception as error:
        db.rollback()
        mapped = _service_error(error)
        logger.info(
            "job feedback rejected job_id=%s error_code=%s",
            job_id,
            mapped.detail["error_code"],
        )
        raise mapped from None
    logger.info(
        "job feedback mutated feedback_id=%s job_id=%s category=%s status=%s version=%s",
        result.id, result.job_id, result.category.value, result.status.value, result.version,
    )
    return FeedbackMutationResponse.model_validate(result, from_attributes=True)


@router.get("/admin/job-feedback", response_model=AdminFeedbackQueueResponse)
def list_admin_job_feedback(
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(_get_db)],
    status: JobFeedbackStatus | None = None,
    category: JobFeedbackCategory | None = None,
    limit: int = 20,
    offset: int = 0,
) -> AdminFeedbackQueueResponse:
    del admin
    if not 1 <= limit <= 100 or offset < 0:
        raise _error(422, "invalid_feedback_pagination", "分页参数无效。")
    total, rows = repository.list_admin_feedback(
        db, status=status, category=category, limit=limit, offset=offset
    )
    aggregates = repository.aggregate_admin_feedback(db)
    return AdminFeedbackQueueResponse(
        total=total,
        feedback=[
            AdminFeedbackDetail(
                id=row.feedback.id, job_id=row.feedback.job_id,
                company_name=row.company_name, title=row.title,
                job_status=row.job_status.value,
                job_review_version=row.job_review_version,
                category=row.feedback.category, status=row.feedback.status,
                note=row.feedback.note, version=row.feedback.version,
                created_at=row.feedback.created_at, updated_at=row.feedback.updated_at,
            )
            for row in rows
        ],
        aggregates=[
            AdminFeedbackAggregate.model_validate(row, from_attributes=True)
            for row in aggregates
        ],
    )


@router.post(
    "/admin/job-feedback/{feedback_id}/decision",
    response_model=FeedbackMutationResponse,
)
def decide_admin_job_feedback(
    feedback_id: str,
    payload: AdminFeedbackDecisionRequest,
    request: Request,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(_get_db)],
    redis_client: Annotated[Any, Depends(get_redis)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> FeedbackMutationResponse:
    key = _validated_key(idempotency_key)
    _enforce_write_limit(request, redis_client, user_id=admin.id, limit=60)
    try:
        result = JobFeedbackService().decide_admin(
            db, feedback_id=feedback_id, actor_user_id=admin.id,
            idempotency_key=key, decision=payload.decision,
            expected_version=payload.expected_version,
        )
        db.commit()
    except Exception as error:
        db.rollback()
        mapped = _service_error(error)
        logger.info(
            "job feedback decision rejected feedback_id=%s error_code=%s",
            feedback_id,
            mapped.detail["error_code"],
        )
        raise mapped from None
    logger.info(
        "job feedback decided feedback_id=%s job_id=%s status=%s version=%s",
        result.id, result.job_id, result.status.value, result.version,
    )
    return FeedbackMutationResponse.model_validate(result, from_attributes=True)
