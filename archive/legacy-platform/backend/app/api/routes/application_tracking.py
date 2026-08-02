"""API routes for application tracking (投递进度跟踪).

A non-agent, user-scoped skill: the user records the jobs they have applied to
and advances each through the state machine.  Gated behind
``application_tracking_enabled`` so a deployment must explicitly opt in.  The
platform never auto-submits (security gate #1) - status advances are an
explicit human action, each recorded in an append-only event log.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from backend.app.api.application_tracking_schemas import (
    ApplicationEventListResponse,
    ApplicationEventResponse,
    ApplicationListResponse,
    ApplicationRecordResponse,
    CreateApplicationRequest,
    TransitionRequest,
    UpdateApplicationRequest,
)
from backend.app.api.dependencies import (
    _get_db,
    get_application_tracking_service,
    get_current_user,
)
from backend.app.db.models import ApplicationRecord, ApplicationRecordEvent, User
from backend.app.domain.application_tracking import ApplicationStatus
from backend.app.services.application_tracking.service import (
    ApplicationInputError,
    ApplicationNotFoundError,
    ApplicationTrackingService,
)

router = APIRouter(tags=["application_tracking"])

#: Map a service input-error code to an HTTP status. Unknown codes default to
#: 400 so an unexpected failure is never a silent 500.
_INPUT_ERROR_STATUS: dict[str, int] = {
    "invalid_transition": status.HTTP_409_CONFLICT,
    "already_terminal": status.HTTP_409_CONFLICT,
    "stale_version": status.HTTP_409_CONFLICT,
    "no_fields": status.HTTP_400_BAD_REQUEST,
}


def _status_value(value: object) -> str:
    return value.value if hasattr(value, "value") else str(value)


def _to_response(record: ApplicationRecord) -> ApplicationRecordResponse:
    """Map an application ORM row to the API response."""
    return ApplicationRecordResponse(
        id=str(record.id),
        user_id=str(record.user_id),
        target_job_id=str(record.target_job_id) if record.target_job_id else None,
        company_name=record.company_name,
        title=record.title,
        apply_url=record.apply_url,
        source=record.source,
        status=_status_value(record.status),
        applied_at=record.applied_at,
        notes=record.notes,
        state_version=record.state_version,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _to_event_response(event: ApplicationRecordEvent) -> ApplicationEventResponse:
    return ApplicationEventResponse(
        id=event.id,
        application_id=str(event.application_id),
        from_status=event.from_status,
        to_status=event.to_status,
        note=event.note,
        created_at=event.created_at,
    )


def _ensure_enabled(request: Request) -> None:
    settings = request.app.state.settings
    if not getattr(settings, "application_tracking_enabled", False):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "application_tracking_disabled",
                "message": "投递进度跟踪功能未启用。",
            },
        )


@router.post(
    "/applications",
    response_model=ApplicationRecordResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_application(
    req: CreateApplicationRequest,
    request: Request,
    db: Annotated[Session, Depends(_get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    service: Annotated[
        ApplicationTrackingService, Depends(get_application_tracking_service)
    ],
) -> ApplicationRecordResponse:
    """Record a new tracked application (starts in ``saved``)."""
    _ensure_enabled(request)
    record = service.create_application(
        db,
        user=current_user,
        company_name=req.company_name,
        title=req.title,
        apply_url=req.apply_url,
        source=req.source,
        notes=req.notes,
        target_job_id=req.target_job_id,
    )
    db.commit()
    return _to_response(record)


@router.get(
    "/applications/{application_id}",
    response_model=ApplicationRecordResponse,
)
def get_application(
    application_id: str,
    db: Annotated[Session, Depends(_get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    service: Annotated[
        ApplicationTrackingService, Depends(get_application_tracking_service)
    ],
) -> ApplicationRecordResponse:
    """Get a single tracked application (owner-scoped)."""
    try:
        record = service.get_application(
            db, user=current_user, application_id=application_id
        )
    except ApplicationNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found"},
        ) from None
    return _to_response(record)


@router.get(
    "/applications",
    response_model=ApplicationListResponse,
)
def list_applications(
    db: Annotated[Session, Depends(_get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    service: Annotated[
        ApplicationTrackingService, Depends(get_application_tracking_service)
    ],
    status_filter: Annotated[
        ApplicationStatus | None, Query(alias="status")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ApplicationListResponse:
    """Page through the current user's tracked applications."""
    items, total = service.list_applications(
        db,
        user=current_user,
        status=status_filter,
        limit=limit,
        offset=offset,
    )
    return ApplicationListResponse(
        items=[_to_response(r) for r in items],
        total=total,
    )


@router.post(
    "/applications/{application_id}/transitions",
    response_model=ApplicationRecordResponse,
)
def transition_application(
    application_id: str,
    req: TransitionRequest,
    request: Request,
    db: Annotated[Session, Depends(_get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    service: Annotated[
        ApplicationTrackingService, Depends(get_application_tracking_service)
    ],
) -> ApplicationRecordResponse:
    """Advance an application to a new status (explicit human action)."""
    _ensure_enabled(request)
    try:
        record = service.transition(
            db,
            user=current_user,
            application_id=application_id,
            to_status=req.to_status,
            note=req.note,
            expected_version=req.expected_version,
        )
    except ApplicationNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found"},
        ) from None
    except ApplicationInputError as exc:
        http_status = _INPUT_ERROR_STATUS.get(exc.code, status.HTTP_400_BAD_REQUEST)
        raise HTTPException(
            status_code=http_status,
            detail={"code": exc.code},
        ) from exc
    db.commit()
    return _to_response(record)


@router.patch(
    "/applications/{application_id}",
    response_model=ApplicationRecordResponse,
)
def update_application(
    application_id: str,
    req: UpdateApplicationRequest,
    request: Request,
    db: Annotated[Session, Depends(_get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    service: Annotated[
        ApplicationTrackingService, Depends(get_application_tracking_service)
    ],
) -> ApplicationRecordResponse:
    """Patch editable fields (notes / apply_url) on an application."""
    _ensure_enabled(request)
    fields = req.model_dump(exclude_unset=True)
    try:
        record = service.update_application(
            db,
            user=current_user,
            application_id=application_id,
            **fields,
        )
    except ApplicationNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found"},
        ) from None
    except ApplicationInputError as exc:
        http_status = _INPUT_ERROR_STATUS.get(exc.code, status.HTTP_400_BAD_REQUEST)
        raise HTTPException(
            status_code=http_status,
            detail={"code": exc.code},
        ) from exc
    db.commit()
    return _to_response(record)


@router.get(
    "/applications/{application_id}/events",
    response_model=ApplicationEventListResponse,
)
def list_application_events(
    application_id: str,
    db: Annotated[Session, Depends(_get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    service: Annotated[
        ApplicationTrackingService, Depends(get_application_tracking_service)
    ],
) -> ApplicationEventListResponse:
    """Return the append-only transition history (owner-scoped)."""
    try:
        events = service.list_events(
            db, user=current_user, application_id=application_id
        )
    except ApplicationNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found"},
        ) from None
    return ApplicationEventListResponse(
        items=[_to_event_response(e) for e in events]
    )
