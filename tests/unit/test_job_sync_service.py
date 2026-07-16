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
    JobPostingStatus,
    JobSourceLink,
    JobSourceLinkType,
    JobSyncRun,
    JobSyncRunStatus,
    JobVerification,
    RawJobRecord,
    User,
    UserRole,
)
from backend.app.db.models import DeduplicationStatus, SubmissionInputType, SubmissionStatus, UserJobSubmission
from backend.app.repositories import jobs
from backend.app.services import job_sync
from backend.app.services.job_review import (
    JobCompletionInput,
    JobReviewService,
    StaleJobReviewError,
)
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


def test_sync_preserves_reviewed_canonical_fields_when_source_changes(
    db: Session,
) -> None:
    gateway = FakeGateway(
        fields=intern_fields(),
        pages={0: TencentRecordPage([complete_record("r1")], 1, False, 0)},
    )
    sync = service(gateway)
    sync.sync(db, source_key=INTERN_SOURCE, actor_user_id="admin")
    posting = db.scalar(select(JobPosting))
    assert posting is not None
    posting.status = JobPostingStatus.PENDING_REVIEW
    posting.title = "人工确认岗位"
    posting.description_text = "人工补全的完整 JD"
    posting.review_version = 1
    db.commit()
    gateway.pages = {
        0: TencentRecordPage([complete_record("r1", title="来源新岗位")], 1, False, 0)
    }

    outcome = sync.sync(db, source_key=INTERN_SOURCE, actor_user_id="admin")
    db.refresh(posting)

    assert outcome.postings_updated == 1
    assert posting.title == "人工确认岗位"
    assert posting.description_text == "人工补全的完整 JD"
    assert posting.source_candidate["title"] == "来源新岗位"
    assert posting.source_changed_since_review is True


def test_source_change_invalidates_loaded_admin_completion(db: Session) -> None:
    gateway = FakeGateway(
        fields=intern_fields(),
        pages={0: TencentRecordPage([complete_record("r1")], 1, False, 0)},
    )
    sync = service(gateway)
    sync.sync(db, source_key=INTERN_SOURCE, actor_user_id="admin")
    posting = db.scalar(select(JobPosting))
    assert posting is not None
    review = JobReviewService(now=lambda: NOW)
    reviewed = review.save_completion(
        db,
        job_id=posting.id,
        actor_user_id="admin",
        expected_version=0,
        values=JobCompletionInput(
            company_name="人工确认公司",
            title="人工确认岗位",
            description_text="人工补全的完整 JD",
            locations=["上海"],
            recruitment_types=["实习"],
            industries=["软件"],
            apply_url="https://example.com/reviewed",
            referral_code=None,
            deadline_text=None,
        ),
    )
    stale_version = reviewed.review_version
    assert stale_version == 1
    db.commit()

    unchanged = sync.sync(db, source_key=INTERN_SOURCE, actor_user_id="admin")
    db.refresh(posting)
    assert unchanged.postings_updated == 0
    assert posting.review_version == stale_version

    gateway.pages = {
        0: TencentRecordPage([complete_record("r1", title="来源新岗位")], 1, False, 0)
    }
    changed = sync.sync(db, source_key=INTERN_SOURCE, actor_user_id="admin")
    db.refresh(posting)

    assert changed.postings_updated == 1
    assert posting.review_version == stale_version + 1
    assert posting.title == "人工确认岗位"
    assert posting.source_changed_since_review is True

    repeated = sync.sync(db, source_key=INTERN_SOURCE, actor_user_id="admin")
    db.refresh(posting)
    assert repeated.postings_updated == 0
    assert posting.review_version == stale_version + 1

    with pytest.raises(StaleJobReviewError):
        review.save_completion(
            db,
            job_id=posting.id,
            actor_user_id="admin",
            expected_version=stale_version,
            values=JobCompletionInput(
                company_name="旧表单公司",
                title="旧表单覆盖岗位",
                description_text="旧表单覆盖 JD",
                locations=["北京"],
                recruitment_types=["校招"],
                industries=["旧行业"],
                apply_url="https://example.com/stale",
                referral_code=None,
                deadline_text=None,
            ),
        )
    assert posting.title == "人工确认岗位"
    assert posting.description_text == "人工补全的完整 JD"
    assert posting.source_changed_since_review is True


