from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session

from backend.app.db.base import Base, utc_now
from backend.app.db.models import (
    JobPosting,
    JobPostingStatus,
    JobSource,
    JobSourceProvider,
    JobVerification,
    RawJobRecord,
    User,
    UserRole,
)
from backend.app.services.job_review import (
    IncompleteJobError,
    InvalidJobReviewTransition,
    JobCompletionInput,
    JobNotFoundError,
    JobReviewService,
    StaleJobReviewError,
)


@pytest.fixture
def db() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture
def admin(db: Session) -> User:
    value = User(
        account="review-admin",
        nickname="Reviewer",
        password_hash="unused",
        role=UserRole.ADMIN,
    )
    db.add(value)
    db.flush()
    return value


@pytest.fixture
def pending_job(db: Session) -> JobPosting:
    source = JobSource(
        source_key="review-source",
        provider=JobSourceProvider.TENCENT_SMARTSHEET,
        name="Review Source",
        file_id="file",
        sheet_id="sheet",
        mapper_version="v1",
        enabled=True,
    )
    db.add(source)
    db.flush()
    raw = RawJobRecord(
        source_id=source.id,
        external_record_id="record-1",
        payload_hash="a" * 64,
        raw_fields=[],
        observed_at=utc_now(),
    )
    db.add(raw)
    db.flush()
    posting = JobPosting(
        source_id=source.id,
        external_record_id="record-1",
        raw_record_id=raw.id,
        status=JobPostingStatus.PENDING_COMPLETION,
        company_name="来源公司",
        title="来源岗位",
        locations=[],
        recruitment_types=[],
        industries=[],
        apply_url="https://example.com/source",
        mapper_version="v1",
        source_candidate={},
    )
    db.add(posting)
    db.flush()
    return posting


def completion_input(**overrides: object) -> JobCompletionInput:
    values: dict[str, object] = {
        "company_name": "示例科技",
        "title": "后端开发实习生",
        "description_text": "负责后端服务开发和测试。",
        "locations": ["上海"],
        "recruitment_types": ["实习"],
        "industries": ["软件"],
        "apply_url": "https://jobs.example.com/roles/1",
        "referral_code": None,
        "deadline_text": "2026-09-01",
    }
    values.update(overrides)
    return JobCompletionInput(**values)  # type: ignore[arg-type]


@pytest.fixture
def pending_review_job(pending_job: JobPosting, db: Session, admin: User) -> JobPosting:
    return JobReviewService().save_completion(
        db,
        job_id=pending_job.id,
        actor_user_id=admin.id,
        expected_version=0,
        values=completion_input(),
    )


def events_for(db: Session, job_id: str) -> list[JobVerification]:
    return list(
        db.scalars(
            select(JobVerification)
            .where(JobVerification.job_id == job_id)
            .order_by(JobVerification.review_version)
        )
    )


def timezone_agnostic(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=None) if value is not None else None


def test_save_completion_moves_job_to_pending_review(
    db: Session, pending_job: JobPosting, admin: User
) -> None:
    updated = JobReviewService().save_completion(
        db,
        job_id=pending_job.id,
        actor_user_id=admin.id,
        expected_version=0,
        values=completion_input(),
    )

    assert updated.status is JobPostingStatus.PENDING_REVIEW
    assert updated.review_version == 1
    assert db.scalar(select(func.count()).select_from(JobVerification)) == 1


def test_completion_then_verification_creates_exact_event_versions(
    db: Session, pending_review_job: JobPosting, admin: User
) -> None:
    verified = JobReviewService().verify(
        db,
        job_id=pending_review_job.id,
        actor_user_id=admin.id,
        expected_version=1,
        gui_eligible=True,
    )

    assert verified.review_version == 2
    assert [item.review_version for item in events_for(db, verified.id)] == [1, 2]


