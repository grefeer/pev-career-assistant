from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import subprocess
import sys
from threading import Barrier, Event, get_ident
import uuid

import pytest
from sqlalchemy import create_engine, delete, event, func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from backend.app.db.models import (
    AuditEvent,
    JobPosting,
    JobPostingStatus,
    JobSource,
    JobSourceProvider,
    JobSyncRun,
    JobSyncRunStatus,
    JobVerification,
    RawJobRecord,
    User,
    UserRole,
)
from backend.app.repositories import jobs
from backend.app.services.job_mappers import BUILTIN_SOURCES
from backend.app.services.job_review import (
    JobCompletionInput,
    JobReviewService,
    StaleJobReviewError,
)
from backend.app.services.job_sync import JobSyncService, SyncOutcome
from backend.app.services.tencent_smartsheet import (
    TencentField,
    TencentRecord,
    TencentRecordPage,
)


INTERN_SOURCE_KEY = "tencent-intern-referrals"


class MutableGateway:
    def __init__(self, record: TencentRecord) -> None:
        self.record = record

    def list_fields(self, _file_id: str, _sheet_id: str) -> list[TencentField]:
        return [
            TencentField("company", "公司名称", "text"),
            TencentField("title", "招聘岗位", "text"),
            TencentField("url", "投递链接", "url"),
        ]

    def list_records(
        self,
        _file_id: str,
        _sheet_id: str,
        *,
        offset: int,
        limit: int,
    ) -> TencentRecordPage:
        assert offset == 0
        assert limit == 100
        return TencentRecordPage([self.record], 1, False, 0)


def _text_field(title: str, value: str) -> dict[str, object]:
    return {
        "field": title,
        "text_value": {"items": [{"text": value, "type": "text"}]},
    }


def _option_field(title: str, values: list[str]) -> dict[str, object]:
    return {
        "field": title,
        "option_value": {
            "items": [{"text": value, "type": "option"} for value in values]
        },
    }


