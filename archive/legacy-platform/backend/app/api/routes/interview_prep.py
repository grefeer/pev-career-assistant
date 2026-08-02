"""API routes for interview prep (面试准备).

A synchronous MVP: ``POST /interview-prep`` creates a ``generating`` kit,
runs the LLM generator in-process, and writes the terminal outcome before
returning.  Gated behind ``interview_prep_enabled`` so a deployment must
explicitly opt in before the endpoint spends LLM budget.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from backend.app.api.dependencies import (
    _get_db,
    get_current_user,
    get_interview_prep_service,
)
from backend.app.api.interview_prep_schemas import (
    CreateInterviewPrepRequest,
    InterviewPrepKitResponse,
    InterviewPrepListResponse,
)
from backend.app.db.models import InterviewPrepKit, User
from backend.app.services.interview_prep.service import (
    InterviewPrepInputError,
    InterviewPrepNotFoundError,
    InterviewPrepService,
)

router = APIRouter(tags=["interview_prep"])

#: Map a service input-error code to an HTTP status. Unknown codes default to
#: 400 (bad request) so an unexpected failure is never a silent 500.
_INPUT_ERROR_STATUS: dict[str, int] = {
    "not_found": status.HTTP_404_NOT_FOUND,
    "match_not_completed": status.HTTP_409_CONFLICT,
    "match_failed": status.HTTP_409_CONFLICT,
}


def _to_response(kit: InterviewPrepKit) -> InterviewPrepKitResponse:
    """Map a kit ORM row to the API response (``*_json`` -> clean names)."""
    return InterviewPrepKitResponse(
        id=str(kit.id),
        user_id=str(kit.user_id),
        target_job_id=str(kit.target_job_id) if kit.target_job_id else None,
        profile_version_id=str(kit.profile_version_id)
        if kit.profile_version_id
        else None,
        agent_version=kit.agent_version,
        status=kit.status.value if hasattr(kit.status, "value") else kit.status,
        content=kit.content_json,
        preferences=kit.preferences_summary_json,
        match_analysis=kit.match_analysis_json,
        error_code=kit.error_code,
        last_error=kit.last_error,
        created_at=kit.created_at,
        updated_at=kit.updated_at,
        started_at=kit.started_at,
        finished_at=kit.finished_at,
    )


def _ensure_enabled(request: Request) -> None:
    settings = request.app.state.settings
    if not getattr(settings, "interview_prep_enabled", False):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "interview_prep_disabled",
                "message": "面试准备功能未启用。",
            },
        )


@router.post(
    "/interview-prep",
    response_model=InterviewPrepKitResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_kit(
    req: CreateInterviewPrepRequest,
    request: Request,
    db: Annotated[Session, Depends(_get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    service: Annotated[InterviewPrepService, Depends(get_interview_prep_service)],
) -> InterviewPrepKitResponse:
    """Generate an interview-prep kit from a completed match report."""
    _ensure_enabled(request)
    try:
        kit = service.create_kit(
            db, user=current_user, match_report_id=req.match_report_id
        )
    except InterviewPrepInputError as exc:
        http_status = _INPUT_ERROR_STATUS.get(exc.code, status.HTTP_400_BAD_REQUEST)
        raise HTTPException(
            status_code=http_status,
            detail={"code": exc.code},
        ) from exc
    db.commit()
    return _to_response(kit)


@router.get(
    "/interview-prep/{kit_id}",
    response_model=InterviewPrepKitResponse,
)
def get_kit(
    kit_id: str,
    db: Annotated[Session, Depends(_get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    service: Annotated[InterviewPrepService, Depends(get_interview_prep_service)],
) -> InterviewPrepKitResponse:
    """Get a single interview-prep kit (owner-scoped)."""
    try:
        kit = service.get_kit(db, kit_id, user=current_user)
    except InterviewPrepNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found"},
        ) from None
    return _to_response(kit)


@router.get(
    "/interview-prep",
    response_model=InterviewPrepListResponse,
)
def list_kits(
    db: Annotated[Session, Depends(_get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    service: Annotated[InterviewPrepService, Depends(get_interview_prep_service)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> InterviewPrepListResponse:
    """Page through the current user's interview-prep kits."""
    items = service.list_kits(db, user=current_user, limit=limit, offset=offset)
    return InterviewPrepListResponse(
        items=[_to_response(k) for k in items],
        total=len(items),
    )
