from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from backend.app.api.dependencies import _get_db, require_admin
from backend.app.api.discovery_schemas import (
    DiscoveredJobCandidateResponse,
    JobDiscoveryReviewGroupResponse,
    JobDiscoveryRetryRequest,
    JobDiscoveryTaskListResponse,
    JobDiscoveryTaskResponse,
)
from backend.app.db.models import User
from backend.app.services.job_discovery.admin_service import (
    JobDiscoveryAdminConflictError,
    JobDiscoveryAdminNotFoundError,
    JobDiscoveryAdminService,
)


router = APIRouter(tags=["job_discovery"])
logger = logging.getLogger(__name__)


def _task_to_response(task: Any, source_name: str | None = None) -> JobDiscoveryTaskResponse:
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


def _candidate_to_response(candidate: Any) -> DiscoveredJobCandidateResponse:
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


def _service_error(error: Exception) -> HTTPException:
    if isinstance(error, JobDiscoveryAdminNotFoundError):
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(error, JobDiscoveryAdminConflictError):
        return HTTPException(status_code=409, detail=str(error))
    raise error


@router.get("/admin/job-discovery/tasks", response_model=JobDiscoveryTaskListResponse)
def list_job_discovery_tasks(
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(_get_db)],
    status: str | None = Query(None),
) -> JobDiscoveryTaskListResponse:
    del admin
    rows = JobDiscoveryAdminService().list_tasks(db, status=status)
    return JobDiscoveryTaskListResponse(
        tasks=[_task_to_response(task, source_name) for task, source_name in rows]
    )


@router.get("/admin/job-discovery/groups", response_model=list[JobDiscoveryReviewGroupResponse])
def list_review_groups(
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(_get_db)],
) -> list[JobDiscoveryReviewGroupResponse]:
    del admin
    groups = JobDiscoveryAdminService().list_review_groups(db)
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
    service = JobDiscoveryAdminService()
    try:
        task = service.retry_task(db, task_id=task_id, admin=admin)
    except (JobDiscoveryAdminNotFoundError, JobDiscoveryAdminConflictError) as error:
        raise _service_error(error) from error
    return _task_to_response(task, service.source_name(db, task.source_key))


@router.post("/admin/job-discovery/candidates/{candidate_id}/approve", response_model=DiscoveredJobCandidateResponse)
def approve_job_discovery_candidate(
    candidate_id: str,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(_get_db)],
) -> DiscoveredJobCandidateResponse:
    try:
        candidate = JobDiscoveryAdminService().approve_candidate(
            db, candidate_id=candidate_id, admin=admin,
        )
    except (JobDiscoveryAdminNotFoundError, JobDiscoveryAdminConflictError) as error:
        raise _service_error(error) from error
    return _candidate_to_response(candidate)


@router.post("/admin/job-discovery/candidates/{candidate_id}/reject", response_model=DiscoveredJobCandidateResponse)
def reject_job_discovery_candidate(
    candidate_id: str,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(_get_db)],
) -> DiscoveredJobCandidateResponse:
    try:
        candidate = JobDiscoveryAdminService().reject_candidate(
            db, candidate_id=candidate_id, admin=admin,
        )
    except (JobDiscoveryAdminNotFoundError, JobDiscoveryAdminConflictError) as error:
        raise _service_error(error) from error
    return _candidate_to_response(candidate)
