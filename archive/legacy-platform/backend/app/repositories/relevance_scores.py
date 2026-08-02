"""Repository for JobRelevanceScore (ranker output cache).

Data access only. Scores are cached keyed on ``(job_id, profile_version_id,
preferences_version)``: when the profile or preferences change version, a new
key applies and stale rows are simply ignored (or cleaned up by a janitor).
This keeps the expensive per-job MatchService off the read path unless the
caller explicitly asks for a deep match on a ranked top-N.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.db.models import JobRelevanceScore


def _profile_clause(profile_version_id: str | None):
    """NULL-safe equality on the nullable profile_version_id column."""
    if profile_version_id is None:
        return JobRelevanceScore.profile_version_id.is_(None)
    return JobRelevanceScore.profile_version_id == profile_version_id


def get(
    db: Session,
    job_id: str,
    *,
    profile_version_id: str | None,
    preferences_version: int,
) -> JobRelevanceScore | None:
    stmt = select(JobRelevanceScore).where(
        JobRelevanceScore.job_id == job_id,
        JobRelevanceScore.preferences_version == preferences_version,
        _profile_clause(profile_version_id),
    )
    return db.scalars(stmt).first()


def upsert(
    db: Session,
    *,
    user_id: str,
    job_id: str,
    profile_version_id: str | None,
    preferences_version: int,
    score: float,
    reason: str | None = None,
    matched_signals: list[str] | None = None,
) -> JobRelevanceScore:
    """Insert or refresh a cached score for the given cache key."""
    existing = get(
        db,
        job_id,
        profile_version_id=profile_version_id,
        preferences_version=preferences_version,
    )
    if existing is None:
        row = JobRelevanceScore(
            user_id=user_id,
            job_id=job_id,
            profile_version_id=profile_version_id,
            preferences_version=preferences_version,
            score=score,
            reason=reason,
            matched_signals_json=matched_signals,
        )
        db.add(row)
        db.flush()
        return row

    existing.user_id = user_id
    existing.score = score
    existing.reason = reason
    existing.matched_signals_json = matched_signals
    existing.scored_at = datetime.now(timezone.utc)
    db.flush()
    return existing


def list_top_for_user(
    db: Session,
    user_id: str,
    *,
    profile_version_id: str | None,
    preferences_version: int,
    limit: int = 50,
) -> Sequence[JobRelevanceScore]:
    """Top-N cached scores for a user, highest first."""
    stmt = (
        select(JobRelevanceScore)
        .where(
            JobRelevanceScore.user_id == user_id,
            JobRelevanceScore.preferences_version == preferences_version,
            _profile_clause(profile_version_id),
        )
        .order_by(JobRelevanceScore.score.desc())
        .limit(limit)
    )
    return db.scalars(stmt).all()


def list_unscored_job_ids(
    db: Session,
    user_id: str,
    job_ids: Sequence[str],
    *,
    profile_version_id: str | None,
    preferences_version: int,
) -> list[str]:
    """Subset of ``job_ids`` lacking a cached score for this key."""
    if not job_ids:
        return []
    stmt = select(JobRelevanceScore.job_id).where(
        JobRelevanceScore.user_id == user_id,
        JobRelevanceScore.job_id.in_(list(job_ids)),
        JobRelevanceScore.preferences_version == preferences_version,
        _profile_clause(profile_version_id),
    )
    scored = set(db.scalars(stmt).all())
    return [jid for jid in job_ids if jid not in scored]
