from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import threading

import pytest
from sqlalchemy import create_engine, event, func, select, update
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Session

from backend.app.db.base import Base, utc_now
from backend.app.db.models import (
    JobPosting,
    JobPostingStatus,
    JobSyncRun,
    JobSyncRunStatus,
    RawJobRecord,
)
from backend.app.repositories import jobs
from backend.app.services.job_mappers import (
    BUILTIN_SOURCES,
    BuiltinJobSource,
    NormalizedJobCandidate,
)


@pytest.fixture
def db() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def seeded_source(db: Session):
    jobs.ensure_builtin_sources(db, BUILTIN_SOURCES)
    db.commit()
    source = jobs.get_source(db, "tencent-intern-referrals")
    assert source is not None
    return source


def candidate(
    *,
    company_name: str = "示例公司",
    title: str = "工程师",
    recruitment_types: list[str] | None = None,
) -> NormalizedJobCandidate:
    return NormalizedJobCandidate(
        company_name=company_name,
        title=title,
        locations=["北京"],
        recruitment_types=recruitment_types or ["暑期实习"],
        industries=["互联网"],
        apply_url="https://example.com/jobs",
        referral_code=None,
        deadline_text=None,
        source_updated_at=None,
    )


def snapshot(
    db: Session,
    *,
    source_id: str,
    external_record_id: str,
    payload_hash: str,
) -> RawJobRecord:
    record, created = jobs.insert_raw_snapshot(
        db,
        source_id=source_id,
        external_record_id=external_record_id,
        raw_fields=[{"field": "公司名称", "text_value": {"items": []}}],
        payload_hash=payload_hash,
        source_updated_at=None,
        observed_at=utc_now(),
    )
    assert created
    return record


def test_builtin_source_initialization_is_idempotent(db: Session) -> None:
    jobs.ensure_builtin_sources(db, BUILTIN_SOURCES)
    jobs.ensure_builtin_sources(db, BUILTIN_SOURCES)
    db.commit()
    assert len(jobs.list_sources(db)) == 2


def test_concurrent_first_use_initialization_is_atomic_on_sqlite(tmp_path) -> None:
    database_path = (tmp_path / "concurrent-sources.db").as_posix()
    engine = create_engine(
        f"sqlite+pysqlite:///{database_path}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    Base.metadata.create_all(engine)
    barrier = threading.Barrier(2)
    blocked_threads = threading.local()

    def synchronize_first_lookup(
        _connection, _cursor, statement, _parameters, _context, _executemany
    ) -> None:
        if statement.lstrip().upper().startswith("INSERT"):
            blocked_threads.insert_seen = True
        if (
            statement.lstrip().upper().startswith("SELECT")
            and "FROM job_sources" in statement
            and not getattr(blocked_threads, "insert_seen", False)
            and not getattr(blocked_threads, "done", False)
        ):
            blocked_threads.done = True
            barrier.wait(timeout=5)

    event.listen(engine, "before_cursor_execute", synchronize_first_lookup)

    def initialize() -> None:
        with Session(engine) as session:
            jobs.ensure_builtin_sources(session, BUILTIN_SOURCES)
            session.commit()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(initialize) for _ in range(2)]
        for future in futures:
            future.result(timeout=15)

    event.remove(engine, "before_cursor_execute", synchronize_first_lookup)
    with Session(engine) as session:
        assert len(jobs.list_sources(session)) == 2


def test_mysql_source_initialization_compiles_as_atomic_upsert() -> None:
    statement = jobs._builtin_source_upsert_statement(
        BUILTIN_SOURCES[0], dialect_name="mysql"
    )
    sql = str(statement.compile(dialect=mysql.dialect())).lower()
    update_clause = sql.split("on duplicate key update", maxsplit=1)[1]

    assert "insert into job_sources" in sql
    assert "on duplicate key update" in sql
    assert {"name", "file_id", "sheet_id", "mapper_version"} <= {
        assignment.split("=", maxsplit=1)[0].strip().split(".")[-1]
        for assignment in update_clause.split(",")
    }
    assert "enabled" not in update_clause


