from __future__ import annotations

from typing import Protocol

from sqlalchemy.orm import Session

from backend.app.api.executor_schemas import (
    ExecutorTaskPayload,
    ExecutorTaskPayloadAny,
)
from backend.app.db.models import (
    ApplicationTask,
    ApplicationTaskStatus,
    Device,
    TaskActor,
)
from backend.app.repositories import executor_tasks
from backend.app.services.applications import (
    ApplicationService,
    TaskNotFoundError,
)
from backend.app.services.executor_v2_provider import (
    SnapshotExecutorPayloadProvider,
)


class ExecutorTaskNotFoundError(LookupError):
    pass


class ExecutorPayloadUnavailableError(RuntimeError):
    pass


class ExecutorPayloadProvider(Protocol):
    def payload_for(self, task: ApplicationTask) -> ExecutorTaskPayload:
        raise NotImplementedError


class UnavailableExecutorPayloadProvider:
    def payload_for(self, task: ApplicationTask) -> ExecutorTaskPayload:
        raise ExecutorPayloadUnavailableError(task.id)


class SimulationExecutorPayloadProvider:
    ROUTES = {
        "simulation-single": "/single-page",
        "simulation-multi": "/multi-step/1",
        "simulation-ambiguous": "/ambiguous",
        "simulation-human": "/human-gate",
        "simulation-mismatch": "/readback-mismatch",
        "simulation-result-success": "/submission-success",
        "simulation-result-failed": "/submission-failed",
        "simulation-result-unknown": "/submission-unknown",
    }

    def __init__(self, base_url: str = "http://127.0.0.1:8765") -> None:
        self.base_url = base_url.rstrip("/")

    def payload_for(self, task: ApplicationTask) -> ExecutorTaskPayload:
        route = self.ROUTES.get(task.target_job_id)
        if route is None:
            raise ExecutorPayloadUnavailableError(task.id)
        return ExecutorTaskPayload(
            task_id=task.id,
            state_version=task.state_version,
            target_url=f"{self.base_url}{route}",
            fields=[
                {
                    "field_key": "full_name",
                    "label": "\u59d3\u540d",
                    "value": "Alice Example",
                    "confidence": "confirmed",
                    "required": True,
                    "sensitive": False,
                },
                {
                    "field_key": "portfolio_url",
                    "label": "\u4f5c\u54c1\u94fe\u63a5",
                    "value": None,
                    "confidence": "missing",
                    "required": False,
                    "sensitive": False,
                },
            ],
        )


class ExecutorTaskService:
    def __init__(
        self, payload_provider: ExecutorPayloadProvider | None = None
    ) -> None:
        self.payload_provider = (
            payload_provider or UnavailableExecutorPayloadProvider()
        )

    def list_assigned(
        self, db: Session, *, device: Device
    ) -> list[ApplicationTask]:
        return executor_tasks.list_assigned(
            db, device_id=device.id, user_id=device.user_id
        )

    def get_assigned(
        self, db: Session, *, device: Device, task_id: str
    ) -> ApplicationTask:
        task = executor_tasks.get_assigned(
            db, device_id=device.id, user_id=device.user_id, task_id=task_id
        )
        if task is None:
            raise ExecutorTaskNotFoundError(task_id)
        return task

    def get_payload(
        self, db: Session, *, device: Device, task_id: str
    ) -> tuple[ApplicationTask, ExecutorTaskPayloadAny]:
        task = self.get_assigned(db, device=device, task_id=task_id)

        # Dispatch by task_kind
        if task.task_kind == "application":
            v2_provider = SnapshotExecutorPayloadProvider(db)
            payload = v2_provider.payload_for(task)
        else:
            payload = self.payload_provider.payload_for(task)

        if payload.task_id != task.id or payload.state_version != task.state_version:
            raise ExecutorPayloadUnavailableError(task.id)
        return task, payload

    def report_progress(
        self,
        db: Session,
        *,
        device: Device,
        task_id: str,
        expected_version: int,
        target: ApplicationTaskStatus,
        page_fingerprint: str,
        page_index: int | None,
        reason_code: str | None,
        field_counts: dict[str, int],
    ) -> ApplicationTask:
        self.get_assigned(db, device=device, task_id=task_id)
        try:
            task = ApplicationService().transition(
                db,
                task_id=task_id,
                expected_version=expected_version,
                target=target,
                actor=TaskActor.EXECUTOR,
                event_type="executor.progress",
                redacted_payload={
                    "page_fingerprint": page_fingerprint,
                    "page_index": page_index,
                    "reason_code": reason_code or "",
                    "confirmed_count": field_counts["confirmed"],
                    "defaulted_count": field_counts["defaulted"],
                    "missing_count": field_counts["missing"],
                    "low_confidence_count": field_counts["low"],
                },
                required_device_id=device.id,
                required_user_id=device.user_id,
            )
        except TaskNotFoundError as error:
            raise ExecutorTaskNotFoundError(task_id) from error
        db.commit()
        return task

    def report_result(
        self,
        db: Session,
        *,
        device: Device,
        task_id: str,
        expected_version: int,
        target: ApplicationTaskStatus,
        page_fingerprint: str,
        reason_code: str,
    ) -> ApplicationTask:
        self.get_assigned(db, device=device, task_id=task_id)
        try:
            task = ApplicationService().transition(
                db,
                task_id=task_id,
                expected_version=expected_version,
                target=target,
                actor=TaskActor.EXECUTOR,
                event_type="executor.result_observed",
                redacted_payload={
                    "page_fingerprint": page_fingerprint,
                    "reason_code": reason_code,
                },
                required_device_id=device.id,
                required_user_id=device.user_id,
            )
        except TaskNotFoundError as error:
            raise ExecutorTaskNotFoundError(task_id) from error
        db.commit()
        return task
