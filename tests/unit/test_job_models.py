from sqlalchemy import UniqueConstraint

from backend.app.db.base import Base
from backend.app.db.models import JobPostingStatus


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
