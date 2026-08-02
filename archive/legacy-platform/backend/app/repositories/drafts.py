from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update as sql_update
from sqlalchemy.orm import Session

from backend.app.db.models import ResumeDraft


class StaleDraftVersionError(RuntimeError):
    """Raised when an optimistic-lock update targets a stale state_version."""

    def __init__(self, draft_id: str) -> None:
        super().__init__(f"resume draft {draft_id} has a stale state version")


def create(db: Session, **kwargs: Any) -> ResumeDraft:
    """Create a new ResumeDraft row."""
    draft = ResumeDraft(**kwargs)
    db.add(draft)
    db.flush()
    return draft


def get_by_id(
    db: Session, draft_id: str, user_id: str
) -> ResumeDraft | None:
    """Fetch a draft by id, scoped to the given user."""
    return db.scalar(
        select(ResumeDraft).where(
            ResumeDraft.id == draft_id,
            ResumeDraft.user_id == user_id,
        )
    )


def list_by_user(db: Session, user_id: str) -> list[ResumeDraft]:
    """List all drafts for a user, newest first."""
    return list(
        db.scalars(
            select(ResumeDraft)
            .where(ResumeDraft.user_id == user_id)
            .order_by(ResumeDraft.created_at.desc())
        ).all()
    )


def _optimistic_transition(
    db: Session,
    draft_id: str,
    expected_version: int,
    *,
    target_status: str,
    timestamp_field: str,
) -> ResumeDraft:
    """Apply an optimistic-lock state transition and return the refreshed row."""
    now = datetime.now(timezone.utc)
    values: dict[str, Any] = {
        "status": target_status,
        "state_version": expected_version + 1,
    }
    if timestamp_field == "approved_at":
        values["approved_at"] = now
    elif timestamp_field == "rejected_at":
        values["rejected_at"] = now

    result = db.execute(
        sql_update(ResumeDraft)
        .where(
            ResumeDraft.id == draft_id,
            ResumeDraft.state_version == expected_version,
        )
        .values(**values)
    )
    if result.rowcount != 1:
        raise StaleDraftVersionError(draft_id)
    db.flush()

    draft = db.scalar(select(ResumeDraft).where(ResumeDraft.id == draft_id))
    assert draft is not None, f"draft {draft_id} vanished after optimistic update"
    return draft


def approve(
    db: Session, draft_id: str, expected_version: int
) -> ResumeDraft:
    """Approve a draft with optimistic locking on state_version."""
    return _optimistic_transition(
        db, draft_id, expected_version,
        target_status="approved",
        timestamp_field="approved_at",
    )


def reject(
    db: Session, draft_id: str, expected_version: int
) -> ResumeDraft:
    """Reject a draft with optimistic locking on state_version."""
    return _optimistic_transition(
        db, draft_id, expected_version,
        target_status="rejected",
        timestamp_field="rejected_at",
    )


def finalize(
    db: Session, draft_id: str, status: str, diffs_or_error: Any
) -> ResumeDraft:
    """Set final status and attach diffs (success) or error_code (failure)."""
    values: dict[str, Any] = {"status": status}
    if status == "failed":
        values["error_code"] = str(diffs_or_error)
    else:
        values["diffs"] = diffs_or_error

    db.execute(
        sql_update(ResumeDraft)
        .where(ResumeDraft.id == draft_id)
        .values(**values)
    )
    db.flush()

    draft = db.scalar(select(ResumeDraft).where(ResumeDraft.id == draft_id))
    assert draft is not None, f"draft {draft_id} not found after finalize"
    return draft
