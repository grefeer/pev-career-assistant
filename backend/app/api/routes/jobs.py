from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from backend.app.api.dependencies import (
    _get_db,
    get_current_user,
    get_job_sync_service,
    require_admin,
)
from backend.app.api.job_schemas import (
    AdminJobDetail,
    AdminJobListResponse,
    JobCompletionRequest,
    JobDecisionRequest,
    JobDetail,
    JobListResponse,
    JobSummary,
    ReviewQueueStatus,
)
from backend.app.db.models import (
    JobPosting,
    JobPostingStatus,
    JobSource,
    JobSyncRunStatus,
    User,
)
from backend.app.repositories import jobs
from backend.app.services.job_review import (
    IncompleteJobError,
    InvalidJobReviewTransition,
    JobCompletionInput,
    JobNotFoundError,
    JobReviewService,
    StaleJobReviewError,
)
from backend.app.services.job_sync import JobSyncFailedError, JobSyncService


router = APIRouter(tags=["jobs"])


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class JobSyncResponse(BaseModel):
    run_id: str
    source_key: str
    status: JobSyncRunStatus
    pages_read: int
    records_read: int
    raw_snapshots_created: int
    postings_created: int
    postings_updated: int
    records_skipped_incomplete: int
    started_at: datetime
    finished_at: datetime

    _normalize_datetimes = field_validator("started_at", "finished_at", mode="before")(
        _as_utc
    )


def _job_summary(posting: JobPosting, source: JobSource) -> JobSummary:
    return JobSummary(
        id=posting.id,
        company_name=posting.company_name,
        title=posting.title,
        locations=posting.locations,
        recruitment_types=posting.recruitment_types,
        industries=posting.industries,
        apply_url=posting.apply_url,
        deadline_text=posting.deadline_text,
        status=posting.status,
        gui_eligible=posting.gui_eligible,
        source_key=source.source_key,
        source_name=source.name,
        updated_at=posting.updated_at,
    )


def _public_detail(posting: JobPosting, source: JobSource) -> JobDetail:
    if posting.description_text is None:
        raise RuntimeError("verified job is missing description_text")
    return JobDetail(
        **_job_summary(posting, source).model_dump(),
        description_text=posting.description_text,
        referral_code=posting.referral_code,
        verified_at=posting.verified_at,
    )


def _admin_detail(posting: JobPosting, source: JobSource) -> AdminJobDetail:
    return AdminJobDetail(
        **_job_summary(posting, source).model_dump(),
        description_text=posting.description_text,
        referral_code=posting.referral_code,
        source_candidate=posting.source_candidate,
        source_changed_since_review=posting.source_changed_since_review,
        review_version=posting.review_version,
    )


def _sync_failure_status(error_code: str) -> int:
    if error_code in {"tencent_protocol_error", "source_schema_changed"}:
        return 502
    if error_code == "tencent_timeout":
        return 504
    return 503


def _job_review_error(error: Exception) -> HTTPException:
    if isinstance(error, JobNotFoundError):
        return HTTPException(
            status_code=404,
            detail={"error_code": "job_not_found", "message": "职位不存在。"},
        )
    if isinstance(error, StaleJobReviewError):
        return HTTPException(
            status_code=409,
            detail={
                "error_code": "stale_job_review",
                "message": "职位审核版本已过期，请重新加载。",
            },
        )
    if isinstance(error, InvalidJobReviewTransition):
        return HTTPException(
            status_code=409,
            detail={
                "error_code": "invalid_job_transition",
                "message": "当前职位状态不允许执行此审核操作。",
            },
        )
    if isinstance(error, IncompleteJobError):
        return HTTPException(
            status_code=422,
            detail={
                "error_code": "incomplete_job",
                "message": "职位信息不完整或投递方式无效。",
            },
        )
    raise error


@router.post("/admin/job-sources/{source_key}/sync", response_model=JobSyncResponse)
def sync_job_source(
    source_key: str,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(_get_db)],
    service: Annotated[JobSyncService, Depends(get_job_sync_service)],
) -> JobSyncResponse:
    try:
        outcome = service.sync(db, source_key=source_key, actor_user_id=admin.id)
    except jobs.SourceNotFoundError:
        raise HTTPException(status_code=404, detail="职位来源不存在。") from None
    except (jobs.SyncConflictError, jobs.SourceDisabledError):
        raise HTTPException(status_code=409, detail="职位来源暂时无法同步。") from None
    except JobSyncFailedError as error:
        raise HTTPException(
            status_code=_sync_failure_status(error.error_code),
            detail={"error_code": error.error_code, "run_id": error.run_id},
        ) from None
    return JobSyncResponse.model_validate(outcome, from_attributes=True)


