from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Request
from sqlalchemy.orm import Session

from backend.app.api import dependencies
from backend.app.api.executor_schemas import (
    ExecutorTaskDetail,
    ExecutorTaskListResponse,
    ExecutorTaskSummary,
)
from backend.app.db.models import ApplicationTask, Device
from backend.app.services.executor_tasks import (
    ExecutorPayloadUnavailableError,
    ExecutorTaskNotFoundError,
    ExecutorTaskService,
    SimulationExecutorPayloadProvider,
)


router = APIRouter(prefix="/executor/tasks", tags=["executor"])


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
