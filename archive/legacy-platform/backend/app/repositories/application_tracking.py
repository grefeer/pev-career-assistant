"""Data access for tracked job applications.

Module-level functions only - no business logic, no HTTP.  Every function
takes an open ``Session``, reads/writes the ORM, and flushes (never commits;
the caller - the route - owns the transaction).  Mirrors the clean
``interview_prep`` / ``company_research`` repository style.

State-machine validation and optimistic-lock comparison live in the service;
this module performs row writes and reads only.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.db.base import utc_now
from backend.app.db.models import ApplicationRecord, ApplicationRecordEvent
from backend.app.domain.application_tracking import ApplicationStatus

# Sentinel distinguishing "field not supplied on PATCH" from "field cleared to
# None" - needed for correct partial-update semantics.
_UNSET: Any = object()


def create_application(
    db: Session,
    *,
    user_id: str,
    company_name: str,
    title: str,
    apply_url: str | None = None,
    source: str | None = None,
    notes: str | None = None,
    target_job_id: str | None = None,
) -> ApplicationRecord:
    """Insert a fresh ``saved`` application record (state_version 0)."""
    record = ApplicationRecord(
        user_id=user_id,
        target_job_id=target_job_id,
        company_name=company_name,
        title=title,
        apply_url=apply_url,
        source=source,
        notes=notes,
        status=ApplicationStatus.saved,
        state_version=0,
    )
    db.add(record)
    db.flush()
    db.refresh(record)
    return record


def get_application(db: Session, application_id: str) -> ApplicationRecord | None:
    """Return an application by id, regardless of owner (admin/internal read)."""
    return db.scalar(
        select(ApplicationRecord).where(ApplicationRecord.id == application_id)
    )


def get_application_for_owner(
    db: Session, application_id: str, user_id: str
) -> ApplicationRecord | None:
    """Return an application only when it belongs to ``user_id``."""
    return db.scalar(
        select(ApplicationRecord).where(
            ApplicationRecord.id == application_id,
            ApplicationRecord.user_id == user_id,
        )
    )


def list_applications(
    db: Session,
    user_id: str,
    *,
    status: ApplicationStatus | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[ApplicationRecord]:
    """Page through a user's applications, newest first, optional status filter."""
    stmt = select(ApplicationRecord).where(ApplicationRecord.user_id == user_id)
    if status is not None:
        stmt = stmt.where(ApplicationRecord.status == status)
    stmt = stmt.order_by(ApplicationRecord.created_at.desc()).limit(limit).offset(offset)
    return list(db.scalars(stmt))


def count_applications(
    db: Session,
    user_id: str,
    *,
    status: ApplicationStatus | None = None,
) -> int:
    """Count a user's applications, optional status filter (for list totals)."""
    stmt = select(func.count()).select_from(ApplicationRecord).where(
        ApplicationRecord.user_id == user_id
    )
    if status is not None:
        stmt = stmt.where(ApplicationRecord.status == status)
    result = db.scalar(stmt)
    return int(result or 0)


def list_events(db: Session, application_id: str) -> list[ApplicationRecordEvent]:
    """Return the append-only transition history, oldest first."""
    return list(
        db.scalars(
            select(ApplicationRecordEvent)
            .where(ApplicationRecordEvent.application_id == application_id)
            .order_by(
                ApplicationRecordEvent.created_at.asc(),
                ApplicationRecordEvent.id.asc(),
            )
        )
    )


def apply_transition(
    db: Session,
    record: ApplicationRecord,
    *,
    to_status: ApplicationStatus,
    note: str | None = None,
) -> ApplicationRecord:
    """Advance ``record`` to ``to_status`` and append an audit event.

    The caller (service) has already validated the transition and the
    optimistic-lock version.  This function bumps ``state_version``, stamps
    ``applied_at`` the first time the record enters ``applied``, writes the
    event row, flushes, and returns the refreshed record.
    """
    from_status = record.status
    record.status = to_status
    record.state_version = record.state_version + 1
    if to_status == ApplicationStatus.applied and record.applied_at is None:
        record.applied_at = utc_now()
    db.add(
        ApplicationRecordEvent(
            application_id=record.id,
            from_status=from_status.value,
            to_status=to_status.value,
            note=note,
        )
    )
    db.flush()
    db.refresh(record)
    return record


def update_application(
    db: Session,
    record: ApplicationRecord,
    *,
    notes: str | None = _UNSET,
    apply_url: str | None = _UNSET,
) -> ApplicationRecord:
    """Patch the editable fields of an application (notes / apply_url).

    Only fields explicitly passed (not ``_UNSET``) are written, so a caller can
    clear a field by passing ``None`` without touching the other.  The status /
    state machine is never touched here - transitions go through
    :func:`apply_transition`.
    """
    if notes is not _UNSET:
        record.notes = notes
    if apply_url is not _UNSET:
        record.apply_url = apply_url
    db.flush()
    return record
