from __future__ import annotations

from typing import Any, cast

from sqlalchemy import select, update
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


class TaskNotFoundError(LookupError):
    def __init__(self, task_id: str) -> None:
        super().__init__(f"application task {task_id} was not found")


def get_authoritative(
    db: Session, task_id: str, *, lock: bool = False
) -> ApplicationTask | None:
    statement = (
        select(ApplicationTask)
        .where(ApplicationTask.id == task_id)
        .execution_options(populate_existing=True)
    )
    if lock:
        statement = statement.with_for_update()
    return db.scalar(statement)


def transition(
    db: Session,
    *,
    task_id: str,
    source: ApplicationTaskStatus,
    expected_version: int,
    target: ApplicationTaskStatus,
    actor: TaskActor,
    event_type: str,
    redacted_payload: dict[str, object],
) -> ApplicationTask:
    statement = (
        update(ApplicationTask)
        .where(
            ApplicationTask.id == task_id,
            ApplicationTask.state_version == expected_version,
            ApplicationTask.status == source,
        )
        .values(
            status=target,
            state_version=expected_version + 1,
            updated_at=utc_now(),
        )
    )
    result = cast(CursorResult[Any], db.execute(statement))
    if result.rowcount != 1:
        authoritative = get_authoritative(db, task_id, lock=True)
        if authoritative is None:
            raise TaskNotFoundError(task_id)
        raise StaleTaskVersionError(task_id)

    db.add(
        ApplicationEvent(
            task_id=task_id,
            actor=actor,
            event_type=event_type,
            from_status=source.value,
            to_status=target.value,
            redacted_payload=redacted_payload,
        )
    )
    db.flush()
    authoritative = get_authoritative(db, task_id)
    if authoritative is None:
        raise TaskNotFoundError(task_id)
    return authoritative
