from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from backend.app.api.dependencies import _get_db, get_current_user, require_admin
from backend.app.api.job_submission_schemas import (
    AdminJobSubmissionDecisionRequest, AdminJobSubmissionListResponse,
    AdminJobSubmissionResponse, DuplicateCandidateListResponse,
    DuplicateCandidateResponse, DuplicateJobSummary, JobSubmissionCreateRequest,
    JobSubmissionListResponse, JobSubmissionResponse, JobSubmissionSubmitRequest,
    JobSubmissionUpdateRequest,
)
from backend.app.db.models import SubmissionStatus, User, UserJobSubmission
from backend.app.domain.job_submissions import InvalidSubmissionInput
from backend.app.repositories import job_submissions
from backend.app.services.job_submissions import (
    InvalidPromotionTarget, InvalidSubmissionTransition, JobSubmissionService,
    StaleSubmissionError, SubmissionNotFoundError,
)


router = APIRouter(tags=["job-submissions"])


def _response(item: UserJobSubmission) -> JobSubmissionResponse:
    return JobSubmissionResponse(
        id=item.id, input_type=item.input_type, input_preview=item.input_preview,
        normalized_url=item.normalized_url, status=item.status, version=item.version,
        deduplication_status=item.deduplication_status,
        deduplication_error_code=item.deduplication_error_code,
        promoted_job_id=item.promoted_job_id,
        created_at=item.created_at, updated_at=item.updated_at,
    )


def _admin_response(item: UserJobSubmission) -> AdminJobSubmissionResponse:
    return AdminJobSubmissionResponse(**_response(item).model_dump(), content_sha256=item.content_sha256)


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, SubmissionNotFoundError):
        return HTTPException(404, detail={"code": exc.error_code, "message": "职位提交不存在。"})
    if isinstance(exc, StaleSubmissionError):
        return HTTPException(409, detail={"code": exc.error_code, "message": "提交版本已过期，请重新加载。"})
    if isinstance(exc, InvalidSubmissionTransition):
        return HTTPException(409, detail={"code": exc.error_code, "message": "当前提交状态不允许此操作。"})
    if isinstance(exc, (InvalidSubmissionInput, InvalidPromotionTarget)):
        return HTTPException(422, detail={"code": exc.error_code, "message": "职位输入或提升目标不合法。"})
    raise exc


def _raw_value(body: JobSubmissionCreateRequest) -> str:
    return body.url if body.input_type.value == "url" else body.jd_text or ""


@router.post("/job-submissions", response_model=JobSubmissionResponse, status_code=status.HTTP_201_CREATED)
def create_submission(
    body: JobSubmissionCreateRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(_get_db)],
) -> JobSubmissionResponse:
    try:
        item = JobSubmissionService().create(
            db, user_id=current_user.id, input_type=body.input_type.value, raw_value=_raw_value(body)
        )
        response = _response(item)
        db.commit()
        return response
    except (InvalidSubmissionInput,) as exc:
        db.rollback()
        raise _error(exc) from None


@router.get("/job-submissions", response_model=JobSubmissionListResponse)
def list_submissions(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(_get_db)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> JobSubmissionListResponse:
    total, items = job_submissions.list_owned(db, user_id=current_user.id, limit=limit, offset=offset)
    return JobSubmissionListResponse(total=total, submissions=[_response(item) for item in items])


@router.get("/job-submissions/{submission_id}", response_model=JobSubmissionResponse)
def get_submission(
    submission_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(_get_db)],
) -> JobSubmissionResponse:
    item = job_submissions.get_owned(db, user_id=current_user.id, submission_id=submission_id)
    if item is None:
        raise _error(SubmissionNotFoundError(submission_id))
    return _response(item)


@router.patch("/job-submissions/{submission_id}", response_model=JobSubmissionResponse)
def update_submission(
    submission_id: str, body: JobSubmissionUpdateRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(_get_db)],
) -> JobSubmissionResponse:
    try:
        item = JobSubmissionService().update(
            db, user_id=current_user.id, submission_id=submission_id,
            expected_version=body.expected_version, input_type=body.input_type.value,
            raw_value=_raw_value(body),
        )
        response = _response(item)
        db.commit()
        return response
    except (SubmissionNotFoundError, StaleSubmissionError, InvalidSubmissionTransition, InvalidSubmissionInput) as exc:
        db.rollback()
        raise _error(exc) from None


