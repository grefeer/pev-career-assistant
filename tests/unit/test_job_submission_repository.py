from collections.abc import Iterator
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.db.base import Base
from backend.app.db.models import (
    DeduplicationStatus, JobPosting, JobPostingStatus, JobSource,
    JobSourceProvider, RawJobRecord, SubmissionInputType,
    SubmissionStatus, User, UserJobSubmission,
)
from backend.app.repositories import job_submissions


@pytest.fixture
def db() -> Iterator[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def _posting(db: Session, source: JobSource, record_id: str, status: JobPostingStatus) -> JobPosting:
    raw = RawJobRecord(
        source_id=source.id, external_record_id=record_id,
        payload_hash=(record_id[0] * 64), raw_fields=[],
        observed_at=datetime.now(timezone.utc),
    )
    db.add(raw)
    db.flush()
    posting = JobPosting(
        source_id=source.id, external_record_id=record_id, raw_record_id=raw.id,
        status=status, company_name="示例科技", title=f"岗位 {record_id}",
        description_text="负责 Python FastAPI MySQL 后端服务开发",
        locations=[], recruitment_types=[], industries=[],
        apply_url=f"https://jobs.example.com/{record_id}",
        mapper_version=source.mapper_version, source_candidate={},
    )
    db.add(posting)
    db.flush()
    return posting


def test_owned_reads_hide_another_users_submission(db: Session) -> None:
    owner = User(account="owner", nickname="Owner", password_hash="hash")
    other = User(account="other", nickname="Other", password_hash="hash")
    db.add_all([owner, other])
    db.flush()
    item = UserJobSubmission(
        user_id=owner.id, input_type=SubmissionInputType.URL,
        original_url="https://jobs.example.com/1", original_jd=None,
        input_preview="https://jobs.example.com/1",
        normalized_url="https://jobs.example.com/1", content_sha256="a" * 64,
        status=SubmissionStatus.DRAFT, version=0,
        deduplication_status=DeduplicationStatus.PENDING,
    )
    db.add(item)
    db.flush()
    assert job_submissions.get_owned(db, user_id=owner.id, submission_id=item.id) is item
    assert job_submissions.get_owned(db, user_id=other.id, submission_id=item.id) is None


def test_candidate_reads_use_only_current_submission_version(db: Session) -> None:
    owner = User(account="candidate-owner", nickname="Owner", password_hash="hash")
    source = JobSource(
        source_key="candidate-source", provider=JobSourceProvider.TENCENT_SMARTSHEET,
        name="Candidate Source", file_id="candidate-file", sheet_id="candidate-sheet",
        mapper_version="candidate-v1", enabled=True,
    )
    db.add_all([owner, source])
    db.flush()
    verified = _posting(db, source, "verified", JobPostingStatus.VERIFIED)
    pending = _posting(db, source, "pending", JobPostingStatus.PENDING_COMPLETION)
    submission = UserJobSubmission(
        user_id=owner.id, input_type=SubmissionInputType.JD_TEXT,
        original_url=None, original_jd="负责 Python FastAPI MySQL 后端服务开发",
        input_preview="负责 Python FastAPI MySQL 后端服务开发", normalized_url=None,
        content_sha256="c" * 64, status=SubmissionStatus.DRAFT, version=0,
        deduplication_status=DeduplicationStatus.PENDING,
    )
    db.add(submission)
    db.flush()
    job_submissions.add_candidates(db, submission=submission, matches=[
        job_submissions.PersistedMatch(verified.id, 9000, ["old"], {"x": 9000}, "manual-job-dedup-v1")
    ])
    submission.version = 1
    job_submissions.add_candidates(db, submission=submission, matches=[
        job_submissions.PersistedMatch(pending.id, 9500, ["new"], {"x": 9500}, "manual-job-dedup-v1")
    ])
    submission.version = 2
    student_rows = job_submissions.list_candidates(db, submission=submission, public_only=True)
    admin_rows = job_submissions.list_candidates(db, submission=submission, public_only=False)
    assert student_rows == []
    assert [row[1].id for row in admin_rows] == [pending.id]
