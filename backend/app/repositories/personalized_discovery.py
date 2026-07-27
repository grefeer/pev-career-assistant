"""SQL-only repository for personalized job discovery v1.

Data access only - no business decisions. The service layer chooses the
representative candidate, maps walls/coverage, validates URLs, ranks, and
constructs canonical keys; this module only persists and reads rows.

Conventions:

* Every list/update predicate includes ``user_id`` (owner scoping is enforced
  here, not just in the API).
* ``upsert_recommendation`` preserves an existing ``dismissed`` presentation
  state across runs - a user who dismissed a job must not have it re-surfged
  by the next discovery run. All other fields are refreshed on conflict so
  the recommendation always reflects the latest retained task / score.
* ``upsert_source_status`` is idempotent per ``(user_id, run_id, task_id,
  reason_code)`` - re-running discovery for the same task records the wall once.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.db.models import (
    DiscoveredJobCandidate,
    JobDiscoveryTask,
    JobDiscoveryTaskStatus,
    PersonalizedDiscoveryRecommendation,
    PersonalizedDiscoveryRun,
    UserDiscoverySourceStatus,
)
from backend.app.domain.personalized_discovery import (
    RecommendationPresentationState,
    SourceStatusReason,
    source_status_copy,
)

# Terminal task statuses that carry retained evidence/candidates or a wall.
# ``queued`` / ``running`` are excluded (not yet terminal); ``cancelled`` is
# excluded because a human-aborted task carries no usable result.
_TERMINAL_TASK_STATUSES: tuple[JobDiscoveryTaskStatus, ...] = (
    JobDiscoveryTaskStatus.succeeded,
    JobDiscoveryTaskStatus.partial_success,
    JobDiscoveryTaskStatus.needs_manual_review,
    JobDiscoveryTaskStatus.failed,
)


def list_latest_retained_tasks(
    db: Session,
    *,
    now: datetime,
    retention_days: int,
) -> list[JobDiscoveryTask]:
    """Return the newest retained terminal task per ``(source_id, record_id)``.

    A task is *retained* when it is terminal and ``finished_at`` falls within
    ``[now - retention_days, now]``. For each ``(source_id,
    external_record_id)`` partition only the most-recently-finished retained
    task is returned (``row_number() == 1``); older retained tasks for the
    same source/record are superseded and dropped. Groups whose newest task is
    outside the retention window are dropped entirely (expired).
    """
    cutoff = now - timedelta(days=retention_days)
    rn = (
        func.row_number()
        .over(
            partition_by=[
                JobDiscoveryTask.source_id,
                JobDiscoveryTask.external_record_id,
            ],
            order_by=[
                JobDiscoveryTask.finished_at.desc(),
                JobDiscoveryTask.created_at.desc(),
            ],
        )
        .label("rn")
    )
    latest = (
        select(JobDiscoveryTask.id.label("tid"), rn)
        .where(
            JobDiscoveryTask.status.in_(_TERMINAL_TASK_STATUSES),
            JobDiscoveryTask.finished_at.is_not(None),
            JobDiscoveryTask.finished_at >= cutoff,
        )
        .subquery()
    )
    stmt = (
        select(JobDiscoveryTask)
        .where(JobDiscoveryTask.id.in_(select(latest.c.tid).where(latest.c.rn == 1)))
        .order_by(JobDiscoveryTask.finished_at.desc())
    )
    return list(db.scalars(stmt).all())


def upsert_recommendation(
    db: Session,
    *,
    user_id: str,
    candidate_id: str,
    task_id: str,
    last_run_id: str,
    canonical_job_key: str,
    preference_version: int,
    relevance_score: float,
    relevance_reason: str | None,
    matched_signals: list[str] | None,
    presentation_state: RecommendationPresentationState | None,
) -> PersonalizedDiscoveryRecommendation:
    """Insert or refresh the recommendation for ``(user_id, canonical_job_key)``.

    On conflict the row is repointed to the incoming candidate/task/run and
    its score refreshed - but an existing ``dismissed`` presentation state is
    preserved so a re-run cannot re-surface a job the user dismissed.
    """
    existing = db.scalars(
        select(PersonalizedDiscoveryRecommendation).where(
            PersonalizedDiscoveryRecommendation.user_id == user_id,
            PersonalizedDiscoveryRecommendation.canonical_job_key == canonical_job_key,
        )
    ).first()

    incoming_state = presentation_state or RecommendationPresentationState.NEW

    if existing is None:
        row = PersonalizedDiscoveryRecommendation(
            user_id=user_id,
            candidate_id=candidate_id,
            task_id=task_id,
            last_run_id=last_run_id,
            canonical_job_key=canonical_job_key,
            preference_version=preference_version,
            relevance_score=relevance_score,
            relevance_reason=relevance_reason,
            matched_signals_json=matched_signals,
            presentation_state=incoming_state,
        )
        db.add(row)
        db.flush()
        return row

    # Repoint to the selected representative + refresh score/version metadata.
    existing.candidate_id = candidate_id
    existing.task_id = task_id
    existing.last_run_id = last_run_id
    existing.preference_version = preference_version
    existing.relevance_score = relevance_score
    existing.relevance_reason = relevance_reason
    existing.matched_signals_json = matched_signals
    # Preserve a user's dismissal; otherwise adopt the incoming state.
    if existing.presentation_state != RecommendationPresentationState.DISMISSED:
        existing.presentation_state = incoming_state
    db.flush()
    return existing


def upsert_source_status(
    db: Session,
    *,
    user_id: str,
    run_id: str,
    task_id: str,
    source_key: str,
    safe_source_url: str,
    reason_code: SourceStatusReason,
) -> UserDiscoverySourceStatus:
    """Idempotently record a per-task source status for one run.

    Re-running discovery for the same task records the wall once. On conflict
    the existing row is returned unchanged (the reason code and display text
    are deterministic from ``reason_code``).
    """
    existing = db.scalars(
        select(UserDiscoverySourceStatus).where(
            UserDiscoverySourceStatus.user_id == user_id,
            UserDiscoverySourceStatus.run_id == run_id,
            UserDiscoverySourceStatus.task_id == task_id,
            UserDiscoverySourceStatus.reason_code == reason_code,
        )
    ).first()
    if existing is not None:
        return existing

    display_text, retry_guidance = source_status_copy(reason_code)
    row = UserDiscoverySourceStatus(
        user_id=user_id,
        run_id=run_id,
        task_id=task_id,
        source_key=source_key,
        safe_source_url=safe_source_url,
        reason_code=reason_code,
        display_text=display_text,
        retry_guidance=retry_guidance,
    )
    db.add(row)
    db.flush()
    return row


def list_recommendations_for_user(
    db: Session,
    user_id: str,
    *,
    limit: int = 50,
    offset: int = 0,
) -> Sequence[PersonalizedDiscoveryRecommendation]:
    """A user's recommendations, highest relevance first."""
    stmt = (
        select(PersonalizedDiscoveryRecommendation)
        .where(PersonalizedDiscoveryRecommendation.user_id == user_id)
        .order_by(PersonalizedDiscoveryRecommendation.relevance_score.desc())
        .limit(limit)
        .offset(offset)
    )
    return db.scalars(stmt).all()


