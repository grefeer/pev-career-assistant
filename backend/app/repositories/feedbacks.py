from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.db.models import JobFeedback
from backend.app.domain.feedbacks import JobFeedbackCategory


def create_feedback(
    db: Session, *,
    job_id: str, user_id: str, category: JobFeedbackCategory,
    note: str | None, idempotency_key: str,
) -> JobFeedback:
    item = JobFeedback(
        job_id=job_id, user_id=user_id, category=category,
        note=note, idempotency_key=idempotency_key,
        created_at=datetime.now(timezone.utc),
    )
    db.add(item)
    db.flush()
    return item


def get_by_id(db: Session, *, feedback_id: str) -> JobFeedback | None:
    return db.scalar(select(JobFeedback).where(JobFeedback.id == feedback_id))


def get_by_idempotency_key(db: Session, *, idempotency_key: str) -> JobFeedback | None:
    return db.scalar(
        select(JobFeedback).where(JobFeedback.idempotency_key == idempotency_key)
    )


def list_by_user(
    db: Session, *, user_id: str, job_id: str | None = None,
    limit: int = 50, offset: int = 0,
) -> tuple[int, Sequence[JobFeedback]]:
    conditions = [JobFeedback.user_id == user_id]
    if job_id is not None:
        conditions.append(JobFeedback.job_id == job_id)
    total = db.scalar(select(func.count()).select_from(JobFeedback).where(*conditions)) or 0
    rows = db.scalars(
        select(JobFeedback).where(*conditions)
        .order_by(JobFeedback.created_at.desc(), JobFeedback.id.desc())
        .limit(limit).offset(offset)
    ).all()
    return int(total), list(rows)


def list_by_job(db: Session, *, job_id: str, limit: int = 50, offset: int = 0) -> tuple[int, Sequence[JobFeedback]]:
    condition = JobFeedback.job_id == job_id
    total = db.scalar(select(func.count()).select_from(JobFeedback).where(condition)) or 0
    rows = db.scalars(
        select(JobFeedback).where(condition)
        .order_by(JobFeedback.created_at.desc(), JobFeedback.id.desc())
        .limit(limit).offset(offset)
    ).all()
    return int(total), list(rows)


def list_all(
    db: Session, *, limit: int = 50, offset: int = 0,
) -> tuple[int, Sequence[JobFeedback]]:
    total = db.scalar(select(func.count()).select_from(JobFeedback)) or 0
    rows = db.scalars(
        select(JobFeedback)
        .order_by(JobFeedback.created_at.desc(), JobFeedback.id.desc())
        .limit(limit).offset(offset)
    ).all()
    return int(total), list(rows)
