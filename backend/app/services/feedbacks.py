from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from backend.app.db.models import JobFeedback, User, UserRole
from backend.app.domain.feedbacks import JobFeedbackCategory
from backend.app.repositories import feedbacks as feedback_repo
from backend.app.services.rate_limit import (
    RateLimitExceededError,
    RateLimitUnavailableError,
    RedisFixedWindowRateLimiter,
)


STUDENT_RATE_LIMIT = 20
ADMIN_RATE_LIMIT = 60
RATE_LIMIT_WINDOW = 60

logger = logging.getLogger(__name__)


class IdempotentFeedbackError(RuntimeError):
    error_code = "duplicate_feedback"


class JobFeedbackNotFoundError(LookupError):
    error_code = "feedback_not_found"


class FeedbackService:
    def __init__(self, rate_limiter: RedisFixedWindowRateLimiter | None = None) -> None:
        self._rate_limiter = rate_limiter

    @staticmethod
    def _check_idempotency(db: Session, *, idempotency_key: str) -> None:
        existing = feedback_repo.get_by_idempotency_key(db, idempotency_key=idempotency_key)
        if existing is not None:
            raise IdempotentFeedbackError(idempotency_key)

    def _enforce_rate_limit(self, user: User) -> None:
        if self._rate_limiter is None:
            return
        limit = ADMIN_RATE_LIMIT if user.role is UserRole.ADMIN else STUDENT_RATE_LIMIT
        try:
            self._rate_limiter.check(
                action="feedback",
                identity=user.id,
                limit=limit,
            )
        except RateLimitExceededError:
            raise
        except Exception as exc:
            raise RateLimitUnavailableError("feedback rate limiter unavailable") from exc

    def create_feedback(
        self, db: Session, *,
        job_id: str, user: User, category: JobFeedbackCategory,
        note: str | None, idempotency_key: str,
    ) -> JobFeedback:
        """Create feedback. Does NOT change JobPosting.status."""
        self._enforce_rate_limit(user)
        self._check_idempotency(db, idempotency_key=idempotency_key)
        item = feedback_repo.create_feedback(
            db, job_id=job_id, user_id=user.id,
            category=category, note=note, idempotency_key=idempotency_key,
        )
        logger.info(
            "feedback created",
            extra={"feedback_id": item.id, "job_id": job_id, "category": category.value},
        )
        return item

    def get_feedback(self, db: Session, *, feedback_id: str) -> JobFeedback:
        item = feedback_repo.get_by_id(db, feedback_id=feedback_id)
        if item is None:
            raise JobFeedbackNotFoundError(feedback_id)
        return item

    def list_user_feedback(
        self, db: Session, *, user_id: str, job_id: str | None = None,
        limit: int = 50, offset: int = 0,
    ) -> tuple[int, list[JobFeedback]]:
        return feedback_repo.list_by_user(
            db, user_id=user_id, job_id=job_id, limit=limit, offset=offset,
        )

    def list_all_feedback(
        self, db: Session, *, limit: int = 50, offset: int = 0,
    ) -> tuple[int, list[JobFeedback]]:
        return feedback_repo.list_all(db, limit=limit, offset=offset)
