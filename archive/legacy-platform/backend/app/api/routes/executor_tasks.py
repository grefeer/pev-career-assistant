from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from sqlalchemy.orm import Session

from backend.app.api import dependencies
from backend.app.api.executor_schemas import (
    ExecutorProgressRequest,
    ExecutorResultRequest,
    ExecutorTaskDetail,
    ExecutorTaskListResponse,
    ExecutorTaskState,
    ExecutorTaskSummary,
)
from backend.app.db.models import ApplicationTask, ApplicationTaskStatus, Device
from backend.app.services.applications import (
    InvalidTransitionError,
    StaleTaskVersionError,
)
from backend.app.services.executor_tasks import (
    ExecutorPayloadUnavailableError,
    ExecutorTaskNotFoundError,
    ExecutorTaskService,
    SimulationExecutorPayloadProvider,
)


class ExecutorAPIRoute(APIRoute):
    def get_route_handler(self):
        original_handler = super().get_route_handler()

        async def stable_validation_handler(request: Request):
            try:
                return await original_handler(request)
            except RequestValidationError:
                return JSONResponse(
                    status_code=422,
                    content={
                        "detail": {
                            "error_code": "executor_validation_failed"
                        }
                    },
                )

        return stable_validation_handler


router = APIRouter(
    prefix="/executor/tasks",
    tags=["executor"],
    route_class=ExecutorAPIRoute,
)


# Use dependencies._get_db directly so dependency_overrides work in tests
_get_db = dependencies._get_db


def get_executor_task_service(request: Request) -> ExecutorTaskService:
    injected = getattr(request.app.state, "executor_payload_provider", None)
    if injected is not None:
        return ExecutorTaskService(injected)
    if request.app.state.settings.app_env == "development":
        return ExecutorTaskService(SimulationExecutorPayloadProvider())
    return ExecutorTaskService()


def _summary(task: ApplicationTask) -> ExecutorTaskSummary:
    return ExecutorTaskSummary(
        task_id=task.id,
        target_job_id=task.target_job_id,
        snapshot_id=task.snapshot_id,
        status=task.status,
        state_version=task.state_version,
    )


def _require_path_binding(task_id: str, header_task_id: str) -> None:
    if not hmac.compare_digest(task_id, header_task_id):
        raise HTTPException(status_code=401, detail={"error_code": "invalid_task_lease"})


def _task_error(error: Exception) -> HTTPException:
    if isinstance(error, ExecutorTaskNotFoundError):
        return HTTPException(
            status_code=404,
            detail={"error_code": "executor_task_not_found"},
        )
    if isinstance(error, ExecutorPayloadUnavailableError):
        return HTTPException(
            status_code=409,
            detail={"error_code": "executor_payload_unavailable"},
        )
    if isinstance(error, StaleTaskVersionError):
        return HTTPException(
            status_code=409,
            detail={"error_code": "stale_task_version"},
        )
    if isinstance(error, InvalidTransitionError):
        return HTTPException(
            status_code=409,
            detail={"error_code": "invalid_executor_transition"},
        )
    raise error


# Custom dependencies to avoid FastAPI param name conflict with path {task_id}
def _require_executor_progress_lease(
    db: Annotated[Session, Depends(_get_db)],
    device: Annotated[Device, Depends(dependencies.get_current_device)],
    service=Depends(dependencies.get_device_service),
    exec_task_id: Annotated[str | None, Header(alias="X-Task-ID")] = None,
    exec_task_lease: Annotated[str | None, Header(alias="X-Task-Lease")] = None,
) -> Device:
    from backend.app.services.devices import InvalidTaskLeaseError

    if not exec_task_id or not exec_task_lease:
        raise HTTPException(status_code=401, detail={"error_code": "invalid_task_lease"})
    try:
        service.verify_task_lease(
            db,
            exec_task_lease,
            device=device,
            task_id=exec_task_id,
            required_scope="task:progress",
        )
    except InvalidTaskLeaseError:
        raise HTTPException(status_code=401, detail={"error_code": "invalid_task_lease"}) from None
    return device