def test_builtin_source_refresh_preserves_disabled_state(db: Session) -> None:
    jobs.ensure_builtin_sources(db, BUILTIN_SOURCES)
    db.commit()
    source = jobs.get_source(db, "tencent-intern-referrals")
    assert source is not None
    source.enabled = False
    db.commit()
    changed = BuiltinJobSource(
        source_key=source.source_key,
        name="更新后的来源名",
        file_id="updated-file",
        sheet_id="updated-sheet",
        mapper_version="v2",
    )

    jobs.ensure_builtin_sources(db, [changed])
    db.commit()
    db.refresh(source)

    assert source.enabled is False
    assert (source.name, source.file_id, source.sheet_id, source.mapper_version) == (
        changed.name,
        changed.file_id,
        changed.sheet_id,
        changed.mapper_version,
    )


def test_active_lease_rejects_a_second_run(db: Session) -> None:
    jobs.ensure_builtin_sources(db, BUILTIN_SOURCES)
    db.commit()
    source = jobs.get_source(db, "tencent-intern-referrals")
    assert source is not None
    now = utc_now()
    jobs.acquire_sync_run(db, source.id, now=now)
    db.commit()
    with pytest.raises(jobs.SyncConflictError):
        jobs.acquire_sync_run(db, source.id, now=now + timedelta(seconds=1))


def test_expired_run_takeover_marks_old_run_failed(db: Session) -> None:
    source = seeded_source(db)
    now = utc_now()
    old_run = jobs.acquire_sync_run(db, source.id, now=now)
    db.commit()

    new_run = jobs.acquire_sync_run(
        db, source.id, now=now + jobs.LEASE_DURATION + timedelta(seconds=1)
    )
    db.flush()

    db.refresh(old_run)
    db.refresh(source)
    assert old_run.status is JobSyncRunStatus.FAILED
    assert old_run.error_code == "sync_lease_expired"
    assert old_run.finished_at == (
        now + jobs.LEASE_DURATION + timedelta(seconds=1)
    ).replace(tzinfo=None)
    assert source.active_sync_run_id == new_run.id


def test_lease_refresh_requires_matching_run_id(db: Session) -> None:
    source = seeded_source(db)
    now = utc_now()
    run = jobs.acquire_sync_run(db, source.id, now=now)

    with pytest.raises(jobs.StaleSyncLeaseError):
        jobs.refresh_sync_lease(db, source.id, "not-the-owner", now=now)

    refreshed_at = now + timedelta(minutes=1)
    jobs.refresh_sync_lease(db, source.id, run.id, now=refreshed_at)
    assert source.sync_lease_expires_at == refreshed_at + jobs.LEASE_DURATION


def test_finishing_success_clears_lease_and_records_success_time(db: Session) -> None:
    source = seeded_source(db)
    started_at = utc_now()
    run = jobs.acquire_sync_run(db, source.id, now=started_at)
    finished_at = started_at + timedelta(seconds=5)

    finished = jobs.finish_sync_run(
        db,
        source.id,
        run.id,
        status=JobSyncRunStatus.SUCCEEDED,
        now=finished_at,
        error_code=None,
    )

    assert finished.status is JobSyncRunStatus.SUCCEEDED
    assert finished.finished_at == finished_at
    assert source.active_sync_run_id is None
    assert source.sync_lease_expires_at is None
    assert source.last_successful_sync_at == finished_at


def test_same_payload_is_one_snapshot(db: Session) -> None:
    source = seeded_source(db)
    first, created_first = jobs.insert_raw_snapshot(
        db,
        source_id=source.id,
        external_record_id="r1",
        raw_fields=[{"field": "公司名称", "text_value": {"items": []}}],
        payload_hash="a" * 64,
        source_updated_at=None,
        observed_at=utc_now(),
    )
    second, created_second = jobs.insert_raw_snapshot(
        db,
        source_id=source.id,
        external_record_id="r1",
        raw_fields=[{"field": "公司名称", "text_value": {"items": []}}],
        payload_hash="a" * 64,
        source_updated_at=None,
        observed_at=utc_now(),
    )
    assert first.id == second.id
    assert (created_first, created_second) == (True, False)


