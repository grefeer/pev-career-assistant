from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.db.models import ApplicationTask, ApplicationTaskStatus


ACTIVE_EXECUTOR_STATUSES = frozenset(
    {
        ApplicationTaskStatus.DISPATCHED,
        ApplicationTaskStatus.RUNNING,
        ApplicationTaskStatus.WAITING_FOR_HUMAN,
        ApplicationTaskStatus.READY_FOR_REVIEW,
        ApplicationTaskStatus.OBSERVING_USER_SUBMISSION,
    }
)


def list_assigned(
    db: Session, *, device_id: str, user_id: str
) -> list[ApplicationTask]:
    return list(
        db.scalars(
            select(ApplicationTask)
            .where(
                ApplicationTask.device_id == device_id,
                ApplicationTask.user_id == user_id,
                ApplicationTask.status.in_(ACTIVE_EXECUTOR_STATUSES),
            )
            .order_by(ApplicationTask.updated_at.asc(), ApplicationTask.id.asc())
        )
    )


def get_assigned(
    db: Session, *, device_id: str, user_id: str, task_id: str
) -> ApplicationTask | None:
    return db.scalar(
        select(ApplicationTask).where(
            ApplicationTask.id == task_id,
            ApplicationTask.device_id == device_id,
            ApplicationTask.user_id == user_id,
        )
    )
