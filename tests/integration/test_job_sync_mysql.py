from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import sys
from threading import Barrier
import uuid

import pytest
from sqlalchemy import create_engine, delete
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session

from backend.app.db.models import (
    JobPosting,
    JobPostingStatus,
    JobSource,
    JobSourceProvider,
    JobSyncRun,
    JobSyncRunStatus,
    RawJobRecord,
)
from backend.app.repositories import jobs


pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_MYSQL_URL"), reason="requires TEST_MYSQL_URL"
)


def _assert_dedicated_test_database(database_url: str) -> None:
    database_name = make_url(database_url).database
    assert database_name and database_name.endswith("_test"), (
        "TEST_MYSQL_URL must target a database whose name ends with _test"
    )


def _migrate_to_head(database_url: str) -> None:
    env = {
        **os.environ,
        "APP_AUTH_SECRET": "test-secret-with-at-least-32-characters",
        "DATABASE_URL": database_url,
        "OBJECT_ENCRYPTION_KEY": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        "REDIS_URL": "redis://localhost:6379/15",
    }
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture(scope="module")
def mysql_engine() -> Engine:
    database_url = os.environ["TEST_MYSQL_URL"]
    _assert_dedicated_test_database(database_url)
    _migrate_to_head(database_url)
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        yield engine
    finally:
        engine.dispose()


def _seed_source(db: Session, prefix: str) -> JobSource:
    source = JobSource(
        source_key=f"{prefix}-source",
        provider=JobSourceProvider.TENCENT_SMARTSHEET,
        name="Task 7 MySQL Gate",
        file_id=f"{prefix}-file",
        sheet_id=f"{prefix}-sheet",
        mapper_version="task7-v1",
        enabled=True,
    )
    db.add(source)
    db.flush()
    return source


def _seed_posting(
    db: Session,
    source: JobSource,
    *,
    record_id: str,
    recruitment_types: list[str],
) -> JobPosting:
    raw = RawJobRecord(
        source_id=source.id,
        external_record_id=record_id,
        payload_hash=uuid.uuid4().hex * 2,
        raw_fields=[{"field": "招聘类型", "value": recruitment_types}],
        observed_at=datetime.now(timezone.utc),
    )
    db.add(raw)
    db.flush()
    posting = JobPosting(
        source_id=source.id,
        external_record_id=record_id,
        raw_record_id=raw.id,
        status=JobPostingStatus.PENDING_COMPLETION,
        company_name="Task 7 Company",
        title="Task 7 Role",
        locations=[],
        recruitment_types=recruitment_types,
        industries=[],
        apply_url="https://example.com/task-7",
        mapper_version=source.mapper_version,
    )
    db.add(posting)
    db.flush()
    return posting


def _cleanup_source(db: Session, source_id: str) -> None:
    db.rollback()
    db.execute(delete(JobPosting).where(JobPosting.source_id == source_id))
    db.execute(delete(RawJobRecord).where(RawJobRecord.source_id == source_id))
    db.execute(delete(JobSyncRun).where(JobSyncRun.source_id == source_id))
    db.execute(delete(JobSource).where(JobSource.id == source_id))
    db.commit()


def test_mysql_exact_recruitment_type_filter(mysql_engine: Engine) -> None:
    prefix = f"task7-filter-{uuid.uuid4().hex}"
    with Session(mysql_engine, expire_on_commit=False) as db:
        source = _seed_source(db, prefix)
        exact = _seed_posting(
            db,
            source,
            record_id=f"{prefix}-exact",
            recruitment_types=["实习", "暑期实习"],
        )
        substring = _seed_posting(
            db,
            source,
            record_id=f"{prefix}-substring",
            recruitment_types=["暑期实习"],
        )
        source_id = source.id
        db.commit()
        try:
            total, rows = jobs.list_postings(
                db,
                limit=20,
                offset=0,
                source_key=source.source_key,
                company=None,
                recruitment_type="实习",
            )
            posting_ids = {item.id for item, _source in rows}
            assert exact.id in posting_ids
            assert substring.id not in posting_ids
            assert total == 1
        finally:
            _cleanup_source(db, source_id)


def test_mysql_active_source_lease_conflicts(mysql_engine: Engine) -> None:
    prefix = f"task7-conflict-{uuid.uuid4().hex}"
    with Session(mysql_engine, expire_on_commit=False) as db:
        source = _seed_source(db, prefix)
        source_id = source.id
        db.commit()
        try:
            first = jobs.acquire_sync_run(
                db, source.id, now=datetime.now(timezone.utc)
            )
            db.commit()
            with pytest.raises(jobs.SyncConflictError):
                jobs.acquire_sync_run(db, source.id, now=datetime.now(timezone.utc))
            db.rollback()
            assert first.status is JobSyncRunStatus.RUNNING
        finally:
            _cleanup_source(db, source_id)


def test_mysql_concurrent_lease_allows_exactly_one_owner(
    mysql_engine: Engine,
) -> None:
    prefix = f"task7-concurrent-{uuid.uuid4().hex}"
    with Session(mysql_engine, expire_on_commit=False) as setup:
        source = _seed_source(setup, prefix)
        source_id = source.id
        setup.commit()

    barrier = Barrier(2)

    def attempt() -> str:
        with Session(mysql_engine) as db:
            barrier.wait(timeout=10)
            try:
                jobs.acquire_sync_run(db, source_id, now=datetime.now(timezone.utc))
                db.commit()
                return "acquired"
            except jobs.SyncConflictError:
                db.rollback()
                return "conflict"

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _index: attempt(), range(2)))
        assert sorted(outcomes) == ["acquired", "conflict"]
    finally:
        with Session(mysql_engine) as cleanup:
            _cleanup_source(cleanup, source_id)