def list_statuses_for_user(
    db: Session,
    user_id: str,
    *,
    run_id: str,
    limit: int = 50,
    offset: int = 0,
) -> Sequence[UserDiscoverySourceStatus]:
    """Per-task source statuses for one run, newest first."""
    stmt = (
        select(UserDiscoverySourceStatus)
        .where(
            UserDiscoverySourceStatus.user_id == user_id,
            UserDiscoverySourceStatus.run_id == run_id,
        )
        .order_by(UserDiscoverySourceStatus.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return db.scalars(stmt).all()


def count_runs_for_user_in_window(
    db: Session,
    *,
    user_id: str,
    started_at: datetime,
    ended_at: datetime,
) -> int:
    """Number of the user's discovery runs that started in ``[started_at, ended_at)``."""
    stmt = (
        select(func.count())
        .select_from(PersonalizedDiscoveryRun)
        .where(
            PersonalizedDiscoveryRun.user_id == user_id,
            PersonalizedDiscoveryRun.started_at >= started_at,
            PersonalizedDiscoveryRun.started_at < ended_at,
        )
    )
    return int(db.scalar(stmt) or 0)


def get_recommendation_for_user(
    db: Session,
    *,
    user_id: str,
    recommendation_id: str,
) -> PersonalizedDiscoveryRecommendation | None:
    """Return one recommendation owned by ``user_id``, or ``None``.

    The ``user_id`` predicate is the ownership gate: a missing or
    not-owned row is indistinguishable to the caller (both ``None``) so the
    API returns 404 without leaking existence.
    """
    return db.scalars(
        select(PersonalizedDiscoveryRecommendation).where(
            PersonalizedDiscoveryRecommendation.id == recommendation_id,
            PersonalizedDiscoveryRecommendation.user_id == user_id,
        )
    ).first()


def set_recommendation_state(
    db: Session,
    *,
    user_id: str,
    recommendation_id: str,
    state: RecommendationPresentationState,
) -> PersonalizedDiscoveryRecommendation | None:
    """Set the presentation state on an owned recommendation.

    Returns the updated row or ``None`` when the recommendation is missing or
    not owned by ``user_id``. ``dismissed`` is sticky from the caller's side:
    once set it is only changed by an explicit later interaction, never by a
    re-run (see :func:`upsert_recommendation`).
    """
    row = get_recommendation_for_user(
        db, user_id=user_id, recommendation_id=recommendation_id
    )
    if row is None:
        return None
    row.presentation_state = state
    db.flush()
    return row


def fetch_candidates_by_id(
    db: Session, candidate_ids: list[str]
) -> dict[str, DiscoveredJobCandidate]:
    """Load candidate rows by id (one query). Missing ids are absent from the map."""
    if not candidate_ids:
        return {}
    rows = db.scalars(
        select(DiscoveredJobCandidate).where(
            DiscoveredJobCandidate.id.in_(candidate_ids)
        )
    ).all()
    return {r.id: r for r in rows}


def fetch_tasks_by_id(
    db: Session, task_ids: list[str]
) -> dict[str, JobDiscoveryTask]:
    """Load task rows by id (one query); used for source-host URL re-validation."""
    if not task_ids:
        return {}
    rows = db.scalars(
        select(JobDiscoveryTask).where(JobDiscoveryTask.id.in_(task_ids))
    ).all()
    return {r.id: r for r in rows}
