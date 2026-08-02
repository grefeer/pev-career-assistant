from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import os
import subprocess
import sys
from threading import Barrier
import uuid

import pytest
from sqlalchemy import Engine, create_engine, delete, func, select
from sqlalchemy.orm import Session

from backend.app.db.models import (
    JobFeedback,
    JobFeedbackEvent,
    JobPosting,
    JobPostingStatus,
    JobSource,
    JobSourceProvider,
    RawJobRecord,
    User,
    UserRole,
)
from backend.app.domain.job_feedback import FeedbackStudentAction, JobFeedbackCategory
from backend.app.services.job_feedback import FeedbackMutationResult, JobFeedbackService


@dataclass(frozen=True)
class SeededIds:
    student_id: str
    source_id: str
    raw_id: str
    job_id: str


@pytest.fixture
def mysql_engine(destructive_mysql_url: str) -> Engine:
    env = {
        **os.environ,
        "DATABASE_URL": destructive_mysql_url,
        "APP_AUTH_SECRET": "test-secret-with-at-least-32-characters",
        "OBJECT_ENCRYPTION_KEY": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        "REDIS_URL": "redis://localhost:6379/15",
    }
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        check=True,
        env=env,
        capture_output=True,
        text=True,
    )
    engine = create_engine(destructive_mysql_url, pool_pre_ping=True)
    yield engine
    engine.dispose()


@pytest.fixture
def seeded_ids(mysql_engine: Engine):
    suffix = uuid.uuid4().hex
    with Session(mysql_engine) as db:
        student = User(
            account=f"feedback-{suffix}", nickname="Feedback Student",
            password_hash="hash", role=UserRole.STUDENT,
        )
        source = JobSource(
            source_key=f"feedback-{suffix}", provider=JobSourceProvider.USER_SUBMISSION,
            name="Feedback Source", file_id=f"file-{suffix}", sheet_id=f"sheet-{suffix}",
            mapper_version="v1", enabled=True,
        )
        db.add_all([student, source])
        db.flush()
        raw = RawJobRecord(
            source_id=source.id, external_record_id=suffix, payload_hash="f" * 64,
            raw_fields=[],
        )
        db.add(raw)
        db.flush()
        job = JobPosting(
            source_id=source.id, external_record_id=suffix, raw_record_id=raw.id,
            status=JobPostingStatus.VERIFIED, company_name="Company", title="Role",
            locations=[], recruitment_types=[], industries=[], apply_url="https://example.com",
            mapper_version="v1", source_candidate={},
        )
        db.add(job)
        db.commit()
        ids = SeededIds(student.id, source.id, raw.id, job.id)
    yield ids
    with Session(mysql_engine) as db:
        feedback_ids = list(
            db.scalars(select(JobFeedback.id).where(JobFeedback.job_id == ids.job_id))
        )
        if feedback_ids:
            db.execute(delete(JobFeedbackEvent).where(JobFeedbackEvent.feedback_id.in_(feedback_ids)))
        db.execute(delete(JobFeedback).where(JobFeedback.job_id == ids.job_id))
        db.execute(delete(JobPosting).where(JobPosting.id == ids.job_id))
        db.execute(delete(RawJobRecord).where(RawJobRecord.id == ids.raw_id))
        db.execute(delete(JobSource).where(JobSource.id == ids.source_id))
        db.execute(delete(User).where(User.id == ids.student_id))
        db.commit()


def test_mysql_same_idempotency_key_serializes_to_one_event(
    mysql_engine: Engine, seeded_ids: SeededIds,
) -> None:
    barrier = Barrier(2)

    def submit() -> FeedbackMutationResult:
        with Session(mysql_engine, expire_on_commit=False) as db:
            barrier.wait(timeout=5)
            result = JobFeedbackService().mutate_student(
                db, job_id=seeded_ids.job_id, actor_user_id=seeded_ids.student_id,
                idempotency_key="mysql-feedback-key-0001",
                action=FeedbackStudentAction.UPSERT,
                category=JobFeedbackCategory.CLOSED,
                expected_version=None, note="官网显示职位关闭",
            )
            db.commit()
            return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(lambda _: submit(), range(2)))
    assert first == second
    with Session(mysql_engine) as db:
        assert db.scalar(
            select(func.count(JobFeedback.id)).where(JobFeedback.job_id == seeded_ids.job_id)
        ) == 1
        assert db.scalar(
            select(func.count(JobFeedbackEvent.id)).where(
                JobFeedbackEvent.feedback_id == first.id
            )
        ) == 1
