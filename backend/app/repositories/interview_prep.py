"""Data access for interview-prep kits.

Module-level functions only - no business logic, no HTTP.  Every function
takes an open ``Session``, reads/writes the ORM, and flushes (never commits;
the caller - the route - owns the transaction).  Mirrors the clean
``company_research`` repository style.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.db.base import utc_now
from backend.app.db.models import InterviewPrepKit
from backend.app.domain.interview_prep import InterviewPrepKitStatus


def create_kit(
    db: Session,
    *,
    user_id: str,
    job_snapshot: dict,
    agent_version: str,
    target_job_id: str | None = None,
    profile_version_id: str | None = None,
    preferences_summary_json: dict | None = None,
    match_analysis_json: dict | None = None,
) -> InterviewPrepKit:
    """Insert a fresh ``generating`` kit row and return it."""
    kit = InterviewPrepKit(
        user_id=user_id,
        target_job_id=target_job_id,
        profile_version_id=profile_version_id,
        job_snapshot=job_snapshot,
        agent_version=agent_version,
        status=InterviewPrepKitStatus.generating,
        started_at=utc_now(),
        preferences_summary_json=preferences_summary_json,
        match_analysis_json=match_analysis_json,
    )
    db.add(kit)
    db.flush()
    db.refresh(kit)
    return kit


def get_kit(db: Session, kit_id: str) -> InterviewPrepKit | None:
    """Return a kit by id, regardless of owner (admin/internal read)."""
    return db.scalar(
        select(InterviewPrepKit).where(InterviewPrepKit.id == kit_id)
    )


def get_kit_for_owner(
    db: Session, kit_id: str, user_id: str
) -> InterviewPrepKit | None:
    """Return a kit only when it belongs to ``user_id`` (student read)."""
    return db.scalar(
        select(InterviewPrepKit).where(
            InterviewPrepKit.id == kit_id,
            InterviewPrepKit.user_id == user_id,
        )
    )


def list_kits(
    db: Session,
    user_id: str,
    *,
    limit: int = 50,
    offset: int = 0,
) -> list[InterviewPrepKit]:
    """Page through a user's kits, newest first."""
    result = db.scalars(
        select(InterviewPrepKit)
        .where(InterviewPrepKit.user_id == user_id)
        .order_by(InterviewPrepKit.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(result)


def complete_kit(
    db: Session,
    kit_id: str,
    *,
    status: InterviewPrepKitStatus,
    content_json: dict | None = None,
    error_code: str | None = None,
    last_error: str | None = None,
) -> InterviewPrepKit | None:
    """Write a terminal outcome onto a kit.

    The caller (service) decides the transition; this function performs the row
    write and stamps ``finished_at``.  Returns the updated kit or ``None`` if
    the row vanished mid-run.
    """
    kit = get_kit(db, kit_id)
    if kit is None:
        return None
    kit.status = status
    kit.error_code = error_code
    kit.last_error = last_error
    kit.finished_at = utc_now()
    if content_json is not None:
        kit.content_json = content_json
    db.flush()
    return kit