def test_verify_requires_complete_jd_and_valid_url(
    db: Session, pending_review_job: JobPosting, admin: User
) -> None:
    pending_review_job.description_text = None

    with pytest.raises(IncompleteJobError, match="required_fields"):
        JobReviewService().verify(
            db,
            job_id=pending_review_job.id,
            actor_user_id=admin.id,
            expected_version=pending_review_job.review_version,
            gui_eligible=True,
        )


def test_stale_review_version_is_rejected(
    db: Session, pending_job: JobPosting, admin: User
) -> None:
    with pytest.raises(StaleJobReviewError):
        JobReviewService().reject(
            db,
            job_id=pending_job.id,
            actor_user_id=admin.id,
            expected_version=9,
            reason_code="invalid_source",
        )


def test_missing_job_is_rejected(db: Session, admin: User) -> None:
    with pytest.raises(JobNotFoundError):
        JobReviewService().reject(
            db,
            job_id="missing-job",
            actor_user_id=admin.id,
            expected_version=0,
            reason_code="invalid_source",
        )


def test_email_application_can_be_verified_but_not_gui_eligible(
    db: Session, pending_review_job: JobPosting, admin: User
) -> None:
    pending_review_job.apply_url = "mailto:jobs@example.com"

    verified = JobReviewService().verify(
        db,
        job_id=pending_review_job.id,
        actor_user_id=admin.id,
        expected_version=pending_review_job.review_version,
        gui_eligible=False,
    )

    assert verified.status is JobPostingStatus.VERIFIED
    assert verified.gui_eligible is False


def test_email_application_is_not_gui_eligible(
    db: Session, pending_review_job: JobPosting, admin: User
) -> None:
    pending_review_job.apply_url = "mailto:jobs@example.com"

    with pytest.raises(IncompleteJobError, match="apply_url"):
        JobReviewService().verify(
            db,
            job_id=pending_review_job.id,
            actor_user_id=admin.id,
            expected_version=pending_review_job.review_version,
            gui_eligible=True,
        )


@pytest.mark.parametrize(
    "invalid_channel",
    [
        "https://exa mple.com/jobs",
        "https://example.com:not-a-port/jobs",
        "https://example.com:99999/jobs",
        "https:///missing-host",
        "https://[invalid/jobs",
        "mailto:",
        "mailto:not-an-address",
        "mailto:two@@example.com",
        "mailto:jobs@example.com subject",
    ],
)
def test_verify_rejects_malformed_application_channels_without_leaking_url_errors(
    invalid_channel: str,
    db: Session,
    pending_review_job: JobPosting,
    admin: User,
) -> None:
    pending_review_job.apply_url = invalid_channel

    with pytest.raises(IncompleteJobError, match="apply_url"):
        JobReviewService().verify(
            db,
            job_id=pending_review_job.id,
            actor_user_id=admin.id,
            expected_version=pending_review_job.review_version,
            gui_eligible=False,
        )


def test_save_completion_normalizes_all_values_and_event_snapshot(
    db: Session, pending_job: JobPosting, admin: User
) -> None:
    updated = JobReviewService().save_completion(
        db,
        job_id=pending_job.id,
        actor_user_id=admin.id,
        expected_version=0,
        values=completion_input(
            company_name="  示例科技  ",
            title="  后端开发实习生  ",
            description_text="  负责后端服务开发和测试。  ",
            locations=[" 上海 ", "", "上海", " 北京 ", "  "],
            recruitment_types=[" 实习 ", "实习", "校招"],
            industries=[" 软件 ", "软件", "人工智能"],
            apply_url="  https://jobs.example.com/roles/1  ",
            referral_code="   ",
            deadline_text="  2026-09-01  ",
        ),
    )

    assert updated.company_name == "示例科技"
    assert updated.title == "后端开发实习生"
    assert updated.description_text == "负责后端服务开发和测试。"
    assert updated.locations == ["上海", "北京"]
    assert updated.recruitment_types == ["实习", "校招"]
    assert updated.industries == ["软件", "人工智能"]
    assert updated.apply_url == "https://jobs.example.com/roles/1"
    assert updated.referral_code is None
    assert updated.deadline_text == "2026-09-01"
    assert events_for(db, updated.id)[0].field_snapshot["locations"] == ["上海", "北京"]


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("company_name", "   "),
        ("title", ""),
        ("description_text", " \t "),
        ("apply_url", "not-a-url"),
    ],
)
def test_save_completion_rejects_incomplete_required_values(
    field_name: str,
    invalid_value: str,
    db: Session,
    pending_job: JobPosting,
    admin: User,
) -> None:
    with pytest.raises(IncompleteJobError, match=field_name):
        JobReviewService().save_completion(
            db,
            job_id=pending_job.id,
            actor_user_id=admin.id,
            expected_version=0,
            values=completion_input(**{field_name: invalid_value}),
        )


