"""Personalized discovery v1 service pipeline tests (Task 6)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.db.models import (
    DiscoveredJobCandidate,
    DiscoveredJobCandidateStatus,
    DiscoveryBlockReason,
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
from backend.app.repositories.personalized_discovery import (
    list_recommendations_for_user,
    list_statuses_for_user,
)
from backend.app.services.job_discovery.schemas import NormalizedJobCandidate
from backend.app.services.personalized_discovery import (
    PersonalizedDiscoveryRateLimitError,
    PersonalizedDiscoveryService,
)
from backend.app.services.relevance.relevance_ranker import RankedCandidate

NOW = datetime(2026, 7, 25, 10, 0, 0, tzinfo=timezone.utc)


# ─── Fake rankers ────────────────────────────────────────────────────────────


class _FakeRanker:
    """Returns a fixed positive score for every candidate and records titles."""

    def __init__(self, score: float = 85.0) -> None:
        self.score = score
        self.received_titles: list[str | None] = []

    def rank(self, candidates, *, profile_summary, preferences):  # noqa: ARG002
        results: list[RankedCandidate] = []
        for i, c in enumerate(candidates):
            title = getattr(c, "title", None)
            self.received_titles.append(title)
            results.append(
                RankedCandidate(
                    index=i,
                    title=title,
                    score=self.score,
                    reason="role match",
                    matched_signals=["AI应用开发"],
                )
            )
        return results


class _FailingRanker:
    """Simulates an LLM failure: every candidate scores 0.0 (never delivered)."""

    def rank(self, candidates, *, profile_summary, preferences):  # noqa: ARG002
        return [
            RankedCandidate(index=i, title=getattr(c, "title", None), score=0.0)
            for i, c in enumerate(candidates)
        ]


def _service(ranker) -> PersonalizedDiscoveryService:
    return PersonalizedDiscoveryService(ranker)


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


def _source(db: Session, key: str, host: str = "app.mokahr.com") -> JobSource:
    s = JobSource(
        id=f"src-{key}",
        source_key=key,
        provider=JobSourceProvider.USER_SUBMISSION,
        name=key,
        file_id=f"file-{key}",  # distinct per source (unique constraint)
        sheet_id=f"sheet-{key}",
        mapper_version="v1",
    )
    db.add(s)
    db.flush()
    # Stash host for source_url construction in _task.
    s._host = host  # type: ignore[attr-defined]
    return s


def _raw(db: Session, source: JobSource, ext: str) -> RawJobRecord:
    r = RawJobRecord(
        id=f"raw-{source.source_key}-{ext}",
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
    ext: str,
    idem: str,
    finished_at: datetime = NOW,
    status: JobDiscoveryTaskStatus = JobDiscoveryTaskStatus.succeeded,
    block_reason: DiscoveryBlockReason | None = None,
    coverage_verified: bool = False,
    single_source_complete: dict | None = None,
) -> JobDiscoveryTask:
    host = getattr(source, "_host", "app.mokahr.com")  # type: ignore[attr-defined]
    summary: dict = {"coverage_verified": coverage_verified}
    if single_source_complete is not None:
        summary["single_source_complete"] = single_source_complete
    t = JobDiscoveryTask(
        source_id=source.id,
        raw_record_id=raw.id,
        external_record_id=ext,
        source_key=source.source_key,
        source_url=f"https://{host}/{ext}",
        url_hash=f"h-{idem}",
        payload_hash="a" * 64,
        idempotency_key=idem,
        agent_version="1.0.0",
        status=status,
        block_reason=block_reason,
        finished_at=finished_at,
        result_summary_json=summary,
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
    cid: str,
    title: str = "AI Agent 应用开发工程师",
    company: str = "某公司",
    responsibilities: str = "负责 AI Agent 应用架构设计与落地",
    requirements: str = "Python；LangChain",
    location: str = "上海",
    apply_url: str = "https://app.mokahr.com/apply/abc",
    evidence_refs: list[dict] | None = None,
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
        title=title,
        company_name=company,
        responsibilities=responsibilities,
        requirements=requirements,
        locations_json=[location] if location else None,
        apply_url=apply_url,
        evidence_refs_json=evidence_refs if evidence_refs is not None else [
            {"url": "https://app.mokahr.com/jobs/1", "content_hash": "h1", "evidence_type": "page_text"}
        ],
    )
    db.add(c)
    db.flush()
    return c


def _prefs(db: Session, user: User, *, desired_roles, min_score=None, excluded=None, synonyms=None):
    from backend.app.db.models import UserPreference

    pref = UserPreference(
        user_id=user.id,
        desired_roles=desired_roles,
        role_synonyms=synonyms or [],
        excluded_roles=excluded or [],
        personalized_discovery_min_score=min_score,
    )
    db.add(pref)
    db.flush()
    return pref


# ─── Read helpers ────────────────────────────────────────────────────────────


def _recommendations(db: Session, user_id: str) -> list[PersonalizedDiscoveryRecommendation]:
    return list(list_recommendations_for_user(db, user_id, limit=500, offset=0))


def _titles(db: Session, user_id: str) -> list[str | None]:
    rows = _recommendations(db, user_id)
    out: list[str | None] = []
    for r in rows:
        c = db.get(DiscoveredJobCandidate, r.candidate_id)
        out.append(c.title if c else None)
    return out


def _status_codes(db: Session, user_id: str, run_id: str) -> list[str]:
    rows = list_statuses_for_user(db, user_id, run_id=run_id, limit=500, offset=0)
    return sorted(r.reason_code.value for r in rows)


def _run_succeeded(db: Session, run_id: str) -> bool:
    r = db.get(PersonalizedDiscoveryRun, run_id)
    return r is not None and r.status == "succeeded"


# ─── Tests ───────────────────────────────────────────────────────────────────


def test_only_complete_evidenced_safe_candidate_is_delivered(db_session: Session) -> None:
    user = _user(db_session)
    _prefs(db_session, user, desired_roles=["AI应用开发", "agent开发"])
    source = _source(db_session, "dji")
    raw = _raw(db_session, source, "rec1")
    task = _task(
        db_session, source, raw, ext="rec1", idem="t1", coverage_verified=True
    )
    _candidate(
        db_session, task, source, raw, cid="c1",
        title="AI应用开发工程师",
        apply_url="https://app.mokahr.com/apply/abc",
    )

    run = _service(_FakeRanker()).run(db_session, user_id=user.id, now=NOW)

    assert _run_succeeded(db_session, run.id)
    assert _titles(db_session, user.id) == ["AI应用开发工程师"]


def test_wall_and_incomplete_task_become_status_not_recommendation(
    db_session: Session,
) -> None:
    user = _user(db_session)
    _prefs(db_session, user, desired_roles=["AI应用开发"])
    source = _source(db_session, "src")
    # Task 1: a login wall (terminal needs_manual_review + block reason).
    raw1 = _raw(db_session, source, "r1")
    _task(
        db_session, source, raw1, ext="r1", idem="t1",
        status=JobDiscoveryTaskStatus.needs_manual_review,
        block_reason=DiscoveryBlockReason.login_required,
    )
    # Task 2: succeeded but no coverage proof -> coverage_incomplete.
    raw2 = _raw(db_session, source, "r2")
    _task(
        db_session, source, raw2, ext="r2", idem="t2",
        coverage_verified=False,
    )

    run = _service(_FakeRanker()).run(db_session, user_id=user.id, now=NOW)

    assert _run_succeeded(db_session, run.id)
    assert _titles(db_session, user.id) == []
    assert _status_codes(db_session, user.id, run.id) == [
        "coverage_incomplete", "login_required",
    ]


def test_ranker_failure_missing_jd_and_unsafe_url_never_deliver(
    db_session: Session,
) -> None:
    user = _user(db_session)
    _prefs(db_session, user, desired_roles=["AI应用开发"])
    source = _source(db_session, "src")
    raw = _raw(db_session, source, "r1")
    task = _task(
        db_session, source, raw, ext="r1", idem="t1", coverage_verified=True
    )
    # (a) missing JD body -> filtered before ranking.
    _candidate(
        db_session, task, source, raw, cid="nojd",
        title="AI应用开发工程师", responsibilities="", requirements="",
    )
    # (b) JD body but unsafe apply URL -> filtered (URL unsafe).
    _candidate(
        db_session, task, source, raw, cid="badurl",
        title="AI应用开发工程师",
        apply_url="javascript:alert(1)",
    )
    # (c) JD body + safe URL, but the ranker fails -> score 0 -> not delivered.
    _candidate(
        db_session, task, source, raw, cid="good",
        title="AI应用开发工程师",
    )

    run = _service(_FailingRanker()).run(db_session, user_id=user.id, now=NOW)

    assert _run_succeeded(db_session, run.id)
    assert _titles(db_session, user.id) == []


def test_run_has_no_implicit_top_twenty_cap(db_session: Session) -> None:
    user = _user(db_session)
    _prefs(db_session, user, desired_roles=["AI应用开发"])
    source = _source(db_session, "src")
    raw = _raw(db_session, source, "r1")
    task = _task(
        db_session, source, raw, ext="r1", idem="t1", coverage_verified=True
    )
    # 21 distinct canonical keys (distinct responsibilities) -> 21 representatives.
    for i in range(21):
        _candidate(
            db_session, task, source, raw, cid=f"c{i}",
            title=f"AI应用开发工程师{i}",
            responsibilities=f"职责{i}",  # distinct core_hash -> distinct key
        )

    run = _service(_FakeRanker(score=85.0)).run(db_session, user_id=user.id, now=NOW)

    assert _run_succeeded(db_session, run.id)
    assert len(_recommendations(db_session, user.id)) == 21


def test_cross_source_duplicates_rank_once_and_select_newest_task_candidate(
    db_session: Session,
) -> None:
    user = _user(db_session)
    _prefs(db_session, user, desired_roles=["AI应用开发"])
    # Two different sources; candidates share the same canonical key (same
    # company + JD + location), so they group into ONE representative.
    src_a = _source(db_session, "a", host="app.mokahr.com")
    src_b = _source(db_session, "b", host="jobs.feishu.cn")
    raw_a = _raw(db_session, src_a, "ra")
    raw_b = _raw(db_session, src_b, "rb")
    older_task = _task(
        db_session, src_a, raw_a, ext="ra", idem="ta",
        finished_at=NOW - timedelta(days=5), coverage_verified=True,
    )
    newer_task = _task(
        db_session, src_b, raw_b, ext="rb", idem="tb",
        finished_at=NOW - timedelta(days=1), coverage_verified=True,
    )
    _candidate(
        db_session, older_task, src_a, raw_a, cid="ca",
        title="AI应用开发工程师", company="某公司",
        responsibilities="职责", requirements="要求", location="上海",
        apply_url="https://app.mokahr.com/apply/ca",
    )
    newer_cand = _candidate(
        db_session, newer_task, src_b, raw_b, cid="cb",
        title="AI应用开发工程师", company="某公司",
        responsibilities="职责", requirements="要求", location="上海",
        apply_url="https://jobs.feishu.cn/apply/cb",
    )

    ranker = _FakeRanker()
    run = _service(ranker).run(db_session, user_id=user.id, now=NOW)

    assert _run_succeeded(db_session, run.id)
    # The duplicate was collapsed to one representative -> ranked once.
    assert ranker.received_titles.count("AI应用开发工程师") == 1
    rows = _recommendations(db_session, user.id)
    assert len(rows) == 1
    assert (rows[0].candidate_id, rows[0].task_id) == (newer_cand.id, newer_task.id)


def test_excluded_role_wins_over_recall(db_session: Session) -> None:
    user = _user(db_session)
    _prefs(
        db_session, user,
        desired_roles=["AI应用开发"],
        excluded=["销售"],  # a title containing 销售 is dropped even if it also matches
    )
    source = _source(db_session, "src")
    raw = _raw(db_session, source, "r1")
    task = _task(
        db_session, source, raw, ext="r1", idem="t1", coverage_verified=True
    )
    _candidate(
        db_session, task, source, raw, cid="c1",
        title="AI应用开发销售工程师",  # matches desired AND excluded
    )

    run = _service(_FakeRanker()).run(db_session, user_id=user.id, now=NOW)

    assert _run_succeeded(db_session, run.id)
    assert _titles(db_session, user.id) == []


def test_score_below_threshold_not_delivered(db_session: Session) -> None:
    user = _user(db_session)
    _prefs(db_session, user, desired_roles=["AI应用开发"], min_score=90.0)
    source = _source(db_session, "src")
    raw = _raw(db_session, source, "r1")
    task = _task(
        db_session, source, raw, ext="r1", idem="t1", coverage_verified=True
    )
    _candidate(db_session, task, source, raw, cid="c1", title="AI应用开发工程师")

    # Score 80 < threshold 90 -> not delivered.
    run = _service(_FakeRanker(score=80.0)).run(db_session, user_id=user.id, now=NOW)

    assert _run_succeeded(db_session, run.id)
    assert _titles(db_session, user.id) == []


def test_single_source_proof_admits_task(db_session: Session) -> None:
    user = _user(db_session)
    _prefs(db_session, user, desired_roles=["AI应用开发"])
    source = _source(db_session, "dji", host="app.mokahr.com")
    raw = _raw(db_session, source, "r1")
    # No coverage_verified, but a registered single-source proof with hosts.
    proof = {
        "contract_id": "dji-mokahr",
        "evidence_hash": "ev1",
        "terminal_signal": "job_list_complete",
        "application_hosts": ["app.mokahr.com"],
    }
    task = _task(
        db_session, source, raw, ext="r1", idem="t1",
        coverage_verified=False, single_source_complete=proof,
    )
    _candidate(db_session, task, source, raw, cid="c1", title="AI应用开发工程师")

    run = _service(_FakeRanker()).run(db_session, user_id=user.id, now=NOW)

    assert _run_succeeded(db_session, run.id)
    assert _titles(db_session, user.id) == ["AI应用开发工程师"]


def test_daily_run_limit_rejects(db_session: Session) -> None:
    user = _user(db_session)
    _prefs(db_session, user, desired_roles=["AI应用开发"])
    # Pre-create 5 succeeded runs in today's China-day window.
    for i in range(5):
        db_session.add(
            PersonalizedDiscoveryRun(
                user_id=user.id,
                preference_version=1,
                status="succeeded",
                started_at=NOW - timedelta(hours=i),
                finished_at=NOW,
            )
        )
    db_session.flush()

    with pytest.raises(PersonalizedDiscoveryRateLimitError):
        _service(_FakeRanker()).run(db_session, user_id=user.id, now=NOW)


def test_run_records_failed_status_on_internal_error(db_session: Session) -> None:
    user = _user(db_session)
    _prefs(db_session, user, desired_roles=["AI应用开发"])
    source = _source(db_session, "src")
    raw = _raw(db_session, source, "r1")
    task = _task(
        db_session, source, raw, ext="r1", idem="t1", coverage_verified=True
    )
    _candidate(db_session, task, source, raw, cid="c1", title="AI应用开发工程师")

    class _ExplodingRanker:
        def rank(self, candidates, *, profile_summary, preferences):  # noqa: ARG002
            raise RuntimeError("boom")

    with pytest.raises(Exception):  # typed PersonalizedDiscoveryError
        _service(_ExplodingRanker()).run(db_session, user_id=user.id, now=NOW)

    runs = db_session.scalars(
        select(PersonalizedDiscoveryRun).where(PersonalizedDiscoveryRun.user_id == user.id)
    ).all()
    assert len(runs) == 1
    assert runs[0].status == "failed"
    assert runs[0].summary_json == {"error": "internal_error"}


# ─── Sanity: the service is wired to the real deduper identity ────────────────


def test_canonical_key_collapses_multi_city_variant() -> None:
    """A role posted in 上海、深圳 vs 深圳、上海 is ONE canonical job
    (same as within-run dedupe), so one recommendation is upserted."""
    from backend.app.services.job_discovery.deduplication import canonical_job_key

    a = NormalizedJobCandidate(
        title="AI工程师", company_name="某公司",
        responsibilities="职责", requirements="要求", locations=["上海、深圳"],
    )
    b = NormalizedJobCandidate(
        title="AI工程师", company_name="某公司",
        responsibilities="职责", requirements="要求", locations=["深圳、上海"],
    )
    assert canonical_job_key(a) == canonical_job_key(b)
