from collections.abc import Iterator
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from backend.app.db.base import Base
from backend.app.db.models import (
    AuditEvent, DeduplicationStatus, JobPosting, JobPostingStatus, JobSource,
    JobSourceLink, JobSourceProvider, JobVerification, RawJobRecord,
    SubmissionStatus, User, UserRole,
)
from backend.app.repositories.job_submissions import MANUAL_SOURCE_ID
from backend.app.repositories import job_submissions
from backend.app.services.job_submissions import (
    InvalidPromotionTarget,
    InvalidSubmissionTransition,
    JobSubmissionService,
    StaleSubmissionError,
)


@pytest.fixture
def service_db() -> Iterator[tuple[JobSubmissionService, Session, User, User]]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        user = User(account="service-owner", nickname="Owner", password_hash="hash")
        admin = User(
            account="service-admin", nickname="Admin", password_hash="hash",
            role=UserRole.ADMIN,
        )
        manual_source = JobSource(
            id=MANUAL_SOURCE_ID, source_key="manual-user-submissions",
            provider=JobSourceProvider.USER_SUBMISSION, name="用户手动提交",
            file_id="manual", sheet_id="manual", mapper_version="manual-submission-v1",
            enabled=False,
        )
        db.add_all([user, admin, manual_source])
        db.flush()
        yield JobSubmissionService(), db, user, admin
    engine.dispose()


@pytest.fixture
def verified_job(
    service_db: tuple[JobSubmissionService, Session, User, User],
) -> JobPosting:
    _service, db, _user, _admin = service_db
    now = datetime.now(timezone.utc)
    source = JobSource(
        source_key="service-tencent", provider=JobSourceProvider.TENCENT_SMARTSHEET,
        name="Service Tencent", file_id="service-file", sheet_id="service-sheet",
        mapper_version="service-v1", enabled=True,
    )
    db.add(source)
    db.flush()
    raw = RawJobRecord(
        source_id=source.id, external_record_id="service-record",
        payload_hash="a" * 64, raw_fields=[], observed_at=now,
    )
    db.add(raw)
    db.flush()
    posting = JobPosting(
        source_id=source.id, external_record_id=raw.external_record_id,
        raw_record_id=raw.id, status=JobPostingStatus.VERIFIED,
        company_name="示例科技", title="后端实习生",
        description_text="负责 FastAPI MySQL 后端服务开发",
        locations=["上海"], recruitment_types=["实习"], industries=["软件"],
        apply_url="https://jobs.example.com/service-role",
        mapper_version=source.mapper_version, source_candidate={}, verified_at=now,
    )
    db.add(posting)
    db.flush()
    return posting


def test_student_create_update_and_submit_are_versioned(service_db) -> None:
    service, db, user, _admin = service_db
    item = service.create(
        db, user_id=user.id, input_type="url", raw_value="https://jobs.example.com/1"
    )
    assert (item.status, item.version, item.deduplication_status) == (
        SubmissionStatus.DRAFT, 0, DeduplicationStatus.SUCCEEDED,
    )
    updated = service.update(
        db, user_id=user.id, submission_id=item.id, expected_version=0,
        input_type="jd_text", raw_value="负责 Python FastAPI MySQL 后端服务开发",
    )
    assert updated.version == 1
    submitted = service.submit(
        db, user_id=user.id, submission_id=item.id, expected_version=1
    )
    assert (submitted.status, submitted.version) == (SubmissionStatus.SUBMITTED, 2)
    with pytest.raises(InvalidSubmissionTransition):
        service.update(
            db, user_id=user.id, submission_id=item.id, expected_version=2,
            input_type="url", raw_value="https://jobs.example.com/2",
        )


def test_stale_update_does_not_mutate_submission(service_db) -> None:
    service, db, user, _admin = service_db
    item = service.create(db, user_id=user.id, input_type="url", raw_value="https://jobs.example.com/1")
    with pytest.raises(StaleSubmissionError):
        service.update(
            db, user_id=user.id, submission_id=item.id, expected_version=9,
            input_type="url", raw_value="https://jobs.example.com/2",
        )
    assert item.version == 0


