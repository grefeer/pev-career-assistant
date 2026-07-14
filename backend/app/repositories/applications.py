from __future__ import annotations

from typing import Any, cast

from sqlalchemy import update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from backend.app.db.base import utc_now
from backend.app.db.models import (
    ApplicationEvent,
    ApplicationTask,
    ApplicationTaskStatus,
    TaskActor,
)


class StaleTaskVersionError(RuntimeError):
    def __init__(self, task_id: str) -> None:
        super().__init__(f"application task {task_id} has a stale state version")


def get_by_id(db: Session, task_id: str) -> ApplicationTask | None:
    return db.get(ApplicationTask, task_id)


def transition(
    db: Session,
    *,
    task: ApplicationTask,
    expected_version: int,
    target: ApplicationTaskStatus,
    actor: TaskActor,
    event_type: str,
    redacted_payload: dict[str, object],
) -> ApplicationTask:
    source = task.status
    statement = (
        update(ApplicationTask)
        .where(
            ApplicationTask.id == task.id,
            ApplicationTask.state_version == expected_version,
        )
        .values(
            status=target,
            state_version=expected_version + 1,
            updated_at=utc_now(),
        )
    )
    result = cast(CursorResult[Any], db.execute(statement))
    if result.rowcount != 1:
        raise StaleTaskVersionError(task.id)

    db.add(
        ApplicationEvent(
            task_id=task.id,
            actor=actor,
            event_type=event_type,
            from_status=source.value,
            to_status=target.value,
            redacted_payload=redacted_payload,
        )
    )
    db.flush()
    db.refresh(task)
    return task
