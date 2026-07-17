import pytest
from pydantic import ValidationError
from sqlalchemy import UniqueConstraint

from backend.app.api.job_schemas import JobDecisionRequest
from backend.app.db.base import Base
from backend.app.db.models import (
    JobDuplicateCandidate,
    JobPosting,
    JobPostingStatus,
    JobSourceLink,
    JobSourceProvider,
    JobVerification,
    UserJobSubmission,
)


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


@pytest.mark.parametrize(
    "payload",
    [
        {"expected_version": 0, "decision": "reject"},
        {"expected_version": 0, "decision": "reject", "reason_code": ""},
        {"expected_version": 0, "decision": "expire", "reason_code": "   "},
        {"expected_version": 0, "decision": "verify", "reason_code": "unused"},
        {"expected_version": 0, "decision": "verify", "reason_code": ""},
        {"expected_version": 0, "decision": "reject", "reason_code": "unknown"},
        {
            "expected_version": 0,
            "decision": "reject",
            "reason_code": "closed_on_official_site",
        },
        {
            "expected_version": 0,
            "decision": "expire",
            "reason_code": "invalid_source",
        },
    ],
)
def test_job_decision_requires_explicit_reason_contract(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        JobDecisionRequest.model_validate(payload)


def test_manual_job_entities_use_uuid_and_versioned_private_ownership() -> None:
    assert JobSourceProvider.USER_SUBMISSION.value == "user_submission"
    assert {
        "user_id", "input_type", "original_url", "original_jd", "input_preview",
        "normalized_url", "content_sha256", "status", "version",
        "deduplication_status", "deduplication_error_code", "promoted_job_id",
        "rejected_reason_code",
    } <= set(UserJobSubmission.__table__.columns.keys())
    assert UserJobSubmission.__table__.columns.id.type.length == 36


def test_duplicate_candidate_and_source_link_preserve_explanations() -> None:
    assert {
        "submission_id", "candidate_job_id", "generated_for_version",
        "score_basis_points", "reasons", "score_components", "algorithm_version",
    } <= set(JobDuplicateCandidate.__table__.columns.keys())
    assert {
        "job_id", "source_type", "source_id", "submission_id",
        "source_record_ref", "normalized_url", "created_at",
    } <= set(JobSourceLink.__table__.columns.keys())


def test_job_decision_accepts_only_matching_reason_shape() -> None:
    rejected = JobDecisionRequest.model_validate(
        {"expected_version": 0, "decision": "reject", "reason_code": "invalid_source"}
    )
    expired = JobDecisionRequest.model_validate(
        {
            "expected_version": 1,
            "decision": "expire",
            "reason_code": "closed_on_official_site",
        }
    )
    verified = JobDecisionRequest.model_validate(
        {"expected_version": 2, "decision": "verify", "reason_code": None}
    )

    assert rejected.reason_code == "invalid_source"
    assert expired.reason_code == "closed_on_official_site"
    assert verified.reason_code is None