def test_student_update_and_submit_lock_the_owned_submission(service_db) -> None:
    service, db, user, _admin = service_db
    item = service.create(
        db, user_id=user.id, input_type="url", raw_value="https://jobs.example.com/1"
    )

    with patch.object(job_submissions, "get_owned", wraps=job_submissions.get_owned) as get_owned:
        service.update(
            db,
            user_id=user.id,
            submission_id=item.id,
            expected_version=0,
            input_type="url",
            raw_value="https://jobs.example.com/2",
        )
    assert get_owned.call_args.kwargs["lock"] is True

    with patch.object(job_submissions, "get_owned", wraps=job_submissions.get_owned) as get_owned:
        service.submit(db, user_id=user.id, submission_id=item.id, expected_version=1)
    assert get_owned.call_args.kwargs["lock"] is True


def test_duplicate_detection_failure_keeps_private_submission_editable(service_db) -> None:
    service, db, user, _admin = service_db
    with patch(
        "backend.app.repositories.job_submissions.list_job_fingerprints",
        side_effect=OperationalError("select", {}, RuntimeError("database detail")),
    ):
        item = service.create(
            db, user_id=user.id, input_type="url", raw_value="https://jobs.example.com/1"
        )
    assert item.status is SubmissionStatus.DRAFT
    assert item.deduplication_status is DeduplicationStatus.FAILED
    assert item.deduplication_error_code == "duplicate_detection_failed"
    assert "database detail" not in item.deduplication_error_code


def test_admin_link_existing_appends_source_and_safe_audit(service_db, verified_job) -> None:
    service, db, user, admin = service_db
    item = service.create(db, user_id=user.id, input_type="url", raw_value=verified_job.apply_url)
    service.submit(db, user_id=user.id, submission_id=item.id, expected_version=0)
    promoted = service.link_existing(
        db, submission_id=item.id, actor_user_id=admin.id,
        expected_version=1, job_id=verified_job.id,
    )
    assert promoted.status is SubmissionStatus.PROMOTED
    assert promoted.promoted_job_id == verified_job.id
    assert db.scalar(select(JobSourceLink).where(JobSourceLink.submission_id == item.id)) is not None
    event = db.scalars(select(AuditEvent).order_by(AuditEvent.id.desc())).first()
    assert event.redacted_payload == {"action": "link_existing", "job_id": verified_job.id}
    assert user.id not in str(event.redacted_payload)


def test_admin_create_pending_does_not_create_verification(service_db) -> None:
    service, db, user, admin = service_db
    item = service.create(
        db, user_id=user.id, input_type="jd_text",
        raw_value="示例科技招聘后端实习生，负责 FastAPI 与 MySQL。",
    )
    service.submit(db, user_id=user.id, submission_id=item.id, expected_version=0)
    promoted, posting = service.create_pending(
        db, submission_id=item.id, actor_user_id=admin.id, expected_version=1,
        company_name="示例科技", title="后端实习生", apply_url="",
    )
    assert posting.status is JobPostingStatus.PENDING_COMPLETION
    assert promoted.promoted_job_id == posting.id
    assert db.scalar(select(JobVerification).where(JobVerification.job_id == posting.id)) is None


def test_admin_create_pending_rejects_blank_normalized_names(service_db) -> None:
    service, db, user, admin = service_db
    item = service.create(
        db,
        user_id=user.id,
        input_type="jd_text",
        raw_value="示例科技招聘后端实习生，负责 FastAPI 与 MySQL。",
    )
    service.submit(db, user_id=user.id, submission_id=item.id, expected_version=0)

    with pytest.raises(InvalidPromotionTarget):
        service.create_pending(
            db,
            submission_id=item.id,
            actor_user_id=admin.id,
            expected_version=1,
            company_name="   ",
            title="后端实习生",
            apply_url="",
        )

    assert item.status is SubmissionStatus.SUBMITTED


def test_second_admin_decision_is_stale_and_has_no_second_side_effect(service_db, verified_job) -> None:
    service, db, user, admin = service_db
    item = service.create(db, user_id=user.id, input_type="url", raw_value=verified_job.apply_url)
    service.submit(db, user_id=user.id, submission_id=item.id, expected_version=0)
    service.link_existing(
        db, submission_id=item.id, actor_user_id=admin.id,
        expected_version=1, job_id=verified_job.id,
    )
    with pytest.raises(StaleSubmissionError):
        service.link_existing(
            db, submission_id=item.id, actor_user_id=admin.id,
            expected_version=1, job_id=verified_job.id,
        )
    assert db.scalar(select(func.count()).select_from(JobSourceLink).where(
        JobSourceLink.submission_id == item.id
    )) == 1