def test_pending_source_change_invalidates_loaded_version_and_refreshes_canonical(
    db: Session,
) -> None:
    gateway = FakeGateway(
        fields=intern_fields(),
        pages={0: TencentRecordPage([complete_record("pending-r1")], 1, False, 0)},
    )
    sync = service(gateway)
    sync.sync(db, source_key=INTERN_SOURCE, actor_user_id="admin")
    posting = db.scalar(
        select(JobPosting).where(JobPosting.external_record_id == "pending-r1")
    )
    assert posting is not None
    loaded_version = posting.review_version
    assert loaded_version == 0

    gateway.pages = {
        0: TencentRecordPage(
            [complete_record("pending-r1", title="来源更新岗位")], 1, False, 0
        )
    }
    sync.sync(db, source_key=INTERN_SOURCE, actor_user_id="admin")
    db.refresh(posting)

    assert posting.title == "来源更新岗位"
    assert posting.source_candidate["title"] == "来源更新岗位"
    assert posting.review_version == loaded_version + 1
    assert posting.source_changed_since_review is False
    with pytest.raises(StaleJobReviewError):
        JobReviewService(now=lambda: NOW).save_completion(
            db,
            job_id=posting.id,
            actor_user_id="admin",
            expected_version=loaded_version,
            values=JobCompletionInput(
                company_name="旧表单公司",
                title="旧表单岗位",
                description_text="旧表单 JD",
                locations=["上海"],
                recruitment_types=["实习"],
                industries=["软件"],
                apply_url="https://example.com/stale-pending",
                referral_code=None,
                deadline_text=None,
            ),
        )


def test_review_event_protects_canonical_fields_after_status_and_version_reset(
    db: Session,
) -> None:
    gateway = FakeGateway(
        fields=intern_fields(),
        pages={0: TencentRecordPage([complete_record("reset-r1")], 1, False, 0)},
    )
    sync = service(gateway)
    sync.sync(db, source_key=INTERN_SOURCE, actor_user_id="admin")
    posting = db.scalar(
        select(JobPosting).where(JobPosting.external_record_id == "reset-r1")
    )
    assert posting is not None
    review = JobReviewService(now=lambda: NOW).save_completion(
        db,
        job_id=posting.id,
        actor_user_id="admin",
        expected_version=0,
        values=JobCompletionInput(
            company_name="人工确认公司",
            title="人工确认岗位",
            description_text="人工确认 JD",
            locations=["上海"],
            recruitment_types=["实习"],
            industries=["软件"],
            apply_url="https://example.com/reviewed-reset",
            referral_code="HUMAN",
            deadline_text="2026-12-31",
        ),
    )
    assert db.scalar(
        select(func.count())
        .select_from(JobVerification)
        .where(JobVerification.job_id == posting.id)
    ) == 1
    review.status = JobPostingStatus.PENDING_COMPLETION
    review.review_version = 0
    db.commit()

    gateway.pages = {
        0: TencentRecordPage(
            [complete_record("reset-r1", title="来源不得覆盖岗位")], 1, False, 0
        )
    }
    sync.sync(db, source_key=INTERN_SOURCE, actor_user_id="admin")
    db.refresh(posting)

    assert posting.title == "人工确认岗位"
    assert posting.description_text == "人工确认 JD"
    assert posting.source_candidate["title"] == "来源不得覆盖岗位"
    assert posting.source_changed_since_review is True
    assert posting.review_version == 1


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


def test_unexpected_gateway_error_is_sanitized_and_finalizes_owned_run(
    db: Session,
) -> None:
    gateway = FakeGateway(fields=intern_fields(), pages={})

    def fail_unexpectedly(_file_id: str, _sheet_id: str) -> list[TencentField]:
        raise RuntimeError("secret unexpected upstream detail")

    gateway.list_fields = fail_unexpectedly  # type: ignore[method-assign]

    with pytest.raises(JobSyncFailedError) as caught:
        service(gateway).sync(db, source_key=INTERN_SOURCE, actor_user_id="admin")

    assert caught.value.args == ("job_sync_unexpected_error",)
    assert caught.value.__cause__ is None
    assert caught.value.error_code == "job_sync_unexpected_error"
    assert caught.value.status is JobSyncRunStatus.FAILED
    run = db.get(JobSyncRun, caught.value.run_id)
    assert run is not None
    assert run.status is JobSyncRunStatus.FAILED
    assert run.error_code == "job_sync_unexpected_error"
    source = jobs.get_source(db, INTERN_SOURCE)
    assert source is not None
    assert source.active_sync_run_id is None
    assert source.sync_lease_expires_at is None
    finished = db.scalar(
        select(AuditEvent).where(AuditEvent.event_type == "job_sync.finished")
    )
    assert finished is not None
    assert finished.redacted_payload["error_code"] == "job_sync_unexpected_error"
    assert "secret unexpected upstream detail" not in repr(finished.redacted_payload)