def _require_executor_result_lease(
    db: Annotated[Session, Depends(_get_db)],
    device: Annotated[Device, Depends(dependencies.get_current_device)],
    service=Depends(dependencies.get_device_service),
    exec_task_id: Annotated[str | None, Header(alias="X-Task-ID")] = None,
    exec_task_lease: Annotated[str | None, Header(alias="X-Task-Lease")] = None,
) -> Device:
    from backend.app.services.devices import InvalidTaskLeaseError

    if not exec_task_id or not exec_task_lease:
        raise HTTPException(status_code=401, detail={"error_code": "invalid_task_lease"})
    try:
        service.verify_task_lease(
            db,
            exec_task_lease,
            device=device,
            task_id=exec_task_id,
            required_scope="task:result",
        )
    except InvalidTaskLeaseError:
        raise HTTPException(status_code=401, detail={"error_code": "invalid_task_lease"}) from None
    return device


@router.get("", response_model=ExecutorTaskListResponse)
def list_executor_tasks(
    device: Annotated[Device, Depends(dependencies.get_current_device)],
    db: Annotated[Session, Depends(_get_db)],
    service: Annotated[ExecutorTaskService, Depends(get_executor_task_service)],
) -> ExecutorTaskListResponse:
    return ExecutorTaskListResponse(
        tasks=[
            _summary(task) for task in service.list_assigned(db, device=device)
        ]
    )


@router.get("/{task_id}", response_model=ExecutorTaskDetail)
def get_executor_task(
    task_id: str,
    header_task_id: Annotated[str, Header(alias="X-Task-ID")],
    device: Annotated[Device, Depends(_require_executor_progress_lease)],
    db: Annotated[Session, Depends(_get_db)],
    service: Annotated[ExecutorTaskService, Depends(get_executor_task_service)],
) -> ExecutorTaskDetail:
    _require_path_binding(task_id, header_task_id)
    try:
        task, payload = service.get_payload(db, device=device, task_id=task_id)
    except ExecutorTaskNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={"error_code": "executor_task_not_found"},
        ) from None
    except ExecutorPayloadUnavailableError:
        raise HTTPException(
            status_code=409,
            detail={"error_code": "executor_payload_unavailable"},
        ) from None
    return ExecutorTaskDetail(
        **_summary(task).model_dump(), payload=payload
    )


@router.post("/{task_id}/progress", response_model=ExecutorTaskState)
def report_executor_progress(
    task_id: str,
    body: ExecutorProgressRequest,
    header_task_id: Annotated[str, Header(alias="X-Task-ID")],
    device: Annotated[Device, Depends(_require_executor_progress_lease)],
    db: Annotated[Session, Depends(_get_db)],
    service: Annotated[ExecutorTaskService, Depends(get_executor_task_service)],
) -> ExecutorTaskState:
    _require_path_binding(task_id, header_task_id)
    try:
        task = service.report_progress(
            db,
            device=device,
            task_id=task_id,
            expected_version=body.expected_version,
            target=ApplicationTaskStatus(body.target_status),
            page_fingerprint=body.page_fingerprint,
            page_index=body.page_index,
            reason_code=body.reason_code,
            field_counts=body.field_counts.model_dump(),
        )
    except (ExecutorTaskNotFoundError, StaleTaskVersionError, InvalidTransitionError) as error:
        raise _task_error(error) from None
    return ExecutorTaskState(
        task_id=task.id, status=task.status, state_version=task.state_version
    )


