"""SQL-only repository tests for personalized discovery v1 (Task 5)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from backend.app.db.models import (
    DiscoveredJobCandidate,
    DiscoveredJobCandidateStatus,
    JobDiscoveryTask,
    JobDiscoveryTaskStatus,
    JobSource,
    JobSourceProvider,
    PersonalizedDiscoveryRun,
    RawJobRecord,
    User,
    UserRole,
)
from backend.app.domain.personalized_discovery import (
    RecommendationPresentationState,
    SourceStatusReason,
)
from backend.app.repositories.personalized_discovery import (
    count_runs_for_user_in_window,
    list_latest_retained_tasks,
    list_recommendations_for_user,
    list_statuses_for_user,
    upsert_recommendation,
    upsert_source_status,
)
from backend.app.services.job_discovery.deduplication import canonical_job_key
from backend.app.services.job_discovery.schemas import NormalizedJobCandidate

NOW = datetime(2026, 7, 25, 10, 0, 0, tzinfo=timezone.utc)


# ─── Fixtures ─────────────────────────────────────────────────────────────────


def _user(db: Session, account: str = "u1") -> User:
    u = User(
        id=account,
        account=account,
        nickname=account,
        password_hash="x",
        role=UserRole.STUDENT,
    )
    db.add(u)
    db.flush()
    return u


def _source(db: Session, key: str = "src") -> JobSource:
    s = JobSource(
        id=f"src-{key}",
        source_key=key,
        provider=JobSourceProvider.USER_SUBMISSION,
        name=key,
        file_id="f",
        sheet_id="s",
        mapper_version="v1",
    )
    db.add(s)
    db.flush()
    return s


def _raw(db: Session, source: JobSource, ext: str = "ext") -> RawJobRecord:
    r = RawJobRecord(
        id=f"raw-{ext}",
        source_id=source.id,
        external_record_id=ext,
        payload_hash="a" * 64,
        raw_fields=[],
    )
    db.add(r)
    db.flush()
    return r


def _task_at(
    db: Session,
    source: JobSource,
    raw: RawJobRecord,
    *,
    ext: str,
    idem: str,
    finished_at: datetime | None = NOW,
    status: JobDiscoveryTaskStatus = JobDiscoveryTaskStatus.succeeded,
) -> JobDiscoveryTask:
    """A task with a controllable idempotency/url hash so several tasks can
    share a ``(source_id, external_record_id)`` partition."""
    t = JobDiscoveryTask(
        source_id=source.id,
        raw_record_id=raw.id,
        external_record_id=ext,
        source_key=source.source_key,
        source_url=f"https://example.com/{ext}/{idem}",
        url_hash=f"h-{idem}",
        payload_hash="a" * 64,
        idempotency_key=idem,
        agent_version="1.0.0",
        status=status,
        finished_at=finished_at,
    )
    db.add(t)
    db.flush()
    return t


def _candidate(
    db: Session,
    task: JobDiscoveryTask,
    source: JobSource,
    raw: RawJobRecord,
    *,
    cid: str = "c1",
) -> DiscoveredJobCandidate:
    c = DiscoveredJobCandidate(
        id=cid,
        task_id=task.id,
        source_id=source.id,
        raw_record_id=raw.id,
        external_record_id=task.external_record_id,
        idempotency_key=f"cand-{cid}",
        similarity_group_key=f"sim-{cid}",
        status=DiscoveredJobCandidateStatus.pending_review,
        title="AI Engineer",
    )
    db.add(c)
    db.flush()
    return c


def _run(
    db: Session,
    user: User,
    *,
    started_at: datetime = NOW,
    status: str = "succeeded",
) -> PersonalizedDiscoveryRun:
    run = PersonalizedDiscoveryRun(
        user_id=user.id,
        preference_version=1,
        status=status,
        started_at=started_at,
        finished_at=NOW,
    )
    db.add(run)
    db.flush()
    return run


def _full_jd(
    title: str,
    company: str,
    resp: str,
    req: str,
    location: str,
) -> NormalizedJobCandidate:
    return NormalizedJobCandidate(
        title=title,
        company_name=company,
        responsibilities=resp,
        requirements=req,
        locations=[location],
    )


# ─── list_latest_retained_tasks ───────────────────────────────────────────────


def test_latest_selection_skips_expired_and_superseded_tasks(db_session: Session) -> None:
    source = _source(db_session)
    raw = _raw(db_session, source)
    # Same (source, record) partition: one expired, one superseded, one newest.
    expired = _task_at(
        db_session, source, raw, ext="rec1", idem="e1",
        finished_at=NOW - timedelta(days=100),  # outside 30-day retention
    )
    older = _task_at(
        db_session, source, raw, ext="rec1", idem="e2",
        finished_at=NOW - timedelta(days=5),
    )
    newest = _task_at(
        db_session, source, raw, ext="rec1", idem="e3",
        finished_at=NOW - timedelta(days=1),
    )
    # A separate partition that is fully retained.
    other = _task_at(
        db_session, source, raw, ext="rec2", idem="e4",
        finished_at=NOW - timedelta(days=2),
    )

    result = list_latest_retained_tasks(db_session, now=NOW, retention_days=30)
    ids = {t.id for t in result}
    assert ids == {newest.id, other.id}
    assert expired.id not in ids
    assert older.id not in ids


def test_latest_selection_excludes_non_terminal_tasks(db_session: Session) -> None:
    source = _source(db_session)
    raw = _raw(db_session, source)
    # A queued task (not terminal) is never retained, even when recent.
    _task_at(
        db_session, source, raw, ext="rec1", idem="q1",
        finished_at=None, status=JobDiscoveryTaskStatus.queued,
    )
    result = list_latest_retained_tasks(db_session, now=NOW, retention_days=30)
    assert result == []


def test_latest_selection_excludes_cancelled_tasks(db_session: Session) -> None:
    source = _source(db_session)
    raw = _raw(db_session, source)
    _task_at(
        db_session, source, raw, ext="rec1", idem="c1",
        finished_at=NOW, status=JobDiscoveryTaskStatus.cancelled,
    )
    result = list_latest_retained_tasks(db_session, now=NOW, retention_days=30)
    assert result == []


# ─── upsert_recommendation ───────────────────────────────────────────────────


def test_delivery_upsert_reuses_user_canonical_key(db_session: Session) -> None:
    user = _user(db_session)
    source = _source(db_session)
    raw = _raw(db_session, source)
    task = _task_at(db_session, source, raw, ext="rec1", idem="t1")
    cand = _candidate(db_session, task, source, raw)
    run = _run(db_session, user)

    first = upsert_recommendation(
        db_session,
        user_id=user.id,
        candidate_id=cand.id,
        task_id=task.id,
        last_run_id=run.id,
        canonical_job_key="k",
        preference_version=1,
        relevance_score=80.0,
        relevance_reason="role match",
        matched_signals=["AI"],
        presentation_state=RecommendationPresentationState.VIEWED,
    )
    second = upsert_recommendation(
        db_session,
        user_id=user.id,
        candidate_id=cand.id,
        task_id=task.id,
        last_run_id=run.id,
        canonical_job_key="k",
        preference_version=2,
        relevance_score=90.0,
        relevance_reason="stronger match",
        matched_signals=["Agent"],
        presentation_state=RecommendationPresentationState.SAVED,
    )
    assert second.id == first.id
    assert second.preference_version == 2
    assert second.relevance_score == 90.0
    assert second.presentation_state == RecommendationPresentationState.SAVED


def test_cross_source_conflict_repoints_delivery_to_selected_representative(
    db_session: Session,
) -> None:
    user = _user(db_session)
    source = _source(db_session)
    raw = _raw(db_session, source)
    older_task = _task_at(db_session, source, raw, ext="rec1", idem="t1")
    older_cand = _candidate(db_session, older_task, source, raw, cid="c1")
    newer_task = _task_at(db_session, source, raw, ext="rec1", idem="t2")
    newer_cand = _candidate(db_session, newer_task, source, raw, cid="c2")
    run = _run(db_session, user)

    original = upsert_recommendation(
        db_session,
        user_id=user.id,
        candidate_id=older_cand.id,
        task_id=older_task.id,
        last_run_id=run.id,
        canonical_job_key="k",
        preference_version=1,
        relevance_score=80.0,
        relevance_reason="match",
        matched_signals=["AI"],
        presentation_state=RecommendationPresentationState.NEW,
    )
    updated = upsert_recommendation(
        db_session,
        user_id=user.id,
        candidate_id=newer_cand.id,
        task_id=newer_task.id,
        last_run_id=run.id,
        canonical_job_key="k",
        preference_version=1,
        relevance_score=80.0,
        relevance_reason="match",
        matched_signals=["AI"],
        presentation_state=RecommendationPresentationState.NEW,
    )
    assert updated.id == original.id
    assert (updated.candidate_id, updated.task_id) == (newer_cand.id, newer_task.id)


def test_upsert_preserves_dismissed_state_across_runs(db_session: Session) -> None:
    user = _user(db_session)
    source = _source(db_session)
    raw = _raw(db_session, source)
    task = _task_at(db_session, source, raw, ext="rec1", idem="t1")
    cand = _candidate(db_session, task, source, raw)
    run = _run(db_session, user)

    upsert_recommendation(
        db_session,
        user_id=user.id,
        candidate_id=cand.id,
        task_id=task.id,
        last_run_id=run.id,
        canonical_job_key="k",
        preference_version=1,
        relevance_score=80.0,
        relevance_reason="match",
        matched_signals=["AI"],
        presentation_state=RecommendationPresentationState.DISMISSED,
    )
    refreshed = upsert_recommendation(
        db_session,
        user_id=user.id,
        candidate_id=cand.id,
        task_id=task.id,
        last_run_id=run.id,
        canonical_job_key="k",
        preference_version=2,
        relevance_score=85.0,
        relevance_reason="match",
        matched_signals=["AI"],
        presentation_state=RecommendationPresentationState.NEW,
    )
    assert refreshed.presentation_state == RecommendationPresentationState.DISMISSED
    # Score still refreshed despite preserved state.
    assert refreshed.relevance_score == 85.0


def test_upsert_defaults_to_new_when_state_is_none(db_session: Session) -> None:
    user = _user(db_session)
    source = _source(db_session)
    raw = _raw(db_session, source)
    task = _task_at(db_session, source, raw, ext="rec1", idem="t1")
    cand = _candidate(db_session, task, source, raw)
    run = _run(db_session, user)

    row = upsert_recommendation(
        db_session,
        user_id=user.id,
        candidate_id=cand.id,
        task_id=task.id,
        last_run_id=run.id,
        canonical_job_key="k",
        preference_version=1,
        relevance_score=70.0,
        relevance_reason="match",
        matched_signals=None,
        presentation_state=None,
    )
    assert row.presentation_state == RecommendationPresentationState.NEW


# ─── list_recommendations_for_user ────────────────────────────────────────────


def test_owner_list_excludes_another_users_records(db_session: Session) -> None:
    user_a = _user(db_session, "a")
    user_b = _user(db_session, "b")
    source = _source(db_session)
    raw = _raw(db_session, source)
    task = _task_at(db_session, source, raw, ext="rec1", idem="t1")
    cand_a = _candidate(db_session, task, source, raw, cid="ca")
    cand_b = _candidate(db_session, task, source, raw, cid="cb")
    run_a = _run(db_session, user_a)
    run_b = _run(db_session, user_b)

    user_a_row = upsert_recommendation(
        db_session,
        user_id=user_a.id,
        candidate_id=cand_a.id,
        task_id=task.id,
        last_run_id=run_a.id,
        canonical_job_key="k-a",
        preference_version=1,
        relevance_score=80.0,
        relevance_reason="match",
        matched_signals=["AI"],
        presentation_state=RecommendationPresentationState.NEW,
    )
    upsert_recommendation(  # user_b's row must not leak into user_a's list
        db_session,
        user_id=user_b.id,
        candidate_id=cand_b.id,
        task_id=task.id,
        last_run_id=run_b.id,
        canonical_job_key="k-b",
        preference_version=1,
        relevance_score=99.0,
        relevance_reason="match",
        matched_signals=["AI"],
        presentation_state=RecommendationPresentationState.NEW,
    )
    assert list_recommendations_for_user(db_session, user_a.id, limit=50, offset=0) == [
        user_a_row
    ]


def test_list_recommendations_orders_by_score_desc(db_session: Session) -> None:
    user = _user(db_session)
    source = _source(db_session)
    raw = _raw(db_session, source)
    run = _run(db_session, user)
    # Three candidates/tasks so each recommendation has its own key.
    rows = []
    for i, score in enumerate([55.0, 90.0, 72.0]):
        task = _task_at(db_session, source, raw, ext=f"r{i}", idem=f"t{i}")
        cand = _candidate(db_session, task, source, raw, cid=f"c{i}")
        rows.append(
            upsert_recommendation(
                db_session,
                user_id=user.id,
                candidate_id=cand.id,
                task_id=task.id,
                last_run_id=run.id,
                canonical_job_key=f"k{i}",
                preference_version=1,
                relevance_score=score,
                relevance_reason="match",
                matched_signals=["AI"],
                presentation_state=RecommendationPresentationState.NEW,
            )
        )
    listed = list_recommendations_for_user(db_session, user.id, limit=50, offset=0)
    assert [r.relevance_score for r in listed] == [90.0, 72.0, 55.0]


# ─── upsert_source_status ─────────────────────────────────────────────────────


def test_upsert_source_status_is_idempotent(db_session: Session) -> None:
    user = _user(db_session)
    source = _source(db_session)
    raw = _raw(db_session, source)
    task = _task_at(db_session, source, raw, ext="rec1", idem="t1")
    run = _run(db_session, user)

    first = upsert_source_status(
        db_session,
        user_id=user.id,
        run_id=run.id,
        task_id=task.id,
        source_key=source.source_key,
        safe_source_url="https://example.com/rec1",
        reason_code=SourceStatusReason.LOGIN_REQUIRED,
    )
    second = upsert_source_status(
        db_session,
        user_id=user.id,
        run_id=run.id,
        task_id=task.id,
        source_key=source.source_key,
        safe_source_url="https://example.com/rec1",
        reason_code=SourceStatusReason.LOGIN_REQUIRED,
    )
    assert second.id == first.id
    assert list_statuses_for_user(db_session, user.id, run_id=run.id) == [first]


def test_upsert_source_status_records_distinct_reasons(db_session: Session) -> None:
    user = _user(db_session)
    source = _source(db_session)
    raw = _raw(db_session, source)
    task = _task_at(db_session, source, raw, ext="rec1", idem="t1")
    run = _run(db_session, user)

    upsert_source_status(
        db_session,
        user_id=user.id,
        run_id=run.id,
        task_id=task.id,
        source_key=source.source_key,
        safe_source_url="https://example.com/rec1",
        reason_code=SourceStatusReason.LOGIN_REQUIRED,
    )
    upsert_source_status(
        db_session,
        user_id=user.id,
        run_id=run.id,
        task_id=task.id,
        source_key=source.source_key,
        safe_source_url="https://example.com/rec1",
        reason_code=SourceStatusReason.COVERAGE_INCOMPLETE,
    )
    statuses = list_statuses_for_user(db_session, user.id, run_id=run.id)
    assert {s.reason_code for s in statuses} == {
        SourceStatusReason.LOGIN_REQUIRED,
        SourceStatusReason.COVERAGE_INCOMPLETE,
    }


# ─── count_runs_for_user_in_window ───────────────────────────────────────────


def test_daily_run_count_is_scoped_to_user_and_window(db_session: Session) -> None:
    user = _user(db_session)
    other = _user(db_session, "other")
    today_start = datetime(2026, 7, 25, 0, 0, 0, tzinfo=timezone.utc)
    tomorrow_start = today_start + timedelta(days=1)

    # Two runs for ``user`` inside the window.
    _run(db_session, user, started_at=today_start + timedelta(hours=2))
    _run(db_session, user, started_at=today_start + timedelta(hours=5))
    # A run outside the window (yesterday) - excluded.
    _run(db_session, user, started_at=today_start - timedelta(days=1))
    # A run for another user inside the window - excluded.
    _run(db_session, other, started_at=today_start + timedelta(hours=1))

    assert (
        count_runs_for_user_in_window(
            db_session,
            user_id=user.id,
            started_at=today_start,
            ended_at=tomorrow_start,
        )
        == 2
    )


# ─── canonical_job_key ────────────────────────────────────────────────────────


def test_public_canonical_key_matches_existing_identity_semantics() -> None:
    first = _full_jd("AI工程师", "某公司", "职责", "要求", "上海、深圳")
    second = _full_jd("AI工程师", "某公司", "职责", "要求", "深圳、上海")
    assert canonical_job_key(first) == canonical_job_key(second)


def test_public_canonical_key_differs_for_different_jd() -> None:
    first = _full_jd("AI工程师", "某公司", "职责A", "要求", "上海")
    second = _full_jd("AI工程师", "某公司", "职责B", "要求", "上海")
    assert canonical_job_key(first) != canonical_job_key(second)


def test_public_canonical_key_is_versioned_hex() -> None:
    first = _full_jd("AI工程师", "某公司", "职责", "要求", "上海")
    key = canonical_job_key(first)
    assert key.startswith("v1:")
    # SHA-256 hex digest (64 chars) after the prefix.
    assert len(key) == 3 + 64
    int(key[3:], 16)  # parses as hex
