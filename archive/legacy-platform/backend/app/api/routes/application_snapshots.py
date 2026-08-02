"""API routes for application snapshot operations."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from backend.app.api.dependencies import _get_db, get_current_user
from backend.app.api.snapshot_schemas import (
    CreateSnapshotRequest,
    CreateTaskRequest,
    DispatchTaskRequest,
    DispatchTaskResponse,
    SnapshotListResponse,
    SnapshotResponse,
    TaskEligibilityResponse,
)
from backend.app.db.models import ApplicationSnapshot, User
from backend.app.services.application_snapshot_service import (
    create_application_task,
    create_snapshot,
)
from backend.app.services.applications import (
    InvalidTransitionError,
    StaleTaskVersionError,
    TaskNotFoundError,
    assign_and_dispatch_task,
)
from backend.app.services.snapshot_validators import SnapshotValidationError
from backend.app.services.task_eligibility_service import check_task_eligibility

router = APIRouter(tags=["application_snapshots"])


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------


def _to_snapshot_response(snapshot: ApplicationSnapshot) -> SnapshotResponse:
    """Convert an ApplicationSnapshot ORM instance to the API schema.

    Sensitive fields (``profile_facts``, ``dynamic_answers``,
    ``local_sensitive_requirements``) are intentionally excluded.
    """
    job_snapshot = snapshot.job_snapshot or {}
    return SnapshotResponse(
        id=snapshot.id,
        job_id=snapshot.job_id,
        approved_resume_version_id=snapshot.approved_resume_version_id,
        profile_version_id=snapshot.profile_version_id,
        company_name=job_snapshot.get("company_name", ""),
        title=job_snapshot.get("title", ""),
        gui_eligible=snapshot.gui_eligible,
        job_status_at_snapshot=snapshot.job_status_at_snapshot,
        job_review_version_at_snapshot=snapshot.job_review_version_at_snapshot,
        created_at=str(snapshot.created_at),
        schema_version=snapshot.schema_version,
    )


# ---------------------------------------------------------------------------
# POST /api/application-snapshots
# ---------------------------------------------------------------------------


@router.post(
    "/application-snapshots",
    response_model=SnapshotResponse,
    status_code=201,
)
def create_application_snapshot(
    req: CreateSnapshotRequest,
    db: Annotated[Session, Depends(_get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> SnapshotResponse:
    """Create an application snapshot from an approved resume version.

    Requires ``Idempotency-Key`` header.

    The snapshot freezes the job posting state, profile facts, dynamic
    answers, and local-sensitive requirements at creation time for
    later audit and task dispatch.
    """
    try:
        snapshot = create_snapshot(
            db=db,
            user_id=current_user.id,
            job_id=req.job_id,
            approved_resume_version_id=req.approved_resume_version_id,
            dynamic_answers=req.dynamic_answers,
            local_sensitive_requirements=req.local_sensitive_requirements,
            idempotency_key=idempotency_key,
        )
    except SnapshotValidationError as e:
        raise HTTPException(422, detail={"code": e.error_code})
    except ValueError as e:
        code = str(e)
        if code.startswith("job_not_found"):
            raise HTTPException(404, detail={"code": "not_found"})
        if code == "approved_resume_version_not_found":
            raise HTTPException(404, detail={"code": "approved_resume_version_not_found"})
        if "idempotency" in code.lower():
            raise HTTPException(409, detail={"code": "idempotency_key_conflict"})
        raise HTTPException(422, detail={"code": code})

    return _to_snapshot_response(snapshot)


# ---------------------------------------------------------------------------
# GET /api/application-snapshots/{snapshot_id}
# ---------------------------------------------------------------------------


@router.get(
    "/application-snapshots/{snapshot_id}",
    response_model=SnapshotResponse,
)
def get_application_snapshot(
    snapshot_id: str,
    db: Annotated[Session, Depends(_get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> SnapshotResponse:
    """Get a single application snapshot by id (user-scoped)."""
    snapshot = (
        db.query(ApplicationSnapshot)
        .filter(
            ApplicationSnapshot.id == snapshot_id,
            ApplicationSnapshot.user_id == current_user.id,
        )
        .first()
    )
    if snapshot is None:
        raise HTTPException(404, detail={"code": "not_found"})
    return _to_snapshot_response(snapshot)


# ---------------------------------------------------------------------------
# GET /api/application-snapshots
# ---------------------------------------------------------------------------


@router.get(
    "/application-snapshots",
    response_model=SnapshotListResponse,
)
def list_application_snapshots(
    db: Annotated[Session, Depends(_get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> SnapshotListResponse:
    """List all application snapshots for the current user."""
    items = (
        db.query(ApplicationSnapshot)
        .filter(ApplicationSnapshot.user_id == current_user.id)
        .order_by(ApplicationSnapshot.created_at.desc())
        .all()
    )
    return SnapshotListResponse(
        items=[_to_snapshot_response(s) for s in items],
        total=len(items),
    )


# ---------------------------------------------------------------------------
# POST /api/application-snapshots/{snapshot_id}/create-task
# ---------------------------------------------------------------------------


@router.post(
    "/application-snapshots/{snapshot_id}/create-task",
    status_code=201,
)
def create_task_for_snapshot(
    snapshot_id: str,
    req: CreateTaskRequest,
    db: Annotated[Session, Depends(_get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> dict:
    """Create an application task under an existing snapshot.

    Requires ``Idempotency-Key`` header.  Optionally accepts
    ``device_id`` in the request body to pre-bind a device.
    """
    try:
        task = create_application_task(
            db=db,
            user_id=current_user.id,
            snapshot_id=snapshot_id,
            idempotency_key=idempotency_key,
            device_id=req.device_id,
        )
    except ValueError as e:
        code = str(e)
        if code == "snapshot_not_found":
            raise HTTPException(404, detail={"code": "not_found"})
        if code.startswith("task_not_eligible"):
            raise HTTPException(422, detail={"code": code})
        raise HTTPException(422, detail={"code": code})

    return {
        "task_id": task.id,
        "snapshot_id": snapshot_id,
        "status": task.status.value,
        "state_version": task.state_version,
    }


# ---------------------------------------------------------------------------
# GET /api/application-snapshots/{snapshot_id}/task-eligibility
# ---------------------------------------------------------------------------


@router.get(
    "/application-snapshots/{snapshot_id}/task-eligibility",
    response_model=TaskEligibilityResponse,
)
def get_task_eligibility(
    snapshot_id: str,
    db: Annotated[Session, Depends(_get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> TaskEligibilityResponse:
    """Check whether an application task can be created for this snapshot.

    Returns ``{"can_create_task": true/false, "reason_code": str | null}``.
    """
    can_create, reason = check_task_eligibility(
        db=db,
        user_id=current_user.id,
        snapshot_id=snapshot_id,
    )
    return TaskEligibilityResponse(
        can_create_task=can_create,
        reason_code=reason,
    )


# ---------------------------------------------------------------------------
# POST /api/application-tasks/{task_id}/dispatch
# ---------------------------------------------------------------------------


@router.post(
    "/application-tasks/{task_id}/dispatch",
    response_model=DispatchTaskResponse,
)
def dispatch_application_task(
    task_id: str,
    req: DispatchTaskRequest,
    db: Annotated[Session, Depends(_get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> DispatchTaskResponse:
    """Dispatch an existing CREATED application task to a device.

    Validates device ownership and task state before performing
    the two-step state machine transition:

        1. ``CREATED`` → ``WAITING_FOR_DEVICE``
        2. ``WAITING_FOR_DEVICE`` → ``DISPATCHED``

    The device is bound to the task between transitions.
    """
    try:
        task = assign_and_dispatch_task(
            db=db,
            user_id=current_user.id,
            task_id=task_id,
            device_id=req.device_id,
            expected_version=req.expected_version,
        )
    except TaskNotFoundError:
        raise HTTPException(404, detail={"code": "not_found"})
    except StaleTaskVersionError:
        raise HTTPException(409, detail={"code": "stale_version"})
    except InvalidTransitionError:
        raise HTTPException(409, detail={"code": "invalid_transition"})
    except ValueError as e:
        code = str(e)
        if code.startswith("device_"):
            raise HTTPException(422, detail={"code": code})
        raise HTTPException(422, detail={"code": code})

    return DispatchTaskResponse(
        task_id=task.id,
        status=task.status.value,
        state_version=task.state_version,
    )
