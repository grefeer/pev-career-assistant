from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.db.models import (
    JobDuplicateCandidate, JobPosting, JobPostingStatus, JobSource, JobSourceLink,
    JobSourceLinkType, RawJobRecord, SubmissionStatus, UserJobSubmission,
)


MANUAL_SOURCE_ID = "00000000-0000-4000-8000-000000000006"
MANUAL_MAPPER_VERSION = "manual-submission-v1"


@dataclass(frozen=True)
class PersistedMatch:
    job_id: str
    score_basis_points: int
    reasons: list[str]
    score_components: dict[str, int]
    algorithm_version: str


def get_owned(db: Session, *, user_id: str, submission_id: str) -> UserJobSubmission | None:
    return db.scalar(select(UserJobSubmission).where(
        UserJobSubmission.id == submission_id, UserJobSubmission.user_id == user_id,
    ))


def list_owned(db: Session, *, user_id: str, limit: int, offset: int) -> tuple[int, list[UserJobSubmission]]:
    condition = UserJobSubmission.user_id == user_id
    total = db.scalar(select(func.count()).select_from(UserJobSubmission).where(condition)) or 0
    rows = db.scalars(select(UserJobSubmission).where(condition).order_by(
        UserJobSubmission.updated_at.desc(), UserJobSubmission.id.desc(),
    ).limit(limit).offset(offset)).all()
    return int(total), list(rows)


def get_for_admin(db: Session, *, submission_id: str, lock: bool = False) -> UserJobSubmission | None:
    statement = select(UserJobSubmission).where(UserJobSubmission.id == submission_id)
    if lock:
        statement = statement.execution_options(populate_existing=True).with_for_update()
    return db.scalar(statement)


def list_for_admin(
    db: Session, *, status: SubmissionStatus, limit: int, offset: int,
) -> tuple[int, list[UserJobSubmission]]:
    condition = UserJobSubmission.status == status
    total = db.scalar(select(func.count()).select_from(UserJobSubmission).where(condition)) or 0
    rows = db.scalars(select(UserJobSubmission).where(condition).order_by(
        UserJobSubmission.updated_at.asc(), UserJobSubmission.id.asc(),
    ).limit(limit).offset(offset)).all()
    return int(total), list(rows)


def list_job_fingerprints(db: Session) -> list[JobPosting]:
    return list(db.scalars(select(JobPosting).where(
        JobPosting.status.not_in({JobPostingStatus.REJECTED, JobPostingStatus.EXPIRED})
    ).order_by(JobPosting.updated_at.desc(), JobPosting.id.desc())))


def add_candidates(
    db: Session, *, submission: UserJobSubmission, matches: Sequence[PersistedMatch],
) -> None:
    existing = db.scalar(select(func.count()).select_from(JobDuplicateCandidate).where(
        JobDuplicateCandidate.submission_id == submission.id,
        JobDuplicateCandidate.generated_for_version == submission.version,
    )) or 0
    if existing:
        return
    db.add_all([JobDuplicateCandidate(
        submission_id=submission.id, candidate_job_id=item.job_id,
        generated_for_version=submission.version, score_basis_points=item.score_basis_points,
        reasons=item.reasons, score_components=item.score_components,
        algorithm_version=item.algorithm_version,
    ) for item in matches])
    db.flush()


def list_candidates(
    db: Session, *, submission: UserJobSubmission, public_only: bool,
) -> list[tuple[JobDuplicateCandidate, JobPosting]]:
    latest_generated_version = select(func.max(JobDuplicateCandidate.generated_for_version)).where(
        JobDuplicateCandidate.submission_id == submission.id,
        JobDuplicateCandidate.generated_for_version <= submission.version,
    ).scalar_subquery()
    statement = select(JobDuplicateCandidate, JobPosting).join(
        JobPosting, JobPosting.id == JobDuplicateCandidate.candidate_job_id
    ).where(
        JobDuplicateCandidate.submission_id == submission.id,
        JobDuplicateCandidate.generated_for_version == latest_generated_version,
    )
    if public_only:
        statement = statement.where(JobPosting.status == JobPostingStatus.VERIFIED)
    statement = statement.order_by(
        JobDuplicateCandidate.score_basis_points.desc(), JobPosting.id.asc()
    )
    return [(candidate, posting) for candidate, posting in db.execute(statement)]


def ensure_tencent_source_link(
    db: Session, *, posting: JobPosting, source: JobSource,
) -> JobSourceLink:
    reference = f"{source.id}:{posting.external_record_id}"
    existing = db.scalar(select(JobSourceLink).where(
        JobSourceLink.job_id == posting.id,
        JobSourceLink.source_type == JobSourceLinkType.TENCENT_SMARTSHEET,
        JobSourceLink.source_record_ref == reference,
    ))
    if existing is not None:
        return existing
    link = JobSourceLink(
        job_id=posting.id, source_type=JobSourceLinkType.TENCENT_SMARTSHEET,
        source_id=source.id, submission_id=None, source_record_ref=reference,
        normalized_url=posting.apply_url,
    )
    db.add(link)
    db.flush()
    return link


def create_manual_pending_posting(
    db: Session, *, submission: UserJobSubmission, company_name: str,
    title: str, apply_url: str, now: datetime,
) -> JobPosting:
    source = db.get(JobSource, MANUAL_SOURCE_ID)
    if source is None:
        raise RuntimeError("manual job source is missing")
    raw = RawJobRecord(
        source_id=source.id, external_record_id=submission.id,
        payload_hash=submission.content_sha256,
        raw_fields=[{"field_name": "submission_reference", "value": submission.id}],
        source_updated_at=None, observed_at=now,
    )
    db.add(raw)
    db.flush()
    posting = JobPosting(
        source_id=source.id, external_record_id=submission.id, raw_record_id=raw.id,
        status=JobPostingStatus.PENDING_COMPLETION, company_name=company_name,
        title=title, description_text=submission.original_jd,
        locations=[], recruitment_types=[], industries=[], apply_url=apply_url,
        referral_code=None, deadline_text=None, source_updated_at=None,
        mapper_version=MANUAL_MAPPER_VERSION,
        source_candidate={
            "company_name": company_name, "title": title, "locations": [],
            "recruitment_types": [], "industries": [], "apply_url": apply_url,
            "referral_code": None, "deadline_text": None,
        },
    )
    db.add(posting)
    db.flush()
    db.add(JobSourceLink(
        job_id=posting.id, source_type=JobSourceLinkType.USER_SUBMISSION,
        source_id=None, submission_id=submission.id, source_record_ref=submission.id,
        normalized_url=submission.normalized_url,
    ))
    db.flush()
    return posting


def link_submission_to_posting(
    db: Session, *, submission: UserJobSubmission, posting: JobPosting,
) -> JobSourceLink:
    existing = db.scalar(select(JobSourceLink).where(
        JobSourceLink.job_id == posting.id,
        JobSourceLink.source_type == JobSourceLinkType.USER_SUBMISSION,
        JobSourceLink.source_record_ref == submission.id,
    ))
    if existing is not None:
        return existing
    link = JobSourceLink(
        job_id=posting.id, source_type=JobSourceLinkType.USER_SUBMISSION,
        source_id=None, submission_id=submission.id, source_record_ref=submission.id,
        normalized_url=submission.normalized_url,
    )
    db.add(link)
    db.flush()
    return link
