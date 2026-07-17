from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db.base import Base
from backend.app.db.models import (
    JobFeedbackEvent,
    JobPosting,
    JobPostingStatus,
    JobSource,
    JobSourceProvider,
    RawJobRecord,
    User,
    UserRole,
)
from backend.app.domain.job_feedback import (
    FeedbackAdminDecision,
    FeedbackStudentAction,
    JobFeedbackCategory,
)
from backend.app.services.job_feedback import (
    IdempotencyKeyConflictError,
    JobFeedbackService,
    StaleFeedbackError,
)


NOW = datetime(2026, 7, 17, tzinfo=timezone.utc)


@pytest.fixture
def seeded_db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    source = JobSource(
        source_key="feedback-service", provider=JobSourceProvider.TENCENT_SMARTSHEET,
        name="source", file_id="file", sheet_id="sheet", mapper_version="v1",
        enabled=True,
    )
    session.add(source)
    session.flush()
    raw = RawJobRecord(
        source_id=source.id, external_record_id="row", payload_hash="a" * 64,
        raw_fields=[],
    )
    session.add(raw)
    session.flush()
    job = JobPosting(
        source_id=source.id, external_record_id="row", raw_record_id=raw.id,
        status=JobPostingStatus.VERIFIED, company_name="Company", title="Role",
        locations=[], recruitment_types=[], industries=[], apply_url="https://example.com",
        mapper_version="v1", source_candidate={},
    )
    student = User(
        account="student", nickname="Student", password_hash="hash", role=UserRole.STUDENT,
    )
    admin = User(
        account="admin", nickname="Admin", password_hash="hash", role=UserRole.ADMIN,
    )
    session.add_all([job, student, admin])
    session.commit()
    yield SimpleNamespace(session=session, job=job, student=student, admin=admin)
    session.close()
    engine.dispose()


def _create(seeded_db, key: str = "student-feedback-0001"):
    return JobFeedbackService(now=lambda: NOW).mutate_student(
        seeded_db.session,
        job_id=seeded_db.job.id,
        actor_user_id=seeded_db.student.id,
        idempotency_key=key,
        action=FeedbackStudentAction.UPSERT,
        category=JobFeedbackCategory.CLOSED,
        expected_version=None,
        note=" 官网显示已关闭 ",
    )


def test_same_request_replays_and_leaves_one_event(seeded_db) -> None:
    first = _create(seeded_db)
    seeded_db.session.commit()
    second = _create(seeded_db)
    seeded_db.session.commit()
    assert second == first
    assert seeded_db.session.scalar(select(func.count(JobFeedbackEvent.id))) == 1
    event = seeded_db.session.scalar(select(JobFeedbackEvent))
    assert event is not None
    snapshot = str(event.redacted_snapshot)
    assert "官网显示已关闭" not in snapshot
    assert seeded_db.student.id not in snapshot
    assert "student-feedback-0001" not in snapshot


def test_same_key_different_body_conflicts_without_key_in_message(seeded_db) -> None:
    _create(seeded_db, "student-feedback-0002")
    seeded_db.session.commit()
    with pytest.raises(IdempotencyKeyConflictError) as caught:
        JobFeedbackService(now=lambda: NOW).mutate_student(
            seeded_db.session, job_id=seeded_db.job.id,
            actor_user_id=seeded_db.student.id,
            idempotency_key="student-feedback-0002",
            action=FeedbackStudentAction.UPSERT,
            category=JobFeedbackCategory.CLOSED,
            expected_version=1, note="different",
        )
    assert "student-feedback-0002" not in str(caught.value)


def test_update_withdraw_and_stale_version(seeded_db) -> None:
    created = _create(seeded_db)
    seeded_db.session.commit()
    with pytest.raises(StaleFeedbackError):
        JobFeedbackService(now=lambda: NOW).mutate_student(
            seeded_db.session, job_id=seeded_db.job.id,
            actor_user_id=seeded_db.student.id,
            idempotency_key="student-feedback-stale",
            action=FeedbackStudentAction.UPSERT,
            category=JobFeedbackCategory.CLOSED,
            expected_version=0, note=None,
        )
    withdrawn = JobFeedbackService(now=lambda: NOW).mutate_student(
        seeded_db.session, job_id=seeded_db.job.id,
        actor_user_id=seeded_db.student.id,
        idempotency_key="student-feedback-withdraw",
        action=FeedbackStudentAction.WITHDRAW,
        category=JobFeedbackCategory.CLOSED,
        expected_version=created.version, note=None,
    )
    assert withdrawn.status.value == "withdrawn"
    assert withdrawn.version == 2


def test_admin_decision_does_not_change_job(seeded_db) -> None:
    created = _create(seeded_db)
    seeded_db.session.commit()
    before = seeded_db.job.review_version
    decided = JobFeedbackService(now=lambda: NOW).decide_admin(
        seeded_db.session, feedback_id=created.id,
        actor_user_id=seeded_db.admin.id,
        idempotency_key="admin-feedback-0001",
        decision=FeedbackAdminDecision.RESOLVE,
        expected_version=created.version,
    )
    seeded_db.session.commit()
    seeded_db.session.refresh(seeded_db.job)
    assert decided.status.value == "resolved"
    assert seeded_db.job.status is JobPostingStatus.VERIFIED
    assert seeded_db.job.review_version == before
