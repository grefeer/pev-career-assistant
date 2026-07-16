from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import sys
from threading import Barrier, Event
import uuid

import pytest
from sqlalchemy import create_engine, delete, event, text
from sqlalchemy.engine import Engine
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
from backend.app.services.job_mappers import NormalizedJobCandidate
from tests.integration.job_sync_gate_safety import (
    require_dedicated_mysql_test_database,
)


requires_mysql = pytest.mark.skipif(
    not os.environ.get("TEST_MYSQL_URL"), reason="requires TEST_MYSQL_URL"
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


def test_database_guard_rejects_non_mysql_backend() -> None:
    invalid_url = "sqlite+pysqlite:///" + "career_assistant_test"
    with pytest.raises(ValueError, match="MySQL.*_test"):
        require_dedicated_mysql_test_database(invalid_url)


def test_database_guard_rejects_postgresql_backend() -> None:
    invalid_url = "postgresql+psycopg://root@localhost/" + "career_assistant_test"
    with pytest.raises(ValueError, match="MySQL.*_test"):
        require_dedicated_mysql_test_database(invalid_url)


def test_database_guard_rejects_mysql_non_test_database() -> None:
    invalid_url = "mysql+pymysql://root@localhost/" + "career_assistant"
    with pytest.raises(ValueError, match="MySQL.*_test"):
        require_dedicated_mysql_test_database(invalid_url)


def test_database_guard_accepts_dedicated_mysql_test_database() -> None:
    valid_url = "mysql+pymysql://root@localhost/" + "career_assistant_test"
    require_dedicated_mysql_test_database(valid_url)


def test_database_guard_is_active_under_python_optimization() -> None:
    env = {
        **os.environ,
        "TASK7_GUARD_URL": "mysql+pymysql://root@localhost/" + "career_assistant",
    }
    script = """
import os
from tests.integration.job_sync_gate_safety import require_dedicated_mysql_test_database

try:
    require_dedicated_mysql_test_database(os.environ["TASK7_GUARD_URL"])
except ValueError:
    raise SystemExit(0)
raise SystemExit(1)
"""
    completed = subprocess.run(
        [sys.executable, "-O", "-c", script],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0
    assert completed.stdout == ""
    assert completed.stderr == ""


@pytest.fixture(scope="module")
def mysql_engine() -> Engine:
    database_url = os.environ["TEST_MYSQL_URL"]
    require_dedicated_mysql_test_database(database_url)
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


@requires_mysql
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


@requires_mysql
def test_mysql_active_source_lease_conflicts(mysql_engine: Engine) -> None:
    prefix = f"task7-conflict-{uuid.uuid4().hex}"
    with Session(mysql_engine, expire_on_commit=False) as db:
        source = _seed_source(db, prefix)
        source_id = source.id
        db.commit()
        try:
            first = jobs.acquire_sync_run(db, source.id, now=datetime.now(timezone.utc))
            db.commit()
            with pytest.raises(jobs.SyncConflictError):
                jobs.acquire_sync_run(db, source.id, now=datetime.now(timezone.utc))
            db.rollback()
            assert first.status is JobSyncRunStatus.RUNNING
        finally:
            _cleanup_source(db, source_id)


@requires_mysql
def test_mysql_concurrent_lease_allows_exactly_one_owner(
    mysql_engine: Engine,
) -> None:
    prefix = f"task7-concurrent-{uuid.uuid4().hex}"
    with Session(mysql_engine, expire_on_commit=False) as setup:
        source = _seed_source(setup, prefix)
        source_id = source.id
        setup.commit()

    connections_ready = Barrier(2)
    owner_holds_lease = Event()
    contender_for_update_started = Event()

    def owner_attempt() -> tuple[str, int]:
        with Session(mysql_engine) as db:
            connection = db.connection()
            connection_id = int(connection.scalar(text("SELECT CONNECTION_ID()")))
            connections_ready.wait(timeout=10)
            jobs.acquire_sync_run(db, source_id, now=datetime.now(timezone.utc))
            owner_holds_lease.set()
            if not contender_for_update_started.wait(timeout=10):
                db.rollback()
                raise TimeoutError("contender did not issue its lease lock query")
            db.commit()
            return "acquired", connection_id

    def contender_attempt() -> tuple[str, int]:
        with Session(mysql_engine) as db:
            connection = db.connection()
            connection_id = int(connection.scalar(text("SELECT CONNECTION_ID()")))
            connections_ready.wait(timeout=10)
            if not owner_holds_lease.wait(timeout=10):
                raise TimeoutError("owner did not acquire the lease")

            def signal_for_update(
                _connection: object,
                _cursor: object,
                statement: str,
                _parameters: object,
                _context: object,
                _executemany: bool,
            ) -> None:
                normalized = statement.upper()
                if "JOB_SOURCES" in normalized and "FOR UPDATE" in normalized:
                    contender_for_update_started.set()

            event.listen(connection, "before_cursor_execute", signal_for_update)
            try:
                try:
                    jobs.acquire_sync_run(db, source_id, now=datetime.now(timezone.utc))
                except jobs.SyncConflictError:
                    db.rollback()
                    return "conflict", connection_id
                db.commit()
                return "acquired", connection_id
            finally:
                event.remove(connection, "before_cursor_execute", signal_for_update)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            owner_future = pool.submit(owner_attempt)
            contender_future = pool.submit(contender_attempt)
            results = [
                owner_future.result(timeout=20),
                contender_future.result(timeout=20),
            ]
        assert len({connection_id for _outcome, connection_id in results}) == 2
        assert sorted(outcome for outcome, _connection_id in results) == [
            "acquired",
            "conflict",
        ]
    finally:
        with Session(mysql_engine) as cleanup:
            _cleanup_source(cleanup, source_id)


@requires_mysql
def test_mysql_sync_waits_for_review_lock_and_preserves_reviewed_fields(
    mysql_engine: Engine,
) -> None:
    prefix = f"task2-review-lock-{uuid.uuid4().hex}"
    with Session(mysql_engine, expire_on_commit=False) as setup:
        source = _seed_source(setup, prefix)
        posting = _seed_posting(
            setup,
            source,
            record_id=f"{prefix}-record",
            recruitment_types=["暑期实习"],
        )
        changed_raw = RawJobRecord(
            source_id=source.id,
            external_record_id=posting.external_record_id,
            payload_hash="b" * 64,
            raw_fields=[{"field": "招聘岗位", "value": "来源新岗位"}],
            observed_at=datetime.now(timezone.utc),
        )
        setup.add(changed_raw)
        setup.commit()
        source_id = source.id
        posting_id = posting.id
        raw_id = changed_raw.id

    reviewer_holds_lock = Event()
    sync_lock_started = Event()

    def review_attempt() -> None:
        with Session(mysql_engine) as db:
            row = jobs.get_posting_for_review(db, posting_id, lock=True)
            assert row is not None
            posting, _source = row
            posting.status = JobPostingStatus.PENDING_REVIEW
            posting.review_version = 1
            posting.title = "人工确认岗位"
            posting.description_text = "人工补全的完整 JD"
            db.flush()
            reviewer_holds_lock.set()
            if not sync_lock_started.wait(timeout=10):
                db.rollback()
                raise TimeoutError("sync did not issue its posting lock query")
            db.commit()

    def sync_attempt() -> tuple[str, str, str | None, bool]:
        with Session(mysql_engine) as db:
            if not reviewer_holds_lock.wait(timeout=10):
                raise TimeoutError("reviewer did not acquire the posting lock")
            connection = db.connection()

            def signal_for_update(
                _connection: object,
                _cursor: object,
                statement: str,
                _parameters: object,
                _context: object,
                _executemany: bool,
            ) -> None:
                normalized = statement.upper()
                if "JOB_POSTINGS" in normalized and "FOR UPDATE" in normalized:
                    sync_lock_started.set()

            event.listen(connection, "before_cursor_execute", signal_for_update)
            try:
                source = db.get(JobSource, source_id)
                raw = db.get(RawJobRecord, raw_id)
                assert source is not None
                assert raw is not None
                updated, action = jobs.upsert_posting(
                    db,
                    source=source,
                    raw_record=raw,
                    candidate=NormalizedJobCandidate(
                        company_name="来源公司",
                        title="来源新岗位",
                        locations=["北京"],
                        recruitment_types=["暑期实习"],
                        industries=["互联网"],
                        apply_url="https://example.com/changed",
                        referral_code=None,
                        deadline_text=None,
                        source_updated_at=None,
                    ),
                )
                db.commit()
                return (
                    action,
                    updated.title,
                    updated.description_text,
                    updated.source_changed_since_review,
                )
            finally:
                event.remove(connection, "before_cursor_execute", signal_for_update)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            reviewer = pool.submit(review_attempt)
            syncer = pool.submit(sync_attempt)
            reviewer.result(timeout=20)
            result = syncer.result(timeout=20)
        assert result == (
            "updated",
            "人工确认岗位",
            "人工补全的完整 JD",
            True,
        )
        with Session(mysql_engine) as verification:
            posting = verification.get(JobPosting, posting_id)
            assert posting is not None
            assert posting.source_candidate["title"] == "来源新岗位"
    finally:
        with Session(mysql_engine) as cleanup:
            _cleanup_source(cleanup, source_id)
