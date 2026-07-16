from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.app.db.base import Base
from backend.app.db.models import JobFeedback, JobFeedbackCategory
from backend.app.repositories import feedbacks as feedback_repo


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


class TestFeedbackRepository:
    def test_create_and_get_by_id(self, db_session: Session) -> None:
        item = feedback_repo.create_feedback(
            db_session,
            job_id="job-001", user_id="user-001",
            category=JobFeedbackCategory.CLOSED,
            note="This job is no longer available",
            idempotency_key="idem-001-" + "a" * 37,
        )
        db_session.flush()
        assert item.id is not None
        assert item.job_id == "job-001"
        assert item.user_id == "user-001"
        assert item.category == JobFeedbackCategory.CLOSED
        assert item.note == "This job is no longer available"
        assert item.idempotency_key.startswith("idem-001-")

        fetched = feedback_repo.get_by_id(db_session, feedback_id=item.id)
        assert fetched is not None
        assert fetched.id == item.id

    def test_get_by_idempotency_key(self, db_session: Session) -> None:
        key = "unique-idem-key-" + "b" * 38
        feedback_repo.create_feedback(
            db_session,
            job_id="job-002", user_id="user-002",
            category=JobFeedbackCategory.APPLICATION_CHANNEL_UNAVAILABLE,
            note=None, idempotency_key=key,
        )
        db_session.flush()

        found = feedback_repo.get_by_idempotency_key(db_session, idempotency_key=key)
        assert found is not None
        assert found.idempotency_key == key

        not_found = feedback_repo.get_by_idempotency_key(
            db_session, idempotency_key="nonexistent-key"
        )
        assert not_found is None

    def test_list_by_user(self, db_session: Session) -> None:
        for i in range(5):
            feedback_repo.create_feedback(
                db_session,
                job_id=f"job-{i:03d}", user_id="user-alice",
                category=JobFeedbackCategory.CLOSED,
                note=f"note-{i}", idempotency_key=f"key-alice-{i}",
            )
        feedback_repo.create_feedback(
            db_session,
            job_id="job-other", user_id="user-bob",
            category=JobFeedbackCategory.CONTENT_CHANGED,
            note="bob note", idempotency_key="key-bob-1",
        )
        db_session.flush()

        total, items = feedback_repo.list_by_user(db_session, user_id="user-alice")
        assert total == 5
        assert len(items) == 5

        total, items = feedback_repo.list_by_user(
            db_session, user_id="user-alice", job_id="job-000",
        )
        assert total == 1
        assert len(items) == 1

    def test_list_by_job(self, db_session: Session) -> None:
        for i in range(3):
            feedback_repo.create_feedback(
                db_session,
                job_id="job-xyz", user_id=f"user-{i}",
                category=JobFeedbackCategory.INCORRECT_INFORMATION,
                note=None, idempotency_key=f"key-job-{i}",
            )
        db_session.flush()

        total, items = feedback_repo.list_by_job(db_session, job_id="job-xyz")
        assert total == 3
        assert len(items) == 3

        total, items = feedback_repo.list_by_job(db_session, job_id="job-nonexistent")
        assert total == 0
        assert len(items) == 0

    def test_list_all(self, db_session: Session) -> None:
        for i in range(3):
            feedback_repo.create_feedback(
                db_session,
                job_id=f"job-{i}", user_id=f"user-{i}",
                category=JobFeedbackCategory.CLOSED,
                note=None, idempotency_key=f"key-all-{i}",
            )
        db_session.flush()

        total, items = feedback_repo.list_all(db_session)
        assert total == 3
        assert len(items) == 3