@router.get("/admin/jobs/review-queue", response_model=AdminJobListResponse)
def review_queue(
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(_get_db)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
    review_status: Annotated[ReviewQueueStatus | None, Query()] = None,
) -> AdminJobListResponse:
    del admin
    statuses = {JobPostingStatus(review_status)} if review_status is not None else set()
    total, rows = jobs.list_review_queue(
        db,
        statuses=statuses,
        limit=limit,
        offset=offset,
    )
    return AdminJobListResponse(
        total=total,
        jobs=[_admin_detail(posting, source) for posting, source in rows],
    )


@router.get("/admin/jobs/verified", response_model=AdminJobListResponse)
def list_verified_jobs(
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(_get_db)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AdminJobListResponse:
    del admin
    total, rows = jobs.list_public_postings(
        db,
        limit=limit,
        offset=offset,
        source_key=None,
        company=None,
        recruitment_type=None,
    )
    return AdminJobListResponse(
        total=total,
        jobs=[_admin_detail(posting, source) for posting, source in rows],
    )


@router.patch("/admin/jobs/{job_id}/completion", response_model=AdminJobDetail)
def save_job_completion(
    job_id: str,
    body: JobCompletionRequest,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(_get_db)],
) -> AdminJobDetail:
    service = JobReviewService()
    try:
        posting = service.save_completion(
            db,
            job_id=job_id,
            actor_user_id=admin.id,
            expected_version=body.expected_version,
            values=JobCompletionInput(
                company_name=body.company_name,
                title=body.title,
                description_text=body.description_text,
                locations=body.locations,
                recruitment_types=body.recruitment_types,
                industries=body.industries,
                apply_url=body.apply_url,
                referral_code=body.referral_code,
                deadline_text=body.deadline_text,
            ),
        )
        row = jobs.get_posting_for_review(db, posting.id)
        assert row is not None
        response = _admin_detail(*row)
        db.commit()
        return response
    except (
        JobNotFoundError,
        StaleJobReviewError,
        InvalidJobReviewTransition,
        IncompleteJobError,
    ) as error:
        db.rollback()
        raise _job_review_error(error) from None


@router.post("/admin/jobs/{job_id}/decision", response_model=AdminJobDetail)
def decide_job(
    job_id: str,
    body: JobDecisionRequest,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(_get_db)],
) -> AdminJobDetail:
    service = JobReviewService()
    try:
        if body.decision == "verify":
            posting = service.verify(
                db,
                job_id=job_id,
                actor_user_id=admin.id,
                expected_version=body.expected_version,
                gui_eligible=body.gui_eligible,
            )
        elif body.decision == "reject":
            assert body.reason_code is not None
            posting = service.reject(
                db,
                job_id=job_id,
                actor_user_id=admin.id,
                expected_version=body.expected_version,
                reason_code=body.reason_code,
            )
        else:
            assert body.reason_code is not None
            posting = service.expire(
                db,
                job_id=job_id,
                actor_user_id=admin.id,
                expected_version=body.expected_version,
                reason_code=body.reason_code,
            )
        row = jobs.get_posting_for_review(db, posting.id)
        assert row is not None
        response = _admin_detail(*row)
        db.commit()
        return response
    except (
        JobNotFoundError,
        StaleJobReviewError,
        InvalidJobReviewTransition,
        IncompleteJobError,
    ) as error:
        db.rollback()
        raise _job_review_error(error) from None


@router.get("/jobs", response_model=JobListResponse)
def list_jobs(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(_get_db)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
    source_key: Annotated[str | None, Query()] = None,
    company: Annotated[str | None, Query()] = None,
    recruitment_type: Annotated[str | None, Query()] = None,
) -> JobListResponse:
    del current_user
    total, rows = jobs.list_public_postings(
        db,
        limit=limit,
        offset=offset,
        source_key=source_key,
        company=company,
        recruitment_type=recruitment_type,
    )
    return JobListResponse(
        total=total,
        jobs=[_job_summary(posting, source) for posting, source in rows],
    )


@router.get("/jobs/{job_id}", response_model=JobDetail)
def get_job(
    job_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(_get_db)],
) -> JobDetail:
    del current_user
    row = jobs.get_public_posting(db, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="职位不存在。")
    return _public_detail(*row)
