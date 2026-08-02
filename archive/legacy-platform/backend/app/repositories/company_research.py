"""Data access for company-research reports.

Module-level functions only - no business logic, no HTTP.  Every function
takes an open ``Session``, reads/writes the ORM, and flushes (never commits;
the caller - the route - owns the transaction).  Mirrors the clean
``profiles``/``jobs`` repository style, not the job-discovery route that
writes SQL inline.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.db.base import utc_now
from backend.app.db.models import CompanyResearchReport
from backend.app.domain.company_research import (
    CompanyResearchBlockReason,
    CompanyResearchStatus,
)


def create_report(
    db: Session,
    *,
    user_id: str,
    company_name: str,
    source_url: str,
    source_url_hash: str,
    agent_version: str,
) -> CompanyResearchReport:
    """Insert a fresh ``queued`` report row and return it."""
    report = CompanyResearchReport(
        user_id=user_id,
        company_name=company_name,
        source_url=source_url,
        source_url_hash=source_url_hash,
        agent_version=agent_version,
        status=CompanyResearchStatus.queued,
    )
    db.add(report)
    db.flush()
    db.refresh(report)
    return report


def get_report(db: Session, report_id: str) -> CompanyResearchReport | None:
    """Return a report by id, regardless of owner (admin/internal read)."""
    return db.scalar(
        select(CompanyResearchReport).where(
            CompanyResearchReport.id == report_id
        )
    )


def get_report_for_owner(
    db: Session, report_id: str, user_id: str
) -> CompanyResearchReport | None:
    """Return a report only when it belongs to ``user_id`` (student read)."""
    return db.scalar(
        select(CompanyResearchReport).where(
            CompanyResearchReport.id == report_id,
            CompanyResearchReport.user_id == user_id,
        )
    )


def list_reports(
    db: Session,
    user_id: str,
    *,
    limit: int = 50,
    offset: int = 0,
) -> list[CompanyResearchReport]:
    """Page through a user's reports, newest first."""
    result = db.scalars(
        select(CompanyResearchReport)
        .where(CompanyResearchReport.user_id == user_id)
        .order_by(CompanyResearchReport.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(result)


def claim_for_run(db: Session, report_id: str) -> CompanyResearchReport | None:
    """Atomically transition a ``queued`` report to ``running``.

    ``with_for_update`` serializes concurrent claimers so only one runtime
    owns a report.  Returns ``None`` (and changes nothing) when the report is
    missing or already past ``queued`` - the caller treats that as "not mine".
    """
    report = db.scalar(
        select(CompanyResearchReport)
        .where(CompanyResearchReport.id == report_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if report is None or report.status != CompanyResearchStatus.queued:
        return None
    report.status = CompanyResearchStatus.running
    report.started_at = utc_now()
    report.block_reason = None
    report.last_error = None
    db.flush()
    return report


def complete_report(
    db: Session,
    report_id: str,
    *,
    status: CompanyResearchStatus,
    profile_json: dict | None = None,
    openings_json: list | None = None,
    evidence_refs_json: list | None = None,
    block_reason: CompanyResearchBlockReason | None = None,
    summary: str | None = None,
    last_error: str | None = None,
) -> CompanyResearchReport | None:
    """Write a terminal outcome onto a report.

    The caller (service) validates the transition with the domain rules; this
    function performs the row write and stamps ``finished_at``.  Returns the
    updated report or ``None`` if the report no longer exists.
    """
    report = get_report(db, report_id)
    if report is None:
        return None
    report.status = status
    report.block_reason = block_reason
    report.summary = summary
    report.last_error = last_error
    report.finished_at = utc_now()
    if profile_json is not None:
        report.profile_json = profile_json
    if openings_json is not None:
        report.openings_json = openings_json
    if evidence_refs_json is not None:
        report.evidence_refs_json = evidence_refs_json
    db.flush()
    return report


def list_reports_by_status(
    db: Session,
    status: CompanyResearchStatus,
    *,
    limit: int = 50,
) -> list[CompanyResearchReport]:
    """Return up to ``limit`` reports in a given status (worker claim feed)."""
    result = db.scalars(
        select(CompanyResearchReport)
        .where(CompanyResearchReport.status == status)
        .order_by(CompanyResearchReport.created_at.asc())
        .limit(limit)
    )
    return list(result)