def test_posting_upsert_points_to_new_snapshot_without_deleting_history(
    db: Session,
) -> None:
    source = seeded_source(db)
    first_raw = snapshot(
        db,
        source_id=source.id,
        external_record_id="r1",
        payload_hash="a" * 64,
    )
    posting, first_action = jobs.upsert_posting(
        db, source=source, raw_record=first_raw, candidate=candidate()
    )
    second_raw = snapshot(
        db,
        source_id=source.id,
        external_record_id="r1",
        payload_hash="b" * 64,
    )
    updated, second_action = jobs.upsert_posting(
        db,
        source=source,
        raw_record=second_raw,
        candidate=candidate(title="高级工程师"),
    )

    assert (first_action, second_action) == ("created", "updated")
    assert updated.id == posting.id
    assert updated.raw_record_id == second_raw.id
    assert updated.review_version == 0
    assert db.get(RawJobRecord, first_raw.id) is first_raw
    assert db.scalar(select(func.count()).select_from(RawJobRecord)) == 2


def test_sync_does_not_overwrite_reviewed_canonical_fields(db: Session) -> None:
    source = seeded_source(db)
    first_raw = snapshot(
        db, source_id=source.id, external_record_id="r1", payload_hash="a" * 64
    )
    posting, _ = jobs.upsert_posting(
        db, source=source, raw_record=first_raw, candidate=candidate(title="来源岗位")
    )
    posting.status = JobPostingStatus.PENDING_REVIEW
    posting.title = "人工确认岗位"
    posting.description_text = "人工补全的完整 JD"
    posting.review_version = 1
    db.flush()

    changed_raw = snapshot(
        db, source_id=source.id, external_record_id="r1", payload_hash="b" * 64
    )
    updated, action = jobs.upsert_posting(
        db,
        source=source,
        raw_record=changed_raw,
        candidate=candidate(title="来源新岗位"),
    )

    assert action == "updated"
    assert updated.title == "人工确认岗位"
    assert updated.description_text == "人工补全的完整 JD"
    assert updated.source_candidate["title"] == "来源新岗位"
    assert updated.source_changed_since_review is True


def test_protected_posting_ignores_equivalent_candidate_for_review_version(
    db: Session,
) -> None:
    source = seeded_source(db)
    first_raw = snapshot(
        db, source_id=source.id, external_record_id="r1", payload_hash="a" * 64
    )
    posting, _ = jobs.upsert_posting(
        db, source=source, raw_record=first_raw, candidate=candidate(title="来源岗位")
    )
    posting.status = JobPostingStatus.PENDING_REVIEW
    posting.review_version = 1
    db.flush()
    changed_raw = snapshot(
        db, source_id=source.id, external_record_id="r1", payload_hash="b" * 64
    )

    updated, action = jobs.upsert_posting(
        db,
        source=source,
        raw_record=changed_raw,
        candidate=candidate(title="来源岗位"),
    )

    assert action == "updated"
    assert updated.review_version == 1
    assert updated.source_changed_since_review is False


def test_sync_refreshes_cached_posting_before_protecting_reviewed_fields(
    db: Session,
) -> None:
    source = seeded_source(db)
    first_raw = snapshot(
        db, source_id=source.id, external_record_id="r1", payload_hash="a" * 64
    )
    cached, _ = jobs.upsert_posting(
        db, source=source, raw_record=first_raw, candidate=candidate(title="来源岗位")
    )
    db.execute(
        update(JobPosting)
        .where(JobPosting.id == cached.id)
        .values(
            status=JobPostingStatus.PENDING_REVIEW,
            review_version=1,
            title="人工确认岗位",
            description_text="人工补全的完整 JD",
        ),
        execution_options={"synchronize_session": False},
    )
    assert cached.status is JobPostingStatus.PENDING_COMPLETION
    assert cached.review_version == 0
    assert cached.title == "来源岗位"
    changed_raw = snapshot(
        db, source_id=source.id, external_record_id="r1", payload_hash="b" * 64
    )

    updated, action = jobs.upsert_posting(
        db,
        source=source,
        raw_record=changed_raw,
        candidate=candidate(title="来源新岗位"),
    )

    assert action == "updated"
    assert updated.status is JobPostingStatus.PENDING_REVIEW
    assert updated.review_version == 2
    assert updated.title == "人工确认岗位"
    assert updated.description_text == "人工补全的完整 JD"
    assert updated.source_candidate["title"] == "来源新岗位"
    assert updated.source_changed_since_review is True


