from __future__ import annotations

from datetime import datetime, timezone
import uuid

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.app.db.models import (
    AuditEvent, DeduplicationStatus, JobPosting, JobPostingStatus,
    SubmissionInputType, SubmissionStatus, UserJobSubmission,
)
from backend.app.domain.job_submissions import (
    DuplicateDetector, JobFingerprint, normalize_submission_input,
)
from backend.app.repositories import job_submissions


class SubmissionNotFoundError(LookupError):
    error_code = "job_submission_not_found"


class StaleSubmissionError(RuntimeError):
    error_code = "stale_job_submission"


class InvalidSubmissionTransition(RuntimeError):
    error_code = "invalid_job_submission_transition"


class InvalidPromotionTarget(ValueError):
    error_code = "invalid_promotion_target"


class JobSubmissionService:
    def __init__(self, detector: DuplicateDetector | None = None) -> None:
        self.detector = detector or DuplicateDetector()

    def _owned(self, db: Session, *, user_id: str, submission_id: str) -> UserJobSubmission:
        item = job_submissions.get_owned(db, user_id=user_id, submission_id=submission_id)
        if item is None:
            raise SubmissionNotFoundError(submission_id)
        return item

    @staticmethod
    def _check_version(item: UserJobSubmission, expected_version: int) -> None:
        if item.version != expected_version:
            raise StaleSubmissionError(item.id)

    def _generate_candidates(self, db: Session, item: UserJobSubmission) -> None:
        try:
            with db.begin_nested():
                postings = job_submissions.list_job_fingerprints(db)
                normalized = normalize_submission_input(
                    item.input_type, item.original_url if item.input_type is SubmissionInputType.URL else item.original_jd or ""
                )
                matches = self.detector.find_candidates(normalized, [
                    JobFingerprint(row.id, row.apply_url, row.description_text) for row in postings
                ])
                job_submissions.add_candidates(db, submission=item, matches=[
                    job_submissions.PersistedMatch(
                        match.job_id, match.score_basis_points, list(match.reasons),
                        match.score_components, match.algorithm_version,
                    ) for match in matches
                ])
        except SQLAlchemyError:
            item.deduplication_status = DeduplicationStatus.FAILED
            item.deduplication_error_code = "duplicate_detection_failed"
        else:
            item.deduplication_status = DeduplicationStatus.SUCCEEDED
            item.deduplication_error_code = None
        db.flush()

    def create(
        self, db: Session, *, user_id: str, input_type: str, raw_value: str,
    ) -> UserJobSubmission:
        normalized = normalize_submission_input(SubmissionInputType(input_type), raw_value)
        item = UserJobSubmission(
            user_id=user_id, input_type=normalized.input_type,
            original_url=normalized.original_url, original_jd=normalized.original_jd,
            input_preview=normalized.preview, normalized_url=normalized.normalized_url,
            content_sha256=normalized.content_sha256, status=SubmissionStatus.DRAFT,
            version=0, deduplication_status=DeduplicationStatus.PENDING,
        )
        db.add(item)
        db.flush()
        self._generate_candidates(db, item)
        return item

    def update(
        self, db: Session, *, user_id: str, submission_id: str,
        expected_version: int, input_type: str, raw_value: str,
    ) -> UserJobSubmission:
        item = self._owned(db, user_id=user_id, submission_id=submission_id)
        self._check_version(item, expected_version)
        if item.status is not SubmissionStatus.DRAFT:
            raise InvalidSubmissionTransition(item.status.value)
        normalized = normalize_submission_input(SubmissionInputType(input_type), raw_value)
        item.input_type = normalized.input_type
        item.original_url = normalized.original_url
        item.original_jd = normalized.original_jd
        item.input_preview = normalized.preview
        item.normalized_url = normalized.normalized_url
        item.content_sha256 = normalized.content_sha256
        item.version += 1
        item.deduplication_status = DeduplicationStatus.PENDING
        item.deduplication_error_code = None
        db.flush()
        self._generate_candidates(db, item)
        return item

    def submit(
        self, db: Session, *, user_id: str, submission_id: str, expected_version: int,
    ) -> UserJobSubmission:
        item = self._owned(db, user_id=user_id, submission_id=submission_id)
        self._check_version(item, expected_version)
        if item.status is not SubmissionStatus.DRAFT:
            raise InvalidSubmissionTransition(item.status.value)
        item.status = SubmissionStatus.SUBMITTED
        item.version += 1
        db.flush()
        return item

    def _lock_submitted(
        self, db: Session, *, submission_id: str, expected_version: int,
    ) -> UserJobSubmission:
        item = job_submissions.get_for_admin(db, submission_id=submission_id, lock=True)
        if item is None:
            raise SubmissionNotFoundError(submission_id)
        self._check_version(item, expected_version)
        if item.status is not SubmissionStatus.SUBMITTED:
            raise InvalidSubmissionTransition(item.status.value)
        return item

    @staticmethod
    def _audit(
        db: Session, *, item: UserJobSubmission, actor_user_id: str,
        action: str, job_id: str | None,
    ) -> None:
        payload = {"action": action}
        if job_id is not None:
            payload["job_id"] = job_id
        db.add(AuditEvent(
            actor_user_id=actor_user_id, actor_device_id=None,
            event_type=f"job_submission.{action}", entity_type="user_job_submission",
            entity_id=item.id, correlation_id=str(uuid.uuid4()), redacted_payload=payload,
        ))

    def link_existing(
        self, db: Session, *, submission_id: str, actor_user_id: str,
        expected_version: int, job_id: str,
    ) -> UserJobSubmission:
        item = self._lock_submitted(db, submission_id=submission_id, expected_version=expected_version)
        posting = db.scalar(select(JobPosting).where(JobPosting.id == job_id).with_for_update())
        if posting is None or posting.status in {JobPostingStatus.REJECTED, JobPostingStatus.EXPIRED}:
            raise InvalidPromotionTarget(job_id)
        job_submissions.link_submission_to_posting(db, submission=item, posting=posting)
        item.status = SubmissionStatus.PROMOTED
        item.promoted_job_id = posting.id
        item.version += 1
        self._audit(db, item=item, actor_user_id=actor_user_id, action="link_existing", job_id=posting.id)
        db.flush()
        return item

    def create_pending(
        self, db: Session, *, submission_id: str, actor_user_id: str,
        expected_version: int, company_name: str, title: str, apply_url: str,
    ) -> tuple[UserJobSubmission, JobPosting]:
        item = self._lock_submitted(db, submission_id=submission_id, expected_version=expected_version)
        if apply_url:
            normalize_submission_input(SubmissionInputType.URL, apply_url)
        posting = job_submissions.create_manual_pending_posting(
            db, submission=item, company_name=company_name.strip(), title=title.strip(),
            apply_url=apply_url.strip(), now=datetime.now(timezone.utc),
        )
        item.status = SubmissionStatus.PROMOTED
        item.promoted_job_id = posting.id
        item.version += 1
        self._audit(db, item=item, actor_user_id=actor_user_id, action="create_pending", job_id=posting.id)
        db.flush()
        return item, posting

    def reject(
        self, db: Session, *, submission_id: str, actor_user_id: str,
        expected_version: int, reason_code: str,
    ) -> UserJobSubmission:
        item = self._lock_submitted(db, submission_id=submission_id, expected_version=expected_version)
        allowed = {"not_a_job", "insufficient_evidence", "unsafe_link", "duplicate_submission"}
        if reason_code not in allowed:
            raise InvalidPromotionTarget(reason_code)
        item.status = SubmissionStatus.REJECTED
        item.rejected_reason_code = reason_code
        item.version += 1
        self._audit(db, item=item, actor_user_id=actor_user_id, action="reject", job_id=None)
        db.flush()
        return item
