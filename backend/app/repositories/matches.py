from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, update as sql_update
from sqlalchemy.orm import Session

from backend.app.db.models import AnalysisSession, MatchReport


def create(db: Session, **kwargs: Any) -> MatchReport:
    """Create a new MatchReport row."""
    report = MatchReport(**kwargs)
    db.add(report)
    db.flush()
    return report


def get_by_id(db: Session, match_id: str, user_id: str) -> MatchReport | None:
    """Fetch a match by id, scoped to the given user."""
    return db.scalar(
        select(MatchReport).where(
            MatchReport.id == match_id,
            MatchReport.user_id == user_id,
        )
    )


def get_by_id_raw(db: Session, match_id: str) -> MatchReport | None:
    """Fetch a match by id without user scope (used by internal services)."""
    return db.scalar(select(MatchReport).where(MatchReport.id == match_id))


def list_by_user(db: Session, user_id: str) -> list[MatchReport]:
    """List all matches for a user, newest first."""
    return list(
        db.scalars(
            select(MatchReport)
            .where(MatchReport.user_id == user_id)
            .order_by(MatchReport.created_at.desc())
        ).all()
    )


def list_by_thread(
    db: Session, thread_id: str, user_id: str
) -> list[MatchReport]:
    """List matches for a given conversation thread, scoped by user owner of the session."""
    return list(
        db.scalars(
            select(MatchReport)
            .join(
                AnalysisSession,
                MatchReport.analysis_session_id == AnalysisSession.id,
            )
            .where(
                AnalysisSession.thread_id == thread_id,
                AnalysisSession.user_id == user_id,
            )
            .order_by(MatchReport.created_at.desc())
        ).all()
    )


def finalize(
    db: Session,
    match_id: str,
    status: str,
    *,
    score: int | None = None,
    score_components: dict[str, Any] | None = None,
    strengths: list[dict[str, Any]] | None = None,
    gaps: list[dict[str, Any]] | None = None,
    unknowns: list[dict[str, Any]] | None = None,
    risks: list[dict[str, Any]] | None = None,
    application_priority: str | None = None,
    recommendation: dict[str, Any] | None = None,
    error_code: str | None = None,
) -> MatchReport:
    """Finalize a match: set status, completed_at, and result fields or error_code."""
    values: dict[str, Any] = {
        "status": status,
        "completed_at": datetime.now(timezone.utc),
    }
    if error_code is not None:
        values["error_code"] = error_code
    if score is not None:
        values["score"] = score
    if score_components is not None:
        values["score_components"] = score_components
    if strengths is not None:
        values["strengths"] = strengths
    if gaps is not None:
        values["gaps"] = gaps
    if unknowns is not None:
        values["unknowns"] = unknowns
    if risks is not None:
        values["risks"] = risks
    if application_priority is not None:
        values["application_priority"] = application_priority
    if recommendation is not None:
        values["recommendation"] = recommendation

    db.execute(
        sql_update(MatchReport)
        .where(MatchReport.id == match_id)
        .values(**values)
    )
    db.flush()

    report = db.scalar(select(MatchReport).where(MatchReport.id == match_id))
    assert report is not None, f"match {match_id} not found after update"
    return report


def recover_stale(db: Session, timeout_minutes: int = 10) -> int:
    """Mark stale pending/running matches as failed. Returns count of rows updated."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=timeout_minutes)
    result = db.execute(
        sql_update(MatchReport)
        .where(
            MatchReport.status.in_(["pending", "running"]),
            MatchReport.created_at < cutoff,
        )
        .values(
            status="failed",
            error_code="stale",
            completed_at=datetime.now(timezone.utc),
        )
    )
    db.flush()
    return result.rowcount or 0