def test_sync_does_not_overwrite_versioned_posting_with_reset_status(
    db: Session,
) -> None:
    source = seeded_source(db)
    first_raw = snapshot(
        db, source_id=source.id, external_record_id="r1", payload_hash="a" * 64
    )
    posting, _ = jobs.upsert_posting(
        db, source=source, raw_record=first_raw, candidate=candidate(title="来源岗位")
    )
    posting.title = "人工确认岗位"
    posting.review_version = 1
    db.flush()

    changed_raw = snapshot(
        db, source_id=source.id, external_record_id="r1", payload_hash="b" * 64
    )
    updated, action = jobs.upsert_posting(
        db,
        source=source,
        raw_record=changed_raw,
        candidate=candidate(title="来源新岗位"),
    )

    assert action == "updated"
    assert updated.title == "人工确认岗位"
    assert updated.source_candidate["title"] == "来源新岗位"
    assert updated.source_changed_since_review is True


def test_unchanged_sync_backfills_empty_source_candidate(db: Session) -> None:
    source = seeded_source(db)
    raw = snapshot(
        db, source_id=source.id, external_record_id="r1", payload_hash="a" * 64
    )
    posting, _ = jobs.upsert_posting(
        db, source=source, raw_record=raw, candidate=candidate(title="来源岗位")
    )
    posting.source_candidate = {}
    db.flush()

    unchanged, action = jobs.upsert_posting(
        db, source=source, raw_record=raw, candidate=candidate(title="来源岗位")
    )

    assert action == "unchanged"
    assert unchanged.source_candidate == {
        "company_name": "示例公司",
        "title": "来源岗位",
        "locations": ["北京"],
        "recruitment_types": ["暑期实习"],
        "industries": ["互联网"],
        "apply_url": "https://example.com/jobs",
        "referral_code": None,
        "deadline_text": None,
    }


def test_mapper_version_change_reprocesses_unchanged_snapshot(db: Session) -> None:
    source = seeded_source(db)
    raw = snapshot(
        db,
        source_id=source.id,
        external_record_id="r1",
        payload_hash="a" * 64,
    )
    posting, first_action = jobs.upsert_posting(
        db, source=source, raw_record=raw, candidate=candidate()
    )
    _, unchanged_action = jobs.upsert_posting(
        db, source=source, raw_record=raw, candidate=candidate(title="ignored")
    )

    source.mapper_version = "v2"
    updated, reprocessed_action = jobs.upsert_posting(
        db, source=source, raw_record=raw, candidate=candidate(title="重新映射")
    )

    assert (first_action, unchanged_action, reprocessed_action) == (
        "created",
        "unchanged",
        "updated",
    )
    assert updated.id == posting.id
    assert updated.raw_record_id == raw.id
    assert updated.mapper_version == "v2"
    assert updated.title == "重新映射"


def test_missing_upstream_rows_remain_queryable(db: Session) -> None:
    source = seeded_source(db)
    first_raw = snapshot(
        db,
        source_id=source.id,
        external_record_id="still-present",
        payload_hash="a" * 64,
    )
    second_raw = snapshot(
        db,
        source_id=source.id,
        external_record_id="seen-now",
        payload_hash="b" * 64,
    )
    first, _ = jobs.upsert_posting(
        db, source=source, raw_record=first_raw, candidate=candidate(title="旧岗位")
    )
    jobs.upsert_posting(
        db, source=source, raw_record=second_raw, candidate=candidate(title="新岗位")
    )

    total, rows = jobs.list_postings(
        db,
        limit=20,
        offset=0,
        source_key=source.source_key,
        company=None,
        recruitment_type=None,
    )

    assert total == 2
    assert first.id in {posting.id for posting, _ in rows}
    assert jobs.get_posting(db, first.id) == (first, source)


