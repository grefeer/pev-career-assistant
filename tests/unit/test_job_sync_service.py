from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from backend.app.db.base import Base
from backend.app.db.models import (
    AuditEvent,
    JobPosting,
    JobSyncRun,
    JobSyncRunStatus,
    RawJobRecord,
    User,
    UserRole,
)
from backend.app.repositories import jobs
from backend.app.services import job_sync
from backend.app.services.job_sync import JobSyncFailedError, JobSyncService
from backend.app.services.tencent_smartsheet import (
    TencentField,
    TencentRecord,
    TencentRecordPage,
    TencentTimeoutError,
)


NOW = datetime(2026, 7, 15, tzinfo=timezone.utc)
INTERN_SOURCE = "tencent-intern-referrals"
SOURCE_27 = "tencent-27-referrals"


class FakeGateway:
    def __init__(
        self,
        *,
        fields: list[TencentField],
        pages: dict[int, TencentRecordPage],
        failure_at_offset: int | None = None,
    ) -> None:
        self.fields = fields
        self.pages = pages
        self.failure_at_offset = failure_at_offset
        self.calls: list[int] = []

    def list_fields(self, _file_id: str, _sheet_id: str) -> list[TencentField]:
        return self.fields

    def list_records(
        self,
        _file_id: str,
        _sheet_id: str,
        *,
        offset: int,
        limit: int,
    ) -> TencentRecordPage:
        assert limit == 100
        self.calls.append(offset)
        if offset == self.failure_at_offset:
            raise TencentTimeoutError("constant test failure")
        return self.pages[offset]


@pytest.fixture
def db() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            User(
                id="admin",
                account="admin",
                nickname="Admin",
                password_hash="not-used",
                role=UserRole.ADMIN,
            )
        )
        session.commit()
        yield session


def intern_fields() -> list[TencentField]:
    return [
        TencentField("company", "公司名称", "text"),
        TencentField("title", "招聘岗位", "text"),
        TencentField("url", "投递链接", "url"),
    ]


def fields_27() -> list[TencentField]:
    return [
        TencentField("company", "企业名称", "text"),
        TencentField("url", "内推链接", "url"),
    ]


def complete_record(record_id: str, *, title: str = "工程师") -> TencentRecord:
    return TencentRecord(
        record_id,
        [
            {
                "field": "公司名称",
                "text_value": {"items": [{"text": "示例公司", "type": "text"}]},
            },
            {
                "field": "招聘岗位",
                "text_value": {"items": [{"text": title, "type": "text"}]},
            },
            {
                "field": "投递链接",
                "url_value": {"items": [{"link": "https://example.com/jobs"}]},
            },
        ],
    )


def service(gateway: FakeGateway, correlation_id: str = "c1") -> JobSyncService:
    return JobSyncService(
        gateway,
        now=lambda: NOW,
        correlation_id_factory=lambda: correlation_id,
    )


def scalar_count(db: Session, model: type[object]) -> int:
    return int(db.scalar(select(func.count()).select_from(model)) or 0)


def test_sync_commits_each_page_and_is_idempotent(db: Session) -> None:
    gateway = FakeGateway(
        fields=intern_fields(),
        pages={
            0: TencentRecordPage([complete_record("r1")], 2, True, 1),
            1: TencentRecordPage([complete_record("r2")], 2, False, 0),
        },
    )
    sync = service(gateway)

    first = sync.sync(db, source_key=INTERN_SOURCE, actor_user_id="admin")
    second = sync.sync(db, source_key=INTERN_SOURCE, actor_user_id="admin")

    assert first.status is JobSyncRunStatus.SUCCEEDED
    assert (first.pages_read, first.records_read) == (2, 2)
    assert (first.raw_snapshots_created, first.postings_created) == (2, 2)
    assert second.raw_snapshots_created == 0
    assert second.postings_created == 0
    assert second.postings_updated == 0
    assert gateway.calls == [0, 1, 0, 1]


