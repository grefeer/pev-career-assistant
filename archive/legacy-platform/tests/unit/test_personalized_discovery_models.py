"""ORM constraint tests for personalized-discovery models (Task 2)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.db.models import (
    DiscoveredJobCandidate,
    DiscoveredJobCandidateStatus,
    JobDiscoveryTask,
    JobDiscoveryTaskStatus,
    JobSource,
    JobSourceProvider,
    PersonalizedDiscoveryRecommendation,
    PersonalizedDiscoveryRun,
    RawJobRecord,
    User,
    UserRole,
)
from backend.app.domain.personalized_discovery import (
    RecommendationPresentationState,
)

NOW = datetime(2026, 7, 25, 10, 0, 0, tzinfo=timezone.utc)


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


def _task(
    db: Session,
    source: JobSource,
    raw: RawJobRecord,
    *,
    ext: str = "ext",
    finished_at: datetime | None = NOW,
) -> JobDiscoveryTask:
    t = JobDiscoveryTask(
        source_id=source.id,
        raw_record_id=raw.id,
        external_record_id=ext,
        source_key=source.source_key,
        source_url=f"https://example.com/{ext}",
        url_hash=ext,
        payload_hash="a" * 64,
        idempotency_key=f"idem-{ext}",
        agent_version="1.0.0",
        status=JobDiscoveryTaskStatus.succeeded,
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


def _run(db: Session, user: User) -> PersonalizedDiscoveryRun:
    run = PersonalizedDiscoveryRun(
        user_id=user.id,
        preference_version=1,
        status="succeeded",
        started_at=NOW,
        finished_at=NOW,
    )
    db.add(run)
    db.flush()
    return run


def _recommendation(
    db: Session,
    *,
    user_id: str,
    canonical_job_key: str,
    candidate_id: str,
    task_id: str,
    run_id: str,
    score: float = 80.0,
    state: RecommendationPresentationState = RecommendationPresentationState.NEW,
) -> PersonalizedDiscoveryRecommendation:
    rec = PersonalizedDiscoveryRecommendation(
        user_id=user_id,
        candidate_id=candidate_id,
        task_id=task_id,
        last_run_id=run_id,
        canonical_job_key=canonical_job_key,
        preference_version=1,
        relevance_score=score,
        relevance_reason="match",
        matched_signals_json=["AI"],
        presentation_state=state,
    )
    db.add(rec)
    return rec


def test_canonical_job_key_is_unique_per_user(db_session: Session) -> None:
    user = _user(db_session)
    source = _source(db_session)
    raw = _raw(db_session, source)
    task = _task(db_session, source, raw)
    cand = _candidate(db_session, task, source, raw)
    run = _run(db_session, user)
    _recommendation(
        db_session,
        user_id=user.id,
        canonical_job_key="company:title:url",
        candidate_id=cand.id,
        task_id=task.id,
        run_id=run.id,
    )
    _recommendation(
        db_session,
        user_id=user.id,
        canonical_job_key="company:title:url",
        candidate_id=cand.id,
        task_id=task.id,
        run_id=run.id,
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_same_canonical_key_is_allowed_for_another_user(db_session: Session) -> None:
    user_a = _user(db_session, "a")
    user_b = _user(db_session, "b")
    source = _source(db_session)
    raw = _raw(db_session, source)
    task = _task(db_session, source, raw)
    cand = _candidate(db_session, task, source, raw)
    run_a = _run(db_session, user_a)
    run_b = _run(db_session, user_b)
    _recommendation(
        db_session,
        user_id=user_a.id,
        canonical_job_key="k",
        candidate_id=cand.id,
        task_id=task.id,
        run_id=run_a.id,
    )
    _recommendation(
        db_session,
        user_id=user_b.id,
        canonical_job_key="k",
        candidate_id=cand.id,
        task_id=task.id,
        run_id=run_b.id,
    )
    db_session.flush()  # no error - different users own their rows


def test_recommendation_candidate_fk_is_restrict(db_session: Session) -> None:
    user = _user(db_session)
    source = _source(db_session)
    raw = _raw(db_session, source)
    task = _task(db_session, source, raw)
    cand = _candidate(db_session, task, source, raw)
    run = _run(db_session, user)
    _recommendation(
        db_session,
        user_id=user.id,
        canonical_job_key="k",
        candidate_id=cand.id,
        task_id=task.id,
        run_id=run.id,
    )
    db_session.flush()
    # Deleting the candidate while a recommendation references it is blocked.
    db_session.delete(cand)
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_preference_columns_accept_new_fields(db_session: Session) -> None:
    from backend.app.db.models import UserPreference

    user = _user(db_session)
    pref = UserPreference(
        user_id=user.id,
        desired_roles=["AI应用开发"],
        role_synonyms=["Agent开发"],
        excluded_roles=["销售"],
        personalized_discovery_min_score=72.0,
    )
    db_session.add(pref)
    db_session.flush()
    assert pref.role_synonyms == ["Agent开发"]
    assert pref.excluded_roles == ["销售"]
    assert pref.personalized_discovery_min_score == 72.0