def test_posting_order_is_stable_by_updated_at_then_id(db: Session) -> None:
    source = seeded_source(db)
    postings: list[JobPosting] = []
    for index in range(3):
        raw = snapshot(
            db,
            source_id=source.id,
            external_record_id=f"r{index}",
            payload_hash=str(index) * 64,
        )
        posting, _ = jobs.upsert_posting(
            db, source=source, raw_record=raw, candidate=candidate(title=f"岗位{index}")
        )
        postings.append(posting)
    common_time = datetime(2026, 7, 15, tzinfo=timezone.utc)
    postings[0].updated_at = common_time - timedelta(seconds=1)
    postings[1].updated_at = common_time
    postings[2].updated_at = common_time
    db.flush()

    total, rows = jobs.list_postings(
        db,
        limit=20,
        offset=0,
        source_key=None,
        company=None,
        recruitment_type=None,
    )

    expected = sorted(postings[1:], key=lambda posting: posting.id, reverse=True)
    assert total == 3
    assert [posting.id for posting, _ in rows] == [
        expected[0].id,
        expected[1].id,
        postings[0].id,
    ]


@pytest.mark.parametrize("literal", ["%", "_", "\\"])
def test_company_filter_escapes_wildcards(db: Session, literal: str) -> None:
    source = seeded_source(db)
    matching_raw = snapshot(
        db,
        source_id=source.id,
        external_record_id=f"matching-{ord(literal)}",
        payload_hash="a" * 64,
    )
    other_raw = snapshot(
        db,
        source_id=source.id,
        external_record_id=f"other-{ord(literal)}",
        payload_hash="b" * 64,
    )
    matching, _ = jobs.upsert_posting(
        db,
        source=source,
        raw_record=matching_raw,
        candidate=candidate(company_name=f"甲{literal}乙"),
    )
    jobs.upsert_posting(
        db,
        source=source,
        raw_record=other_raw,
        candidate=candidate(company_name="甲X乙"),
    )

    total, rows = jobs.list_postings(
        db,
        limit=20,
        offset=0,
        source_key=None,
        company=literal,
        recruitment_type=None,
    )

    assert total == 1
    assert [posting.id for posting, _ in rows] == [matching.id]


def test_recruitment_type_filter_matches_complete_sqlite_json_label(
    db: Session,
) -> None:
    source = seeded_source(db)
    exact_raw = snapshot(
        db,
        source_id=source.id,
        external_record_id="exact",
        payload_hash="a" * 64,
    )
    substring_raw = snapshot(
        db,
        source_id=source.id,
        external_record_id="substring",
        payload_hash="b" * 64,
    )
    exact, _ = jobs.upsert_posting(
        db,
        source=source,
        raw_record=exact_raw,
        candidate=candidate(recruitment_types=["实习", "校招"]),
    )
    jobs.upsert_posting(
        db,
        source=source,
        raw_record=substring_raw,
        candidate=candidate(recruitment_types=["暑期实习"]),
    )

    total, rows = jobs.list_postings(
        db,
        limit=20,
        offset=0,
        source_key=None,
        company=None,
        recruitment_type="实习",
    )

    assert total == 1
    assert [posting.id for posting, _ in rows] == [exact.id]


def test_get_posting_excludes_non_pending_statuses(db: Session) -> None:
    source = seeded_source(db)
    raw = snapshot(
        db,
        source_id=source.id,
        external_record_id="r1",
        payload_hash="a" * 64,
    )
    posting, _ = jobs.upsert_posting(
        db, source=source, raw_record=raw, candidate=candidate()
    )
    assert posting.status is JobPostingStatus.PENDING_COMPLETION
    assert jobs.get_posting(db, posting.id) == (posting, source)


def test_public_query_only_returns_verified_jobs(db: Session) -> None:
    source = seeded_source(db)
    pending_raw = snapshot(
        db,
        source_id=source.id,
        external_record_id="pending",
        payload_hash="a" * 64,
    )
    verified_raw = snapshot(
        db,
        source_id=source.id,
        external_record_id="verified",
        payload_hash="b" * 64,
    )
    pending, _ = jobs.upsert_posting(
        db, source=source, raw_record=pending_raw, candidate=candidate()
    )
    verified, _ = jobs.upsert_posting(
        db, source=source, raw_record=verified_raw, candidate=candidate()
    )
    verified.status = JobPostingStatus.VERIFIED
    db.flush()

    total, rows = jobs.list_public_postings(
        db,
        limit=20,
        offset=0,
        source_key=None,
        company=None,
        recruitment_type=None,
    )

    assert total == 1
    assert [posting.id for posting, _source in rows] == [verified.id]
    assert jobs.get_public_posting(db, verified.id) == (verified, source)
    assert jobs.get_public_posting(db, pending.id) is None


