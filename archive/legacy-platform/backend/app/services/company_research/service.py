"""Business logic for the Company Research skill.

Sits between the API layer (which owns HTTP and the transaction) and the
repository (SQL only) + runtime (Skill execution).  All methods take an open
``Session`` and flush (never commit); the route commits or rolls back.

The POST flow is synchronous for the MVP: a request creates a ``queued``
report, claims it, runs the deterministic browse, and writes the terminal
outcome before returning.  A background worker that claims ``queued`` rows is
the documented future enhancement - the claim/complete split already supports
it without API changes.
"""

from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy.orm import Session

from backend.app.config import Settings
from backend.app.db.models import CompanyResearchReport, User
from backend.app.domain.company_research import (
    COMPANY_NAME_MAX_LENGTH,
    SOURCE_URL_MAX_LENGTH,
    CompanyResearchBlockReason,
    CompanyResearchStatus,
    is_valid_transition,
)
from backend.app.repositories import company_research as repo
from backend.app.services.company_research.runtime import (
    CompanyResearchResult,
    CompanyResearchRuntime,
)


class CompanyResearchError(Exception):
    """Base class for company-research service errors."""


class CompanyResearchNotFoundError(CompanyResearchError):
    """Raised when a report does not exist or is not owned by the caller."""


class InvalidCompanyResearchTransition(CompanyResearchError):
    """Raised when a report is not in a state that allows running."""


class InvalidCompanyResearchInput(CompanyResearchError):
    """Raised when create input fails defense-in-depth validation."""


def _hash_url(source_url: str) -> str:
    return hashlib.sha256(source_url.encode("utf-8")).hexdigest()


def _validate_input(company_name: str, source_url: str) -> tuple[str, str]:
    name = (company_name or "").strip()
    if not name or len(name) > COMPANY_NAME_MAX_LENGTH:
        raise InvalidCompanyResearchInput("company_name")
    url = (source_url or "").strip()
    if not url or len(url) > SOURCE_URL_MAX_LENGTH:
        raise InvalidCompanyResearchInput("source_url")
    if not (url.startswith("http://") or url.startswith("https://")):
        raise InvalidCompanyResearchInput("source_url")
    return name, url


def _block_reason_from_result(result: CompanyResearchResult) -> CompanyResearchBlockReason | None:
    """Map a runtime block-reason string to the domain enum.

    The runtime only ever emits valid enum values, but a future caller could
    pass an unknown reason; fall back to ``no_evidence`` so the row is still
    reviewable rather than crashing the write.
    """
    raw = result.block_reason
    if not raw:
        return None
    try:
        return CompanyResearchBlockReason(raw)
    except ValueError:
        return CompanyResearchBlockReason.no_evidence


class CompanyResearchService:
    """Orchestrates create / run / read for company-research reports."""

    def __init__(
        self,
        settings: Settings,
        *,
        object_store: Any = None,
        runtime: CompanyResearchRuntime | None = None,
    ) -> None:
        self.settings = settings
        self.object_store = object_store
        self.runtime = runtime or CompanyResearchRuntime(
            settings, object_store=object_store
        )
        self.agent_version = settings.company_research_agent_version

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------
    def create_report(
        self,
        db: Session,
        *,
        user: User,
        company_name: str,
        source_url: str,
    ) -> CompanyResearchReport:
        """Validate input and insert a fresh ``queued`` report."""
        name, url = _validate_input(company_name, source_url)
        return repo.create_report(
            db,
            user_id=user.id,
            company_name=name,
            source_url=url,
            source_url_hash=_hash_url(url),
            agent_version=self.agent_version,
        )

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------
    def run_report(
        self,
        db: Session,
        report_id: str,
        *,
        user: User,
    ) -> CompanyResearchReport:
        """Claim and execute one report, writing its terminal outcome.

        Raises :class:`CompanyResearchNotFoundError` when the report is
        missing or not owned by ``user``, and
        :class:`InvalidCompanyResearchTransition` when it is not ``queued``
        (so a re-POST on an already-finished report is a clean 409).
        """
        report = repo.get_report_for_owner(db, report_id, user.id)
        if report is None:
            raise CompanyResearchNotFoundError(report_id)

        claimed = repo.claim_for_run(db, report_id)
        if claimed is None:
            # Not queued anymore - either running elsewhere or already
            # terminal.  Reflect the actual status in the error.
            raise InvalidCompanyResearchTransition(report.status.value)

        result = self.runtime.run(
            report_id=str(claimed.id),
            company_name=claimed.company_name,
            source_url=claimed.source_url,
        )
        return self._apply_result(db, claimed, result)

    def _apply_result(
        self,
        db: Session,
        report: CompanyResearchReport,
        result: CompanyResearchResult,
    ) -> CompanyResearchReport:
        status = CompanyResearchStatus(result.status)
        if not is_valid_transition(CompanyResearchStatus.running, status):
            # Defensive: runtime emitted an unexpected status.  Treat as
            # failed so the row is never left silently in ``running``.
            status = CompanyResearchStatus.failed

        kwargs: dict[str, Any] = {"status": status, "summary": result.summary}
        if status is CompanyResearchStatus.succeeded:
            kwargs.update(
                profile_json=result.profile,
                openings_json=result.openings,
                evidence_refs_json=result.evidence_refs,
            )
        elif status is CompanyResearchStatus.needs_manual_review:
            kwargs.update(
                block_reason=_block_reason_from_result(result),
                profile_json=result.profile,
                openings_json=result.openings,
                evidence_refs_json=result.evidence_refs,
            )
        elif status is CompanyResearchStatus.failed:
            kwargs.update(last_error=result.last_error)

        updated = repo.complete_report(db, str(report.id), **kwargs)
        # complete_report returns None only if the row vanished mid-run; fall
        # back to the in-memory report so the caller still gets an object.
        return updated if updated is not None else report

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------
    def get_report(
        self,
        db: Session,
        report_id: str,
        *,
        user: User,
    ) -> CompanyResearchReport:
        report = repo.get_report_for_owner(db, report_id, user.id)
        if report is None:
            raise CompanyResearchNotFoundError(report_id)
        return report

    def list_reports(
        self,
        db: Session,
        *,
        user: User,
        limit: int = 50,
        offset: int = 0,
    ) -> list[CompanyResearchReport]:
        return repo.list_reports(db, user.id, limit=limit, offset=offset)
