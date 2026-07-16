from sqlalchemy import UniqueConstraint

from backend.app.db.base import Base
from backend.app.db.models import JobPosting, JobPostingStatus, JobVerification


def test_job_sync_tables_and_status_exist() -> None:
    assert {
        "job_sources",
        "job_sync_runs",
        "raw_job_records",
        "job_postings",
    } <= set(Base.metadata.tables)
    assert JobPostingStatus.PENDING_COMPLETION.value == "pending_completion"


def test_raw_snapshot_and_posting_identity_are_unique() -> None:
    raw = Base.metadata.tables["raw_job_records"]
    posting = Base.metadata.tables["job_postings"]
    raw_unique = {
        tuple(constraint.columns.keys())
        for constraint in raw.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    posting_unique = {
        tuple(constraint.columns.keys())
        for constraint in posting.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert ("source_id", "external_record_id", "payload_hash") in raw_unique
    assert ("source_id", "external_record_id") in posting_unique


def test_job_posting_status_covers_review_lifecycle() -> None:
    assert {item.value for item in JobPostingStatus} == {
        "pending_completion",
        "pending_review",
        "verified",
        "expired",
        "rejected",
    }


def test_job_posting_has_review_and_source_candidate_fields() -> None:
    columns = JobPosting.__table__.columns
    assert {
        "description_text",
        "source_candidate",
        "source_changed_since_review",
        "gui_eligible",
        "review_version",
        "verified_at",
        "expired_at",
        "rejected_at",
    } <= set(columns.keys())


def test_job_verification_is_an_immutable_review_record() -> None:
    columns = JobVerification.__table__.columns
    assert {
        "job_id",
        "actor_user_id",
        "action",
        "from_status",
        "to_status",
        "review_version",
        "field_snapshot",
        "reason_code",
        "created_at",
    } <= set(columns.keys())