@router.post("/job-submissions/{submission_id}/submit", response_model=JobSubmissionResponse)
def submit_submission(
    submission_id: str, body: JobSubmissionSubmitRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(_get_db)],
) -> JobSubmissionResponse:
    try:
        item = JobSubmissionService().submit(
            db, user_id=current_user.id, submission_id=submission_id,
            expected_version=body.expected_version,
        )
        response = _response(item)
        db.commit()
        return response
    except (SubmissionNotFoundError, StaleSubmissionError, InvalidSubmissionTransition) as exc:
        db.rollback()
        raise _error(exc) from None


@router.get(
    "/job-submissions/{submission_id}/duplicate-candidates",
    response_model=DuplicateCandidateListResponse,
)
def duplicate_candidates(
    submission_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(_get_db)],
) -> DuplicateCandidateListResponse:
    item = job_submissions.get_owned(db, user_id=current_user.id, submission_id=submission_id)
    if item is None:
        raise _error(SubmissionNotFoundError(submission_id))
    rows = job_submissions.list_candidates(db, submission=item, public_only=True)
    return DuplicateCandidateListResponse(candidates=[DuplicateCandidateResponse(
        job=DuplicateJobSummary(
            id=posting.id, company_name=posting.company_name, title=posting.title,
            status=posting.status.value, apply_url=posting.apply_url,
        ),
        score_basis_points=candidate.score_basis_points,
        reasons=candidate.reasons, score_components=candidate.score_components,
        algorithm_version=candidate.algorithm_version,
    ) for candidate, posting in rows])


@router.get("/admin/job-submissions", response_model=AdminJobSubmissionListResponse)
def admin_submission_queue(
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(_get_db)],
    submission_status: Annotated[SubmissionStatus, Query(alias="status")] = SubmissionStatus.SUBMITTED,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AdminJobSubmissionListResponse:
    del admin
    total, items = job_submissions.list_for_admin(
        db, status=submission_status, limit=limit, offset=offset
    )
    return AdminJobSubmissionListResponse(
        total=total, submissions=[_admin_response(item) for item in items]
    )


@router.post(
    "/admin/job-submissions/{submission_id}/decision",
    response_model=AdminJobSubmissionResponse,
)
def decide_submission(
    submission_id: str, body: AdminJobSubmissionDecisionRequest,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(_get_db)],
) -> AdminJobSubmissionResponse:
    service = JobSubmissionService()
    try:
        if body.action == "link_existing":
            if body.job_id is None:
                raise InvalidPromotionTarget("missing_job_id")
            item = service.link_existing(
                db, submission_id=submission_id, actor_user_id=admin.id,
                expected_version=body.expected_version, job_id=body.job_id,
            )
        elif body.action == "create_pending":
            if body.company_name is None or body.title is None:
                raise InvalidPromotionTarget("missing_pending_job_identity")
            item, _posting = service.create_pending(
                db, submission_id=submission_id, actor_user_id=admin.id,
                expected_version=body.expected_version, company_name=body.company_name,
                title=body.title, apply_url=body.apply_url or "",
            )
        else:
            if body.reason_code is None:
                raise InvalidPromotionTarget("missing_rejection_reason")
            item = service.reject(
                db, submission_id=submission_id, actor_user_id=admin.id,
                expected_version=body.expected_version, reason_code=body.reason_code,
            )
        response = _admin_response(item)
        db.commit()
        return response
    except (
        SubmissionNotFoundError, StaleSubmissionError, InvalidSubmissionTransition,
        InvalidSubmissionInput, InvalidPromotionTarget,
    ) as exc:
        db.rollback()
        raise _error(exc) from None


@router.get(
    "/admin/job-submissions/{submission_id}/duplicate-candidates",
    response_model=DuplicateCandidateListResponse,
)
def admin_duplicate_candidates(
    submission_id: str,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(_get_db)],
) -> DuplicateCandidateListResponse:
    del admin
    item = job_submissions.get_for_admin(db, submission_id=submission_id)
    if item is None:
        raise _error(SubmissionNotFoundError(submission_id))
    rows = job_submissions.list_candidates(db, submission=item, public_only=False)
    return DuplicateCandidateListResponse(candidates=[DuplicateCandidateResponse(
        job=DuplicateJobSummary(
            id=posting.id, company_name=posting.company_name, title=posting.title,
            status=posting.status.value, apply_url=posting.apply_url,
        ),
        score_basis_points=candidate.score_basis_points,
        reasons=candidate.reasons, score_components=candidate.score_components,
        algorithm_version=candidate.algorithm_version,
    ) for candidate, posting in rows])