def test_reject_trims_reason_and_rejects_empty_reason(
    db: Session, pending_job: JobPosting, admin: User
) -> None:
    rejected = JobReviewService().reject(
        db,
        job_id=pending_job.id,
        actor_user_id=admin.id,
        expected_version=0,
        reason_code="  invalid_source  ",
    )
    assert events_for(db, rejected.id)[0].reason_code == "invalid_source"

    second_job = JobPosting(
        source_id=pending_job.source_id,
        external_record_id="record-2",
        raw_record_id=pending_job.raw_record_id,
        status=JobPostingStatus.PENDING_COMPLETION,
        company_name="来源公司",
        title="来源岗位",
        locations=[],
        recruitment_types=[],
        industries=[],
        apply_url="https://example.com/source",
        mapper_version="v1",
        source_candidate={},
    )
    db.add(second_job)
    db.flush()
    with pytest.raises(IncompleteJobError, match="reason_code"):
        JobReviewService().reject(
            db,
            job_id=second_job.id,
            actor_user_id=admin.id,
            expected_version=0,
            reason_code="   ",
        )


def test_expire_rejects_empty_reason(
    db: Session, pending_review_job: JobPosting, admin: User
) -> None:
    verified = JobReviewService().verify(
        db,
        job_id=pending_review_job.id,
        actor_user_id=admin.id,
        expected_version=1,
        gui_eligible=True,
    )

    with pytest.raises(IncompleteJobError, match="reason_code"):
        JobReviewService().expire(
            db,
            job_id=verified.id,
            actor_user_id=admin.id,
            expected_version=2,
            reason_code="  ",
        )


def test_expire_only_accepts_verified_job(
    db: Session, pending_job: JobPosting, admin: User
) -> None:
    with pytest.raises(InvalidJobReviewTransition):
        JobReviewService().expire(
            db,
            job_id=pending_job.id,
            actor_user_id=admin.id,
            expected_version=pending_job.review_version,
            reason_code="closed_on_official_site",
        )


def test_terminal_timestamps_and_gui_flags_follow_status_invariants(
    db: Session, pending_job: JobPosting, admin: User
) -> None:
    timestamps = iter(
        [
            datetime(2026, 7, 16, 1, tzinfo=timezone.utc),
            datetime(2026, 7, 16, 2, tzinfo=timezone.utc),
            datetime(2026, 7, 16, 3, tzinfo=timezone.utc),
            datetime(2026, 7, 16, 4, tzinfo=timezone.utc),
            datetime(2026, 7, 16, 5, tzinfo=timezone.utc),
        ]
    )
    calls: list[datetime] = []

    def now() -> datetime:
        value = next(timestamps)
        calls.append(value)
        return value

    service = JobReviewService(now=now)
    review = service.save_completion(
        db,
        job_id=pending_job.id,
        actor_user_id=admin.id,
        expected_version=0,
        values=completion_input(),
    )
    assert (review.verified_at, review.rejected_at, review.expired_at) == (
        None,
        None,
        None,
    )
    assert review.gui_eligible is False

    verified = service.verify(
        db,
        job_id=review.id,
        actor_user_id=admin.id,
        expected_version=1,
        gui_eligible=True,
    )
    assert verified.verified_at == calls[1]
    assert verified.rejected_at is None
    assert verified.expired_at is None
    assert verified.gui_eligible is True

    expired = service.expire(
        db,
        job_id=verified.id,
        actor_user_id=admin.id,
        expected_version=2,
        reason_code="  closed  ",
    )
    assert timezone_agnostic(expired.verified_at) == timezone_agnostic(calls[1])
    assert timezone_agnostic(expired.expired_at) == timezone_agnostic(calls[2])
    assert expired.rejected_at is None
    assert expired.gui_eligible is False
    assert events_for(db, expired.id)[-1].reason_code == "closed"
    assert len(calls) == 3
    assert [
        timezone_agnostic(item.created_at) for item in events_for(db, expired.id)
    ] == [timezone_agnostic(item) for item in calls]


