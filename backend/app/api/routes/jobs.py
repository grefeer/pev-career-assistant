from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.app.api.dependencies import (
    _get_db,
    get_current_user,
    get_job_sync_service,
    require_admin,
)
from backend.app.db.models import (
    JobPosting,
    JobPostingStatus,
    JobSource,
    JobSyncRunStatus,
    User,
)
from backend.app.repositories import jobs
from backend.app.services.job_sync import JobSyncFailedError, JobSyncService


router = APIRouter(tags=["jobs"])


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


class JobSummary(BaseModel):
    id: str
    company_name: str
    title: str
    locations: list[str]
    recruitment_types: list[str]
    industries: list[str]
    apply_url: str
    deadline_text: str | None
    status: JobPostingStatus
    source_key: str
    source_name: str
    updated_at: datetime


class JobListResponse(BaseModel):
    total: int
    jobs: list[JobSummary]


class JobDetail(JobSummary):
    referral_code: str | None
    source_updated_at: datetime | None
    mapper_version: str


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
        source_key=source.source_key,
        source_name=source.name,
        updated_at=posting.updated_at,
    )


def _job_detail(posting: JobPosting, source: JobSource) -> JobDetail:
    summary = _job_summary(posting, source)
    return JobDetail(
        **summary.model_dump(),
        referral_code=posting.referral_code,
        source_updated_at=posting.source_updated_at,
        mapper_version=posting.mapper_version,
    )


def _sync_failure_status(error_code: str) -> int:
    if error_code in {"tencent_protocol_error", "source_schema_changed"}:
        return 502
    if error_code == "tencent_timeout":
        return 504
    return 503


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
    total, rows = jobs.list_postings(
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
    row = jobs.get_posting(db, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="职位不存在。")
    return _job_detail(*row)