@router.post("/{task_id}/result", response_model=ExecutorTaskState)
def report_executor_result(
    task_id: str,
    body: ExecutorResultRequest,
    header_task_id: Annotated[str, Header(alias="X-Task-ID")],
    device: Annotated[Device, Depends(_require_executor_result_lease)],
    db: Annotated[Session, Depends(_get_db)],
    service: Annotated[ExecutorTaskService, Depends(get_executor_task_service)],
) -> ExecutorTaskState:
    _require_path_binding(task_id, header_task_id)
    try:
        task = service.report_result(
            db,
            device=device,
            task_id=task_id,
            expected_version=body.expected_version,
            target=ApplicationTaskStatus(body.target_status),
            page_fingerprint=body.page_fingerprint,
            reason_code=body.reason_code,
        )
    except (ExecutorTaskNotFoundError, StaleTaskVersionError, InvalidTransitionError) as error:
        raise _task_error(error) from None
    return ExecutorTaskState(
        task_id=task.id, status=task.status, state_version=task.state_version
    )


# ---------------------------------------------------------------------------
# Attachment download
# ---------------------------------------------------------------------------


@router.get("/{task_id}/attachments/{attachment_id}")
def download_executor_attachment(
    task_id: str,
    attachment_id: str,
    header_task_id: Annotated[str, Header(alias="X-Task-ID")],
    device: Annotated[Device, Depends(_require_executor_progress_lease)],
    db: Annotated[Session, Depends(_get_db)],
    request: Request,
) -> Response:
    """Download a resume attachment for an executor task.

    Validates:
      1. Device token (X-Device-Token) authenticates the device.
      2. X-Task-ID + X-Task-Lease binds the device to this task.
      3. X-Task-ID matches the path ``task_id``.
      4. The task belongs to the device's user and references a snapshot
         that contains the requested attachment.
    """
    _require_path_binding(task_id, header_task_id)

    # Load the task and verify user/device ownership
    task = (
        db.query(ApplicationTask)
        .filter(
            ApplicationTask.id == task_id,
            ApplicationTask.user_id == device.user_id,
            ApplicationTask.device_id == device.id,
        )
        .first()
    )
    if task is None:
        raise HTTPException(
            status_code=404,
            detail={"error_code": "executor_task_not_found"},
        )

    # Load the snapshot
    snapshot_id = task.snapshot_id
    if not snapshot_id:
        raise HTTPException(
            status_code=409,
            detail={"error_code": "executor_payload_unavailable"},
        )

    from backend.app.db.models import ApplicationSnapshot

    snapshot = (
        db.query(ApplicationSnapshot)
        .filter(
            ApplicationSnapshot.id == snapshot_id,
            ApplicationSnapshot.user_id == device.user_id,
        )
        .first()
    )
    if snapshot is None:
        raise HTTPException(
            status_code=409,
            detail={"error_code": "executor_payload_unavailable"},
        )

    # Verify the attachment belongs to this snapshot
    attachment_ids: list[str] = list(snapshot.attachment_ids or [])
    if attachment_id not in attachment_ids:
        raise HTTPException(
            status_code=404,
            detail={"error_code": "attachment_not_found"},
        )

    # Load the attachment record
    from backend.app.db.models import ApprovedResumeAttachment

    attachment = (
        db.query(ApprovedResumeAttachment)
        .filter(
            ApprovedResumeAttachment.id == attachment_id,
            ApprovedResumeAttachment.user_id == device.user_id,
        )
        .first()
    )
    if attachment is None:
        raise HTTPException(
            status_code=404,
            detail={"error_code": "attachment_not_found"},
        )

    # Decrypt and return
    from backend.app.services.storage import EncryptedObjectStore

    object_store: EncryptedObjectStore = request.app.state.object_store  # type: ignore[assignment]
    try:
        body = object_store.get(key=attachment.object_key)
    except Exception:
        raise HTTPException(
            status_code=503,
            detail={"error_code": "attachment_storage_unavailable"},
        )

    return Response(
        content=body,
        media_type=attachment.content_type,
        headers={
            "Content-Disposition": f'attachment; filename="resume.{attachment.format}"',
            "X-Encryption-Version": attachment.encryption_version,
        },
    )
