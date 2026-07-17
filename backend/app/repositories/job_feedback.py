from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from backend.app.db.models import (
    JobFeedback,
    JobFeedbackEvent,
    JobPosting,
    JobPostingStatus,
)
from backend.app.domain.job_feedback import JobFeedbackCategory, JobFeedbackStatus


@dataclass(frozen=True)
class AdminFeedbackRow:
    feedback: JobFeedback
    company_name: str
    title: str
    job_status: JobPostingStatus
    job_review_version: int


@dataclass(frozen=True)
class AdminFeedbackAggregate:
    job_id: str
    company_name: str
    title: str
    category: JobFeedbackCategory
    open_count: int
    accepted_count: int
    total_count: int
    latest_updated_at: datetime


def lock_verified_job(db: Session, job_id: str) -> JobPosting | None:
    return db.scalar(
        select(JobPosting)
        .where(
            JobPosting.id == job_id,
            JobPosting.status == JobPostingStatus.VERIFIED,
        )
        .with_for_update()
    )


def lock_user_feedback(
    db: Session,
    *,
    user_id: str,
    job_id: str,
    category: JobFeedbackCategory,
) -> JobFeedback | None:
    return db.scalar(
        select(JobFeedback)
        .where(
            JobFeedback.user_id == user_id,
            JobFeedback.job_id == job_id,
            JobFeedback.category == category,
        )
        .with_for_update()
    )


def lock_feedback(db: Session, feedback_id: str) -> JobFeedback | None:
    return db.scalar(
        select(JobFeedback)
        .where(JobFeedback.id == feedback_id)
        .with_for_update()
    )


def lock_actor_event(
    db: Session, *, actor_user_id: str, idempotency_key: str
) -> JobFeedbackEvent | None:
    return db.scalar(
        select(JobFeedbackEvent)
        .where(
            JobFeedbackEvent.actor_user_id == actor_user_id,
            JobFeedbackEvent.idempotency_key == idempotency_key,
        )
        .with_for_update()
    )


def list_user_feedback(
    db: Session, *, user_id: str, job_id: str
) -> list[JobFeedback]:
    return list(
        db.scalars(
            select(JobFeedback)
            .join(JobPosting, JobPosting.id == JobFeedback.job_id)
            .where(
                JobFeedback.user_id == user_id,
                JobFeedback.job_id == job_id,
                JobPosting.status == JobPostingStatus.VERIFIED,
            )
            .order_by(JobFeedback.category.asc())
        )
    )


def list_admin_feedback(
    db: Session,
    *,
    status: JobFeedbackStatus | None,
    category: JobFeedbackCategory | None,
    limit: int,
    offset: int,
) -> tuple[int, list[AdminFeedbackRow]]:
    filters = []
    if status is None:
        filters.append(
            JobFeedback.status.in_([JobFeedbackStatus.OPEN, JobFeedbackStatus.ACCEPTED])
        )
    else:
        filters.append(JobFeedback.status == status)
    if category is not None:
        filters.append(JobFeedback.category == category)
    total = db.scalar(
        select(func.count()).select_from(JobFeedback).where(*filters)
    ) or 0
    rows = db.execute(
        select(JobFeedback, JobPosting)
        .join(JobPosting, JobPosting.id == JobFeedback.job_id)
        .where(*filters)
        .order_by(JobFeedback.updated_at.asc(), JobFeedback.id.asc())
        .limit(limit)
        .offset(offset)
    ).all()
    return int(total), [
        AdminFeedbackRow(
            feedback=feedback,
            company_name=posting.company_name,
            title=posting.title,
            job_status=posting.status,
            job_review_version=posting.review_version,
        )
        for feedback, posting in rows
    ]


def aggregate_admin_feedback(db: Session) -> list[AdminFeedbackAggregate]:
    rows = db.execute(
        select(
            JobFeedback.job_id,
            JobPosting.company_name,
            JobPosting.title,
            JobFeedback.category,
            func.sum(case((JobFeedback.status == JobFeedbackStatus.OPEN, 1), else_=0)),
            func.sum(
                case((JobFeedback.status == JobFeedbackStatus.ACCEPTED, 1), else_=0)
            ),
            func.count(JobFeedback.id),
            func.max(JobFeedback.updated_at),
        )
        .join(JobPosting, JobPosting.id == JobFeedback.job_id)
        .where(
            JobFeedback.status.in_([JobFeedbackStatus.OPEN, JobFeedbackStatus.ACCEPTED])
        )
        .group_by(
            JobFeedback.job_id,
            JobPosting.company_name,
            JobPosting.title,
            JobFeedback.category,
        )
        .order_by(
            func.count(JobFeedback.id).desc(),
            func.max(JobFeedback.updated_at).desc(),
        )
    ).all()
    return [
        AdminFeedbackAggregate(
            job_id=job_id,
            company_name=company_name,
            title=title,
            category=category,
            open_count=int(open_count or 0),
            accepted_count=int(accepted_count or 0),
            total_count=int(total_count),
            latest_updated_at=latest_updated_at,
        )
        for (
            job_id,
            company_name,
            title,
            category,
            open_count,
            accepted_count,
            total_count,
            latest_updated_at,
        ) in rows
    ]