def test_second_page_failure_preserves_first_page(db: Session) -> None:
    gateway = FakeGateway(
        fields=intern_fields(),
        pages={0: TencentRecordPage([complete_record("r1")], 2, True, 1)},
        failure_at_offset=1,
    )

    with pytest.raises(JobSyncFailedError) as caught:
        service(gateway, "c2").sync(db, source_key=INTERN_SOURCE, actor_user_id="admin")

    assert caught.value.status is JobSyncRunStatus.PARTIAL
    assert caught.value.error_code == "tencent_timeout"
    assert scalar_count(db, JobPosting) == 1
    assert scalar_count(db, RawJobRecord) == 1


def test_schema_failure_before_first_page_is_failed(db: Session) -> None:
    gateway = FakeGateway(fields=[], pages={})

    with pytest.raises(JobSyncFailedError) as caught:
        service(gateway).sync(db, source_key=INTERN_SOURCE, actor_user_id="admin")

    assert caught.value.status is JobSyncRunStatus.FAILED
    assert caught.value.error_code == "source_schema_changed"
    assert gateway.calls == []


def test_source_27_records_create_raw_snapshots_but_no_postings(db: Session) -> None:
    record = TencentRecord(
        "r27",
        [
            {
                "field": "企业名称",
                "text_value": {"items": [{"text": "示例公司"}]},
            },
            {
                "field": "内推链接",
                "url_value": {"items": [{"link": "https://example.com/jobs"}]},
            },
        ],
    )
    gateway = FakeGateway(
        fields=fields_27(),
        pages={0: TencentRecordPage([record], 1, False, 0)},
    )

    outcome = service(gateway).sync(db, source_key=SOURCE_27, actor_user_id="admin")

    assert outcome.raw_snapshots_created == 1
    assert outcome.records_skipped_incomplete == 1
    assert outcome.postings_created == 0
    assert scalar_count(db, RawJobRecord) == 1


def test_incomplete_record_is_retained_and_counted(db: Session) -> None:
    incomplete = TencentRecord(
        "incomplete",
        [
            {
                "field": "公司名称",
                "text_value": {"items": [{"text": "示例公司"}]},
            }
        ],
    )
    gateway = FakeGateway(
        fields=intern_fields(),
        pages={0: TencentRecordPage([incomplete], 1, False, 0)},
    )

    outcome = service(gateway).sync(db, source_key=INTERN_SOURCE, actor_user_id="admin")

    assert outcome.records_skipped_incomplete == 1
    assert outcome.raw_snapshots_created == 1
    assert scalar_count(db, RawJobRecord) == 1
    assert scalar_count(db, JobPosting) == 0


def test_changed_content_creates_snapshot_and_updates_posting(db: Session) -> None:
    gateway = FakeGateway(
        fields=intern_fields(),
        pages={0: TencentRecordPage([complete_record("r1")], 1, False, 0)},
    )
    sync = service(gateway)
    sync.sync(db, source_key=INTERN_SOURCE, actor_user_id="admin")
    gateway.pages[0] = TencentRecordPage(
        [complete_record("r1", title="高级工程师")], 1, False, 0
    )

    changed = sync.sync(db, source_key=INTERN_SOURCE, actor_user_id="admin")

    assert changed.raw_snapshots_created == 1
    assert changed.postings_created == 0
    assert changed.postings_updated == 1
    assert scalar_count(db, RawJobRecord) == 2
    assert scalar_count(db, JobPosting) == 1
    assert db.scalar(select(JobPosting.title)) == "高级工程师"


def test_missing_upstream_record_does_not_delete_posting(db: Session) -> None:
    gateway = FakeGateway(
        fields=intern_fields(),
        pages={0: TencentRecordPage([complete_record("r1")], 1, False, 0)},
    )
    sync = service(gateway)
    sync.sync(db, source_key=INTERN_SOURCE, actor_user_id="admin")
    gateway.pages[0] = TencentRecordPage([], 0, False, 0)

    sync.sync(db, source_key=INTERN_SOURCE, actor_user_id="admin")

    assert scalar_count(db, JobPosting) == 1