def test_unexpected_error_after_committed_page_remains_partial(db: Session) -> None:
    gateway = FakeGateway(
        fields=intern_fields(),
        pages={0: TencentRecordPage([complete_record("r1")], 2, True, 1)},
    )

    def fail_second_page(
        _file_id: str, _sheet_id: str, *, offset: int, limit: int
    ) -> TencentRecordPage:
        if offset == 0:
            return gateway.pages[0]
        raise ValueError("secret mapper-adjacent detail")

    gateway.list_records = fail_second_page  # type: ignore[method-assign]

    with pytest.raises(JobSyncFailedError) as caught:
        service(gateway).sync(db, source_key=INTERN_SOURCE, actor_user_id="admin")

    assert caught.value.error_code == "job_sync_unexpected_error"
    assert caught.value.status is JobSyncRunStatus.PARTIAL
    assert scalar_count(db, JobPosting) == 1


def test_unexpected_cleanup_error_does_not_mask_sanitized_failure(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = FakeGateway(fields=intern_fields(), pages={})
    gateway.list_fields = lambda *_: (_ for _ in ()).throw(  # type: ignore[method-assign]
        RuntimeError("secret original detail")
    )

    def fail_cleanup(*_args: object, **_kwargs: object) -> JobSyncRun:
        raise RuntimeError("secret cleanup detail")

    monkeypatch.setattr(jobs, "finish_sync_run", fail_cleanup)

    with pytest.raises(JobSyncFailedError) as caught:
        service(gateway).sync(db, source_key=INTERN_SOURCE, actor_user_id="admin")

    assert caught.value.args == ("job_sync_unexpected_error",)
    assert caught.value.__cause__ is None


@pytest.mark.parametrize("signal", [KeyboardInterrupt(), SystemExit()])
def test_process_control_exceptions_are_not_wrapped(
    db: Session, signal: BaseException
) -> None:
    gateway = FakeGateway(fields=intern_fields(), pages={})

    def interrupt(_file_id: str, _sheet_id: str) -> list[TencentField]:
        raise signal

    gateway.list_fields = interrupt  # type: ignore[method-assign]

    with pytest.raises(type(signal)) as caught:
        service(gateway).sync(db, source_key=INTERN_SOURCE, actor_user_id="admin")

    assert caught.value is signal


def seeded_private_submission(db: Session) -> UserJobSubmission:
    owner = db.scalar(select(User).where(User.account == "admin"))
    assert owner is not None
    item = UserJobSubmission(
        user_id=owner.id, input_type=SubmissionInputType.URL,
        original_url="https://example.com/jobs", original_jd=None,
        input_preview="https://example.com/jobs",
        normalized_url="https://example.com/jobs",
        content_sha256="a" * 64, status=SubmissionStatus.DRAFT, version=0,
        deduplication_status=DeduplicationStatus.PENDING,
    )
    db.add(item)
    db.flush()
    return item


def test_tencent_resync_preserves_manual_source_link(db: Session) -> None:
    gateway = FakeGateway(
        fields=intern_fields(),
        pages={0: TencentRecordPage([complete_record("r1")], 1, False, 0)},
    )
    _outcome = service(gateway).sync(db, source_key=INTERN_SOURCE, actor_user_id="admin")
    posting = db.scalar(select(JobPosting))
    assert posting is not None
    manual = seeded_private_submission(db)
    db.add(JobSourceLink(
        job_id=posting.id, source_type=JobSourceLinkType.USER_SUBMISSION,
        source_id=None, submission_id=manual.id,
        source_record_ref=manual.id, normalized_url=manual.normalized_url,
    ))
    db.commit()
    service(gateway).sync(db, source_key=INTERN_SOURCE, actor_user_id="admin")
    links = db.scalars(select(JobSourceLink).where(JobSourceLink.job_id == posting.id)).all()
    assert {link.source_type for link in links} == {
        JobSourceLinkType.TENCENT_SMARTSHEET,
        JobSourceLinkType.USER_SUBMISSION,
    }
