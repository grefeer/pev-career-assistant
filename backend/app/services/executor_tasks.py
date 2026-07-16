from __future__ import annotations

from typing import Protocol

from sqlalchemy.orm import Session

from backend.app.api.executor_schemas import ExecutorTaskPayload
from backend.app.db.models import ApplicationTask, Device
from backend.app.repositories import executor_tasks


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
    ) -> tuple[ApplicationTask, ExecutorTaskPayload]:
        task = self.get_assigned(db, device=device, task_id=task_id)
        payload = self.payload_provider.payload_for(task)
        if payload.task_id != task.id or payload.state_version != task.state_version:
            raise ExecutorPayloadUnavailableError(task.id)
        return task, payload