def _changed_record(record_id: str) -> TencentRecord:
    return TencentRecord(
        record_id,
        [
            _text_field("公司名称", "来源更新公司"),
            _text_field("招聘岗位", "来源更新岗位"),
            {
                "field": "投递链接",
                "url_value": {
                    "items": [{"link": "https://source.example.com/changed"}]
                },
            },
            _text_field("工作地点", "北京、深圳"),
            _option_field("招聘类型", ["实习", "校招"]),
            _option_field("多选", ["软件"]),
            _text_field("内推码", "SOURCE-REFERRAL"),
            _text_field("截止日期", "2027-01-01"),
            {"field": "更新时间", "string_value": "1810000000000"},
        ],
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
def mysql_engine(destructive_mysql_url: str) -> Engine:
    _migrate_to_head(destructive_mysql_url)
    engine = create_engine(destructive_mysql_url, pool_pre_ping=True)
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
            first = jobs.acquire_sync_run(db, source.id, now=datetime.now(timezone.utc))
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


def test_mysql_job_sync_service_lock_order_preserves_review_and_stales_admin(
    mysql_engine: Engine,
) -> None:
    record_id = f"task7-service-{uuid.uuid4().hex}"
    actor_id = str(uuid.uuid4())
    reviewed_at = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=1)
    with Session(mysql_engine, expire_on_commit=False) as setup:
        jobs.ensure_builtin_sources(setup, BUILTIN_SOURCES)
        source = jobs.get_source(setup, INTERN_SOURCE_KEY)
        assert source is not None
        admin = User(
            id=actor_id,
            account=f"task7-sync-admin-{uuid.uuid4().hex}",
            nickname="Task 7 Sync Admin",
            password_hash="unused",
            role=UserRole.ADMIN,
        )
        setup.add(admin)
        old_raw_fields = [{"field": "old", "value": "immutable-old-value"}]
        old_raw = RawJobRecord(
            source_id=source.id,
            external_record_id=record_id,
            payload_hash="0" * 64,
            raw_fields=old_raw_fields,
            observed_at=reviewed_at,
        )
        setup.add(old_raw)
        setup.flush()
        posting = JobPosting(
            source_id=source.id,
            external_record_id=record_id,
            raw_record_id=old_raw.id,
            status=JobPostingStatus.VERIFIED,
            company_name="人工确认公司",
            title="人工确认岗位",
            description_text="人工确认的完整 JD",
            locations=["上海"],
            recruitment_types=["实习"],
            industries=["人工行业"],
            apply_url="https://reviewed.example.com/apply",
            referral_code="HUMAN-REFERRAL",
            deadline_text="2026-12-31",
            mapper_version=source.mapper_version,
            source_candidate={
                "company_name": "旧来源公司",
                "title": "旧来源岗位",
                "locations": ["旧地点"],
                "recruitment_types": ["旧类型"],
                "industries": ["旧行业"],
                "apply_url": "https://source.example.com/old",
                "referral_code": "OLD",
                "deadline_text": "2026-01-01",
            },
            review_version=7,
            verified_at=reviewed_at,
            gui_eligible=True,
        )
        setup.add(posting)
        setup.flush()
        setup.add(
            JobVerification(
                job_id=posting.id,
                actor_user_id=actor_id,
                action="verified",
                from_status=JobPostingStatus.PENDING_REVIEW.value,
                to_status=JobPostingStatus.VERIFIED.value,
                review_version=7,
                field_snapshot={"title": "人工确认岗位"},
                created_at=reviewed_at,
            )
        )
        setup.commit()
        source_id = source.id
        posting_id = posting.id
        old_raw_id = old_raw.id

    reviewer_holds_lock = Event()
    sync_posting_lock_started = Event()
    sync_thread_id: list[int] = []
    lock_transactions: list[list[str]] = [[]]

    def capture_sync_lock_order(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if not sync_thread_id or get_ident() != sync_thread_id[0]:
            return
        normalized = statement.upper()
        if "FOR UPDATE" not in normalized:
            return
        if "JOB_POSTINGS" in normalized:
            lock_transactions[-1].append("posting")
            sync_posting_lock_started.set()
        elif "JOB_SOURCES" in normalized:
            lock_transactions[-1].append("source")

    def capture_sync_commit(_session: Session) -> None:
        lock_transactions.append([])

    def review_lock_attempt() -> None:
        with Session(mysql_engine) as db:
            db.execute(text("SET SESSION innodb_lock_wait_timeout = 10"))
            row = jobs.get_posting_for_review(db, posting_id, lock=True)
            assert row is not None
            reviewer_holds_lock.set()
            if not sync_posting_lock_started.wait(timeout=10):
                db.rollback()
                raise TimeoutError("sync service did not issue the posting lock query")
            db.commit()

    def sync_attempt() -> SyncOutcome:
        if not reviewer_holds_lock.wait(timeout=10):
            raise TimeoutError("reviewer did not acquire the posting lock")
        sync_thread_id.append(get_ident())
        with Session(mysql_engine) as db:
            db.execute(text("SET SESSION innodb_lock_wait_timeout = 10"))
            event.listen(db, "after_commit", capture_sync_commit)
            try:
                return JobSyncService(MutableGateway(_changed_record(record_id))).sync(
                    db,
                    source_key=INTERN_SOURCE_KEY,
                    actor_user_id=actor_id,
                )
            finally:
                event.remove(db, "after_commit", capture_sync_commit)

    event.listen(mysql_engine, "before_cursor_execute", capture_sync_lock_order)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            reviewer = pool.submit(review_lock_attempt)
            syncer = pool.submit(sync_attempt)
            reviewer.result(timeout=25)
            outcome = syncer.result(timeout=25)

        assert outcome.status is JobSyncRunStatus.SUCCEEDED
        page_transactions = [
            transaction for transaction in lock_transactions if "posting" in transaction
        ]
        assert len(page_transactions) == 1
        page_lock_order = page_transactions[0]
        assert "source" in page_lock_order
        assert page_lock_order.index("source") < page_lock_order.index("posting")

        with Session(mysql_engine) as verification:
            persisted = verification.get(JobPosting, posting_id)
            old_raw = verification.get(RawJobRecord, old_raw_id)
            assert persisted is not None
            assert old_raw is not None
            assert persisted.status is JobPostingStatus.VERIFIED
            assert persisted.company_name == "人工确认公司"
            assert persisted.title == "人工确认岗位"
            assert persisted.description_text == "人工确认的完整 JD"
            assert persisted.locations == ["上海"]
            assert persisted.recruitment_types == ["实习"]
            assert persisted.industries == ["人工行业"]
            assert persisted.apply_url == "https://reviewed.example.com/apply"
            assert persisted.referral_code == "HUMAN-REFERRAL"
            assert persisted.deadline_text == "2026-12-31"
            assert persisted.gui_eligible is True
            assert persisted.verified_at == reviewed_at.replace(tzinfo=None)
            assert persisted.source_candidate == {
                "company_name": "来源更新公司",
                "title": "来源更新岗位",
                "locations": ["北京", "深圳"],
                "recruitment_types": ["实习", "校招"],
                "industries": ["软件"],
                "apply_url": "https://source.example.com/changed",
                "referral_code": "SOURCE-REFERRAL",
                "deadline_text": "2027-01-01",
            }
            assert persisted.source_changed_since_review is True
            assert persisted.review_version == 8
            assert old_raw.raw_fields == old_raw_fields
            assert old_raw.payload_hash == "0" * 64
            assert verification.scalar(
                select(func.count())
                .select_from(RawJobRecord)
                .where(
                    RawJobRecord.source_id == source_id,
                    RawJobRecord.external_record_id == record_id,
                )
            ) == 2

            with pytest.raises(StaleJobReviewError):
                JobReviewService().save_completion(
                    verification,
                    job_id=posting_id,
                    actor_user_id=actor_id,
                    expected_version=7,
                    values=JobCompletionInput(
                        company_name="旧管理员公司",
                        title="旧管理员岗位",
                        description_text="旧管理员 JD",
                        locations=["北京"],
                        recruitment_types=["校招"],
                        industries=["旧行业"],
                        apply_url="https://stale.example.com/apply",
                        referral_code=None,
                        deadline_text=None,
                    ),
                )
            verification.rollback()
            assert verification.scalar(
                select(func.count())
                .select_from(JobVerification)
                .where(JobVerification.job_id == posting_id)
            ) == 1
    finally:
        event.remove(mysql_engine, "before_cursor_execute", capture_sync_lock_order)
        with Session(mysql_engine) as cleanup:
            run_ids = list(
                cleanup.scalars(
                    select(AuditEvent.entity_id).where(
                        AuditEvent.actor_user_id == actor_id,
                        AuditEvent.entity_type == "job_sync_run",
                    )
                )
            )
            source = cleanup.get(JobSource, source_id)
            if source is not None:
                source.active_sync_run_id = None
                source.sync_lease_expires_at = None
            cleanup.execute(delete(AuditEvent).where(AuditEvent.actor_user_id == actor_id))
            cleanup.execute(delete(JobVerification).where(JobVerification.job_id == posting_id))
            cleanup.execute(delete(JobPosting).where(JobPosting.id == posting_id))
            cleanup.execute(
                delete(RawJobRecord).where(
                    RawJobRecord.source_id == source_id,
                    RawJobRecord.external_record_id == record_id,
                )
            )
            if run_ids:
                cleanup.execute(delete(JobSyncRun).where(JobSyncRun.id.in_(run_ids)))
            cleanup.execute(delete(User).where(User.id == actor_id))
            cleanup.commit()


def test_mysql_concurrent_admin_review_commits_one_state_and_event(
    mysql_engine: Engine,
) -> None:
    prefix = f"task7-admin-review-{uuid.uuid4().hex}"
    actor_id = str(uuid.uuid4())
    with Session(mysql_engine, expire_on_commit=False) as setup:
        source = _seed_source(setup, prefix)
        raw = RawJobRecord(
            source_id=source.id,
            external_record_id=f"{prefix}-record",
            payload_hash="c" * 64,
            raw_fields=[{"field": "seed", "value": "pending-review"}],
            observed_at=datetime.now(timezone.utc),
        )
        admin = User(
            id=actor_id,
            account=f"{prefix}-admin",
            nickname="Task 7 Review Admin",
            password_hash="unused",
            role=UserRole.ADMIN,
        )
        setup.add_all([raw, admin])
        setup.flush()
        posting = JobPosting(
            source_id=source.id,
            external_record_id=raw.external_record_id,
            raw_record_id=raw.id,
            status=JobPostingStatus.PENDING_REVIEW,
            company_name="并发公司",
            title="并发岗位",
            description_text="并发审核的完整 JD",
            locations=["上海"],
            recruitment_types=["实习"],
            industries=["软件"],
            apply_url="https://concurrent.example.com/apply",
            mapper_version=source.mapper_version,
            source_candidate={},
            review_version=11,
        )
        setup.add(posting)
        setup.commit()
        source_id = source.id
        posting_id = posting.id

    attempts_ready = Barrier(2)

    def verify_attempt() -> str:
        with Session(mysql_engine) as db:
            db.execute(text("SET SESSION innodb_lock_wait_timeout = 10"))
            attempts_ready.wait(timeout=10)
            try:
                JobReviewService().verify(
                    db,
                    job_id=posting_id,
                    actor_user_id=actor_id,
                    expected_version=11,
                    gui_eligible=True,
                )
                db.commit()
                return "success"
            except StaleJobReviewError:
                db.rollback()
                return "stale"

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [
                future.result(timeout=25)
                for future in [pool.submit(verify_attempt), pool.submit(verify_attempt)]
            ]
        assert sorted(results) == ["stale", "success"]
        with Session(mysql_engine) as verification:
            persisted = verification.get(JobPosting, posting_id)
            events = list(
                verification.scalars(
                    select(JobVerification).where(JobVerification.job_id == posting_id)
                )
            )
            assert persisted is not None
            assert persisted.status is JobPostingStatus.VERIFIED
            assert persisted.review_version == 12
            assert persisted.gui_eligible is True
            assert len(events) == 1
            assert events[0].action == "verified"
            assert events[0].review_version == 12
            assert events[0].field_snapshot["title"] == "并发岗位"
    finally:
        with Session(mysql_engine) as cleanup:
            _cleanup_source(cleanup, source_id)
            cleanup.execute(delete(User).where(User.id == actor_id))
            cleanup.commit()
