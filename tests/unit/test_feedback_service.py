from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.app.db.base import Base
from backend.app.db.models import JobFeedback, JobFeedbackCategory, User, UserRole
from backend.app.services.feedbacks import (
    FeedbackService,
    IdempotentFeedbackError,
    JobFeedbackNotFoundError,
)


@pytest.fixture
def db_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _student(db: Session) -> User:
    user = User(account="student1", nickname="Student", password_hash="hash", role=UserRole.STUDENT)
    db.add(user)
    db.flush()
    return user


def _admin(db: Session) -> User:
    user = User(account="admin1", nickname="Admin", password_hash="hash", role=UserRole.ADMIN)
    db.add(user)
    db.flush()
    return user


class TestFeedbackService:
    def test_create_feedback_for_student(self, db_session: Session) -> None:
        service = FeedbackService(rate_limiter=None)
        user = _student(db_session)
        item = service.create_feedback(
            db_session, job_id="job-001", user=user,
            category=JobFeedbackCategory.CLOSED,
            note="Test feedback", idempotency_key="idem-student-1" + "x" * 37,
        )
        assert item.job_id == "job-001"
        assert item.user_id == user.id
        assert item.category == JobFeedbackCategory.CLOSED

    def test_create_feedback_for_admin(self, db_session: Session) -> None:
        service = FeedbackService(rate_limiter=None)
        admin = _admin(db_session)
        item = service.create_feedback(
            db_session, job_id="job-002", user=admin,
            category=JobFeedbackCategory.APPLICATION_CHANNEL_UNAVAILABLE,
            note=None, idempotency_key="idem-admin-1" + "x" * 38,
        )
        assert item.job_id == "job-002"
        assert item.user_id == admin.id

    def test_idempotency_key_prevents_duplicates(self, db_session: Session) -> None:
        service = FeedbackService(rate_limiter=None)
        user = _student(db_session)
        key = "idem-unique-" + "z" * 39
        service.create_feedback(
            db_session, job_id="job-003", user=user,
            category=JobFeedbackCategory.CONTENT_CHANGED,
            note="first", idempotency_key=key,
        )
        with pytest.raises(IdempotentFeedbackError):
            service.create_feedback(
                db_session, job_id="job-003", user=user,
                category=JobFeedbackCategory.CONTENT_CHANGED,
                note="duplicate", idempotency_key=key,
            )

    def test_get_feedback_not_found(self, db_session: Session) -> None:
        service = FeedbackService(rate_limiter=None)
        with pytest.raises(JobFeedbackNotFoundError):
            service.get_feedback(db_session, feedback_id="nonexistent-id")

    def test_get_feedback_success(self, db_session: Session) -> None:
        service = FeedbackService(rate_limiter=None)
        user = _student(db_session)
        created = service.create_feedback(
            db_session, job_id="job-004", user=user,
            category=JobFeedbackCategory.INCORRECT_INFORMATION,
            note="Wrong info", idempotency_key="idem-get-" + "a" * 43,
        )
        db_session.flush()
        fetched = service.get_feedback(db_session, feedback_id=created.id)
        assert fetched.id == created.id
        assert fetched.note == "Wrong info"

    def test_list_user_feedback(self, db_session: Session) -> None:
        service = FeedbackService(rate_limiter=None)
        user = _student(db_session)
        for i in range(3):
            service.create_feedback(
                db_session, job_id=f"job-{i:03d}", user=user,
                category=JobFeedbackCategory.CLOSED,
                note=f"note-{i}", idempotency_key=f"key-list-{i}",
            )
        total, items = service.list_user_feedback(db_session, user_id=user.id)
        assert total == 3
        assert len(items) == 3

    def test_list_all_feedback(self, db_session: Session) -> None:
        service = FeedbackService(rate_limiter=None)
        student = _student(db_session)
        admin = _admin(db_session)
        service.create_feedback(
            db_session, job_id="job-a", user=student,
            category=JobFeedbackCategory.CLOSED,
            note="from student", idempotency_key="key-all-1",
        )
        service.create_feedback(
            db_session, job_id="job-b", user=admin,
            category=JobFeedbackCategory.CONTENT_CHANGED,
            note="from admin", idempotency_key="key-all-2",
        )
        total, items = service.list_all_feedback(db_session)
        assert total == 2
        assert len(items) == 2