def test_rejected_job_can_be_completed_and_clears_rejection_state(
    db: Session, pending_job: JobPosting, admin: User
) -> None:
    service = JobReviewService()
    rejected = service.reject(
        db,
        job_id=pending_job.id,
        actor_user_id=admin.id,
        expected_version=0,
        reason_code="invalid_source",
    )
    assert rejected.rejected_at is not None
    assert rejected.gui_eligible is False

    review = service.save_completion(
        db,
        job_id=rejected.id,
        actor_user_id=admin.id,
        expected_version=1,
        values=completion_input(),
    )

    assert review.status is JobPostingStatus.PENDING_REVIEW
    assert review.rejected_at is None
    assert review.verified_at is None
    assert review.expired_at is None
    assert review.gui_eligible is False


def test_verification_flush_failure_rolls_back_posting_version_and_event(
    db: Session, pending_review_job: JobPosting, admin: User
) -> None:
    job_id = pending_review_job.id
    db.commit()

    def fail_verification_event_flush(
        session: Session,
        _flush_context: object,
        _instances: object,
    ) -> None:
        if any(
            isinstance(item, JobVerification) and item.action == "verified"
            for item in session.new
        ):
            raise RuntimeError("injected verification event flush failure")

    event.listen(db, "before_flush", fail_verification_event_flush)
    try:
        with pytest.raises(RuntimeError, match="injected verification event"):
            JobReviewService().verify(
                db,
                job_id=job_id,
                actor_user_id=admin.id,
                expected_version=1,
                gui_eligible=True,
            )
    finally:
        event.remove(db, "before_flush", fail_verification_event_flush)
        db.rollback()

    posting = db.get(JobPosting, job_id)
    assert posting is not None
    assert posting.status is JobPostingStatus.PENDING_REVIEW
    assert posting.review_version == 1
    assert posting.verified_at is None
    assert [item.review_version for item in events_for(db, job_id)] == [1]


def test_service_never_commits_caller_transaction(
    db: Session,
    pending_job: JobPosting,
    admin: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_commit() -> None:
        raise AssertionError("service must not commit")

    monkeypatch.setattr(db, "commit", unexpected_commit)

    JobReviewService().save_completion(
        db,
        job_id=pending_job.id,
        actor_user_id=admin.id,
        expected_version=0,
        values=completion_input(),
    )


def test_every_transition_requests_the_authoritative_row_lock(
    db: Session,
    pending_job: JobPosting,
    admin: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.repositories import jobs

    actual = jobs.get_posting_for_review
    lock_values: list[bool] = []

    def tracked_get(
        session: Session, job_id: str, *, lock: bool = False
    ) -> tuple[JobPosting, JobSource] | None:
        lock_values.append(lock)
        return actual(session, job_id, lock=lock)

    monkeypatch.setattr(jobs, "get_posting_for_review", tracked_get)
    service = JobReviewService()
    review = service.save_completion(
        db,
        job_id=pending_job.id,
        actor_user_id=admin.id,
        expected_version=0,
        values=completion_input(),
    )
    verified = service.verify(
        db,
        job_id=review.id,
        actor_user_id=admin.id,
        expected_version=1,
        gui_eligible=True,
    )
    service.expire(
        db,
        job_id=verified.id,
        actor_user_id=admin.id,
        expected_version=2,
        reason_code="closed",
    )

    assert lock_values == [True, True, True]
