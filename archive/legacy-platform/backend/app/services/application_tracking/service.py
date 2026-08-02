"""Business logic for the application-tracking skill.

A non-agent, user-scoped skill: the user records the jobs they have applied to
(or plan to) and advances each record through the state machine.  All methods
take an open ``Session`` and flush (never commit) - the route owns the
transaction.  There is no LLM, no crawl, and **no auto-submit** (security gate
#1): every status advance is an explicit human action validated here and
recorded as an append-only event by the repository.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from backend.app.config import Settings
from backend.app.db.models import ApplicationRecord, ApplicationRecordEvent, User
from backend.app.domain.application_tracking import (
    ApplicationStatus,
    is_terminal,
    is_valid_transition,
)
from backend.app.repositories import application_tracking as repo

logger = logging.getLogger(__name__)


class ApplicationTrackingError(Exception):
    """Base class for application-tracking service errors."""


class ApplicationNotFoundError(ApplicationTrackingError):
    """Raised when an application does not exist or is not owned by the caller."""


class ApplicationInputError(ApplicationTrackingError):
    """Raised when a create / transition / update input is unusable.

    Carries a stable ``code`` (``invalid_transition`` / ``already_terminal`` /
    ``stale_version`` / ``no_fields``) so the route can map it to a meaningful
    HTTP status.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class ApplicationTrackingService:
    """Owns the application-tracking state machine and field validation."""

    def __init__(self, settings: Settings):
        self._settings = settings

    # ------------------------------------------------------------------ create
    def create_application(
        self,
        db: Session,
        *,
        user: User,
        company_name: str,
        title: str,
        apply_url: str | None = None,
        source: str | None = None,
        notes: str | None = None,
        target_job_id: str | None = None,
    ) -> ApplicationRecord:
        """Create a fresh ``saved`` application record for ``user``."""
        return repo.create_application(
            db,
            user_id=user.id,
            company_name=company_name,
            title=title,
            apply_url=apply_url,
            source=source,
            notes=notes,
            target_job_id=target_job_id,
        )

    # ------------------------------------------------------------ transitions
    def transition(
        self,
        db: Session,
        *,
        user: User,
        application_id: str,
        to_status: ApplicationStatus,
        note: str | None = None,
        expected_version: int | None = None,
    ) -> ApplicationRecord:
        """Advance an application to ``to_status`` after full validation."""
        record = repo.get_application_for_owner(db, application_id, user.id)
        if record is None:
            raise ApplicationNotFoundError(application_id)

        if is_terminal(record.status):
            raise ApplicationInputError(
                "already_terminal",
                f"application {application_id} is already in a terminal status",
            )
        if not is_valid_transition(record.status, to_status):
            raise ApplicationInputError(
                "invalid_transition",
                f"cannot transition from {record.status.value} to {to_status.value}",
            )
        if expected_version is not None and expected_version != record.state_version:
            raise ApplicationInputError(
                "stale_version",
                f"expected version {expected_version} but record is at "
                f"{record.state_version}",
            )

        return repo.apply_transition(db, record, to_status=to_status, note=note)

    # ------------------------------------------------------------------ reads
    def get_application(
        self, db: Session, *, user: User, application_id: str
    ) -> ApplicationRecord:
        """Return one application, owner-scoped (raises if missing)."""
        record = repo.get_application_for_owner(db, application_id, user.id)
        if record is None:
            raise ApplicationNotFoundError(application_id)
        return record

    def list_applications(
        self,
        db: Session,
        *,
        user: User,
        status: ApplicationStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[ApplicationRecord], int]:
        """Page through a user's applications with an optional status filter."""
        items = repo.list_applications(
            db, user.id, status=status, limit=limit, offset=offset
        )
        total = repo.count_applications(db, user.id, status=status)
        return items, total

    def list_events(
        self, db: Session, *, user: User, application_id: str
    ) -> list[ApplicationRecordEvent]:
        """Return the transition history, owner-scoped."""
        # Owner-check first so we never leak another user's event log.
        record = repo.get_application_for_owner(db, application_id, user.id)
        if record is None:
            raise ApplicationNotFoundError(application_id)
        return repo.list_events(db, application_id)

    # ----------------------------------------------------------------- update
    def update_application(
        self,
        db: Session,
        *,
        user: User,
        application_id: str,
        **fields: object,
    ) -> ApplicationRecord:
        """Patch editable fields (notes / apply_url) on an application.

        ``fields`` carries only the keys the caller explicitly set (PATCH
        semantics); an empty payload raises ``no_fields``.
        """
        if not fields:
            raise ApplicationInputError("no_fields", "no editable fields supplied")

        record = repo.get_application_for_owner(db, application_id, user.id)
        if record is None:
            raise ApplicationNotFoundError(application_id)

        return repo.update_application(db, record, **fields)  # type: ignore[arg-type]
