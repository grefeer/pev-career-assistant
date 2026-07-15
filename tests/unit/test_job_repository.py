from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, func, select
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
from backend.app.services.job_mappers import BUILTIN_SOURCES, NormalizedJobCandidate


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
    assert db.get(RawJobRecord, first_raw.id) is first_raw
    assert db.scalar(select(func.count()).select_from(RawJobRecord)) == 2


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