def test_review_queue_filters_pending_statuses(db: Session) -> None:
    source = seeded_source(db)
    raw = snapshot(
        db,
        source_id=source.id,
        external_record_id="review",
        payload_hash="c" * 64,
    )
    posting, _ = jobs.upsert_posting(
        db, source=source, raw_record=raw, candidate=candidate()
    )
    posting.status = JobPostingStatus.PENDING_REVIEW
    db.flush()

    total, rows = jobs.list_review_queue(
        db, statuses={JobPostingStatus.PENDING_REVIEW}, limit=20, offset=0
    )

    assert total == 1
    assert rows[0][0].id == posting.id
    assert jobs.get_posting_for_review(db, posting.id) == (posting, source)


def test_locked_review_read_refreshes_loaded_posting(db: Session) -> None:
    source = seeded_source(db)
    raw = snapshot(
        db,
        source_id=source.id,
        external_record_id="stale-review",
        payload_hash="d" * 64,
    )
    posting, _ = jobs.upsert_posting(
        db, source=source, raw_record=raw, candidate=candidate(title="陈旧岗位")
    )
    db.execute(
        update(JobPosting)
        .where(JobPosting.id == posting.id)
        .values(title="数据库最新岗位"),
        execution_options={"synchronize_session": False},
    )
    assert posting.title == "陈旧岗位"

    locked = jobs.get_posting_for_review(db, posting.id, lock=True)

    assert locked is not None
    assert locked[0] is posting
    assert locked[0].title == "数据库最新岗位"


def test_empty_review_queue_statuses_include_all_reviewable_but_not_expired(
    db: Session,
) -> None:
    source = seeded_source(db)
    statuses = [
        JobPostingStatus.PENDING_COMPLETION,
        JobPostingStatus.PENDING_REVIEW,
        JobPostingStatus.REJECTED,
        JobPostingStatus.EXPIRED,
    ]
    postings: list[JobPosting] = []
    for index, status in enumerate(statuses):
        raw = snapshot(
            db,
            source_id=source.id,
            external_record_id=f"queue-{index}",
            payload_hash=str(index) * 64,
        )
        posting, _ = jobs.upsert_posting(
            db, source=source, raw_record=raw, candidate=candidate()
        )
        posting.status = status
        postings.append(posting)
    db.flush()

    total, rows = jobs.list_review_queue(db, statuses=set(), limit=20, offset=0)

    assert total == 3
    assert {posting.id for posting, _source in rows} == {
        posting.id for posting in postings[:3]
    }


def test_review_queue_rejects_non_reviewable_statuses(db: Session) -> None:
    with pytest.raises(ValueError, match="review queue status"):
        jobs.list_review_queue(
            db,
            statuses={JobPostingStatus.PENDING_REVIEW, JobPostingStatus.VERIFIED},
            limit=20,
            offset=0,
        )


def test_acquire_rejects_missing_or_disabled_source(db: Session) -> None:
    with pytest.raises(jobs.SourceNotFoundError):
        jobs.acquire_sync_run(db, "missing", now=utc_now())

    source = seeded_source(db)
    source.enabled = False
    db.flush()
    with pytest.raises(jobs.SourceDisabledError):
        jobs.acquire_sync_run(db, source.id, now=utc_now())


def test_finishing_requires_matching_lease_owner(db: Session) -> None:
    source = seeded_source(db)
    now = utc_now()
    run = JobSyncRun(
        source_id=source.id,
        status=JobSyncRunStatus.RUNNING,
        started_at=now,
    )
    db.add(run)
    db.flush()
    with pytest.raises(jobs.StaleSyncLeaseError):
        jobs.finish_sync_run(
            db,
            source.id,
            run.id,
            status=JobSyncRunStatus.FAILED,
            now=now,
            error_code="database_write_failed",
        )