def test_page_cap_fails_after_preserving_committed_page(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(job_sync, "MAX_PAGES", 1)
    gateway = FakeGateway(
        fields=intern_fields(),
        pages={0: TencentRecordPage([complete_record("r1")], 2, True, 1)},
    )

    with pytest.raises(JobSyncFailedError) as caught:
        service(gateway).sync(db, source_key=INTERN_SOURCE, actor_user_id="admin")

    assert caught.value.error_code == "tencent_protocol_error"
    assert caught.value.status is JobSyncRunStatus.PARTIAL
    assert scalar_count(db, JobPosting) == 1


def test_record_cap_fails_without_committing_oversized_page(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(job_sync, "MAX_RECORDS", 1)
    gateway = FakeGateway(
        fields=intern_fields(),
        pages={
            0: TencentRecordPage(
                [complete_record("r1"), complete_record("r2")], 2, False, 0
            )
        },
    )

    with pytest.raises(JobSyncFailedError) as caught:
        service(gateway).sync(db, source_key=INTERN_SOURCE, actor_user_id="admin")

    assert caught.value.error_code == "tencent_protocol_error"
    assert caught.value.status is JobSyncRunStatus.FAILED
    assert scalar_count(db, RawJobRecord) == 0


def test_audit_payloads_are_whitelisted(db: Session) -> None:
    gateway = FakeGateway(
        fields=intern_fields(),
        pages={0: TencentRecordPage([complete_record("r1")], 1, False, 0)},
    )

    outcome = service(gateway, "correlation").sync(
        db, source_key=INTERN_SOURCE, actor_user_id="admin"
    )
    events = list(db.scalars(select(AuditEvent).order_by(AuditEvent.id)))

    assert [event.event_type for event in events] == [
        "job_sync.started",
        "job_sync.finished",
    ]
    assert all(event.correlation_id == "correlation" for event in events)
    assert events[0].redacted_payload == {
        "source_key": INTERN_SOURCE,
        "run_id": outcome.run_id,
    }
    assert set(events[1].redacted_payload) == {
        "source_key",
        "run_id",
        "status",
        "pages_read",
        "records_read",
        "raw_snapshots_created",
        "postings_created",
        "postings_updated",
        "records_skipped_incomplete",
    }


def test_failed_audit_contains_only_safe_ids_counters_and_error(db: Session) -> None:
    gateway = FakeGateway(fields=[], pages={})

    with pytest.raises(JobSyncFailedError) as caught:
        service(gateway, "correlation").sync(
            db, source_key=INTERN_SOURCE, actor_user_id="admin"
        )
    finished = db.scalar(
        select(AuditEvent).where(AuditEvent.event_type == "job_sync.finished")
    )

    assert finished is not None
    assert finished.redacted_payload == {
        "source_key": INTERN_SOURCE,
        "run_id": caught.value.run_id,
        "status": "failed",
        "pages_read": 0,
        "records_read": 0,
        "error_code": "source_schema_changed",
    }
    run = db.get(JobSyncRun, caught.value.run_id)
    assert run is not None
    assert run.error_code == "source_schema_changed"


def _install_takeover(db: Session, *, source_id: str, old_run_id: str) -> JobSyncRun:
    old_run = db.get(JobSyncRun, old_run_id)
    source = next(source for source in jobs.list_sources(db) if source.id == source_id)
    assert old_run is not None
    old_run.status = JobSyncRunStatus.FAILED
    old_run.error_code = "sync_lease_expired"
    old_run.finished_at = NOW
    replacement = JobSyncRun(
        source_id=source_id,
        status=JobSyncRunStatus.RUNNING,
        started_at=NOW,
    )
    db.add(replacement)
    db.flush()
    source.active_sync_run_id = replacement.id
    source.sync_lease_expires_at = NOW + timedelta(minutes=10)
    db.commit()
    return replacement


def test_stale_page_refresh_rolls_back_page_and_preserves_new_owner(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = FakeGateway(
        fields=intern_fields(),
        pages={0: TencentRecordPage([complete_record("r1")], 1, False, 0)},
    )
    replacement_id: str | None = None

    def lose_lease(
        session: Session, source_id: str, run_id: str, *, now: datetime
    ) -> None:
        del now
        nonlocal replacement_id
        session.rollback()
        replacement_id = _install_takeover(
            session, source_id=source_id, old_run_id=run_id
        ).id
        raise jobs.StaleSyncLeaseError(run_id)

    monkeypatch.setattr(jobs, "refresh_sync_lease", lose_lease)

    with pytest.raises(JobSyncFailedError) as caught:
        service(gateway).sync(db, source_key=INTERN_SOURCE, actor_user_id="admin")

    assert caught.value.status is JobSyncRunStatus.FAILED
    assert caught.value.error_code == "sync_lease_expired"
    assert scalar_count(db, RawJobRecord) == 0
    source = jobs.get_source(db, INTERN_SOURCE)
    assert source is not None
    assert source.active_sync_run_id == replacement_id
    assert source.sync_lease_expires_at is not None


def test_stale_finalization_preserves_committed_page_and_new_owner(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = FakeGateway(
        fields=intern_fields(),
        pages={0: TencentRecordPage([complete_record("r1")], 1, False, 0)},
    )
    replacement_id: str | None = None

    def lose_lease(
        session: Session,
        source_id: str,
        run_id: str,
        *,
        status: JobSyncRunStatus,
        now: datetime,
        error_code: str | None,
    ) -> JobSyncRun:
        del status, now, error_code
        nonlocal replacement_id
        session.rollback()
        replacement_id = _install_takeover(
            session, source_id=source_id, old_run_id=run_id
        ).id
        raise jobs.StaleSyncLeaseError(run_id)

    monkeypatch.setattr(jobs, "finish_sync_run", lose_lease)

    with pytest.raises(JobSyncFailedError) as caught:
        service(gateway).sync(db, source_key=INTERN_SOURCE, actor_user_id="admin")

    assert caught.value.status is JobSyncRunStatus.FAILED
    assert caught.value.error_code == "sync_lease_expired"
    assert scalar_count(db, RawJobRecord) == 1
    source = jobs.get_source(db, INTERN_SOURCE)
    assert source is not None
    assert source.active_sync_run_id == replacement_id
    assert source.sync_lease_expires_at is not None


def test_gateway_failure_after_session_detach_keeps_stable_error(db: Session) -> None:
    gateway = FakeGateway(fields=intern_fields(), pages={})

    def detach_then_fail(_file_id: str, _sheet_id: str) -> list[TencentField]:
        db.expunge_all()
        raise TencentTimeoutError("secret upstream detail")

    gateway.list_fields = detach_then_fail  # type: ignore[method-assign]

    with pytest.raises(JobSyncFailedError) as caught:
        service(gateway).sync(db, source_key=INTERN_SOURCE, actor_user_id="admin")

    assert caught.value.error_code == "tencent_timeout"
    assert caught.value.status is JobSyncRunStatus.FAILED


def test_cleanup_database_failure_does_not_mask_gateway_error(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = FakeGateway(fields=[], pages={})

    def fail_cleanup(*_args: object, **_kwargs: object) -> JobSyncRun:
        raise OperationalError("UPDATE job_sync_runs", {}, ConnectionError("offline"))

    monkeypatch.setattr(jobs, "finish_sync_run", fail_cleanup)

    with pytest.raises(JobSyncFailedError) as caught:
        service(gateway).sync(db, source_key=INTERN_SOURCE, actor_user_id="admin")

    assert caught.value.error_code == "source_schema_changed"
    assert caught.value.status is JobSyncRunStatus.FAILED
