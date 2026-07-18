from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.api.dependencies import _get_db, require_admin
from backend.app.api.discovery_schemas import (
    DiscoveredJobCandidateResponse,
    JobDiscoveryReviewGroupResponse,
    JobDiscoveryRetryRequest,
    JobDiscoveryTaskListResponse,
    JobDiscoveryTaskResponse,
)
from backend.app.db.models import (
    DiscoveredJobCandidate,
    DiscoveredJobCandidateStatus,
    JobDiscoveryTask,
    JobDiscoveryTaskStatus,
    JobPosting,
    JobPostingStatus,
    JobSource,
    User,
)
from backend.app.repositories import job_discovery as repository


router = APIRouter(tags=["job_discovery"])
logger = logging.getLogger(__name__)


def _task_to_response(task: JobDiscoveryTask, source_name: str | None = None) -> JobDiscoveryTaskResponse:
    return JobDiscoveryTaskResponse(
        id=task.id,
        source_key=task.source_key,
        source_name=source_name,
        source_url=task.source_url,
        status=task.status.value if hasattr(task.status, "value") else task.status,
        block_reason=task.block_reason.value if task.block_reason and hasattr(task.block_reason, "value") else str(task.block_reason) if task.block_reason else None,
        attempt_count=task.attempt_count,
        result_summary_json=task.result_summary_json,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


def _candidate_to_response(candidate: DiscoveredJobCandidate) -> DiscoveredJobCandidateResponse:
    return DiscoveredJobCandidateResponse(
        id=candidate.id,
        task_id=candidate.task_id,
        similarity_group_key=candidate.similarity_group_key,
        status=candidate.status.value if hasattr(candidate.status, "value") else candidate.status,
        title=candidate.title,
        company_name=candidate.company_name,
        description_text=candidate.description_text,
        locations_json=candidate.locations_json,
        apply_url=candidate.apply_url,
        confidence=candidate.confidence,
        evidence_refs_json=candidate.evidence_refs_json,
        normalization_warnings_json=candidate.normalization_warnings_json,
        created_at=candidate.created_at,
    )


def _candidate_not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="候选记录不存在。")


def _candidate_conflict(message: str) -> HTTPException:
    return HTTPException(status_code=409, detail=message)


@router.get("/admin/job-discovery/tasks", response_model=JobDiscoveryTaskListResponse)
def list_job_discovery_tasks(
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(_get_db)],
    status: str | None = Query(None),
) -> JobDiscoveryTaskListResponse:
    del admin
    query = (
        select(JobDiscoveryTask, JobSource.name)
        .outerjoin(JobSource, JobDiscoveryTask.source_key == JobSource.source_key)
        .order_by(JobDiscoveryTask.created_at.desc())
    )
    if status:
        query = query.where(JobDiscoveryTask.status == status)
    rows = db.execute(query).all()
    return JobDiscoveryTaskListResponse(
        tasks=[_task_to_response(task, source_name) for task, source_name in rows]
    )


@router.get("/admin/job-discovery/groups", response_model=list[JobDiscoveryReviewGroupResponse])
def list_review_groups(
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(_get_db)],
) -> list[JobDiscoveryReviewGroupResponse]:
    del admin
    groups = repository.list_review_groups(db)
    return [
        JobDiscoveryReviewGroupResponse(
            similarity_group_key=group["similarity_group_key"],
            candidates=[_candidate_to_response(c) for c in group["candidates"]],
        )
        for group in groups
    ]


@router.post("/admin/job-discovery/tasks/{task_id}/retry", response_model=JobDiscoveryTaskResponse)
def retry_job_discovery_task(
    task_id: str,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(_get_db)],
    body: JobDiscoveryRetryRequest | None = None,
) -> JobDiscoveryTaskResponse:
    del body
    del admin
    task = db.get(JobDiscoveryTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在。")
    if task.status is JobDiscoveryTaskStatus.running:
        raise HTTPException(status_code=409, detail="任务正在运行，无法重试。")

    task.status = JobDiscoveryTaskStatus.queued
    task.attempt_count = 0
    task.last_error = None
    task.block_reason = None
    task.finished_at = None
    db.commit()
    db.refresh(task)

    source_name: str | None = None
    source = db.scalar(
        select(JobSource).where(JobSource.source_key == task.source_key)
    )
    if source is not None:
        source_name = source.name

    return _task_to_response(task, source_name)


@router.post("/admin/job-discovery/candidates/{candidate_id}/approve", response_model=DiscoveredJobCandidateResponse)
def approve_job_discovery_candidate(
    candidate_id: str,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(_get_db)],
) -> DiscoveredJobCandidateResponse:
    del admin
    candidate = db.get(DiscoveredJobCandidate, candidate_id)
    if candidate is None:
        raise _candidate_not_found()
    if candidate.status is not DiscoveredJobCandidateStatus.pending_review:
        raise _candidate_conflict("候选记录状态不允许审批通过。")

    candidate.status = DiscoveredJobCandidateStatus.approved

    existing_posting = db.scalar(
        select(JobPosting).where(
            JobPosting.source_id == candidate.source_id,
            JobPosting.external_record_id == candidate.external_record_id,
        )
    )

    if existing_posting is not None:
        posting = existing_posting
        if candidate.title is not None:
            posting.title = candidate.title
        if candidate.company_name is not None:
            posting.company_name = candidate.company_name
        if candidate.description_text is not None:
            posting.description_text = candidate.description_text
        if candidate.locations_json is not None:
            posting.locations = candidate.locations_json
        if candidate.apply_url is not None:
            posting.apply_url = candidate.apply_url
        posting.status = JobPostingStatus.PENDING_REVIEW
    else:
        posting = JobPosting(
            source_id=candidate.source_id,
            external_record_id=candidate.external_record_id,
            raw_record_id=candidate.raw_record_id,
            status=JobPostingStatus.PENDING_REVIEW,
            company_name=candidate.company_name or "",
            title=candidate.title or "",
            description_text=candidate.description_text,
            locations=candidate.locations_json or [],
            apply_url=candidate.apply_url or "",
        )
        db.add(posting)

    db.commit()
    db.refresh(candidate)
    return _candidate_to_response(candidate)


@router.post("/admin/job-discovery/candidates/{candidate_id}/reject", response_model=DiscoveredJobCandidateResponse)
def reject_job_discovery_candidate(
    candidate_id: str,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(_get_db)],
) -> DiscoveredJobCandidateResponse:
    del admin
    candidate = db.get(DiscoveredJobCandidate, candidate_id)
    if candidate is None:
        raise _candidate_not_found()
    if candidate.status is not DiscoveredJobCandidateStatus.pending_review:
        raise _candidate_conflict("候选记录状态不允许拒绝。")

    candidate.status = DiscoveredJobCandidateStatus.rejected
    db.commit()
    db.refresh(candidate)
    return _candidate_to_response(candidate)
