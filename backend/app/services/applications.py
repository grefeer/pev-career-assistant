from __future__ import annotations

from collections.abc import Mapping
import logging

from sqlalchemy.orm import Session

from backend.app.db.models import (
    ApplicationTask,
    ApplicationTaskStatus,
    TaskActor,
)
from backend.app.repositories import applications
from backend.app.repositories.applications import (
    StaleTaskVersionError,
    TaskNotFoundError,
)


class InvalidTransitionError(ValueError):
    pass


class UnsafeAuditPayloadError(ValueError):
    pass


logger = logging.getLogger(__name__)


ALLOWED_TRANSITIONS = {
    ApplicationTaskStatus.CREATED: {
        ApplicationTaskStatus.WAITING_FOR_DEVICE,
        ApplicationTaskStatus.CANCELLED,
    },
    ApplicationTaskStatus.WAITING_FOR_DEVICE: {
        ApplicationTaskStatus.DISPATCHED,
        ApplicationTaskStatus.CANCELLED,
    },
    ApplicationTaskStatus.DISPATCHED: {
        ApplicationTaskStatus.RUNNING,
        ApplicationTaskStatus.WAITING_FOR_HUMAN,
        ApplicationTaskStatus.FAILED,
        ApplicationTaskStatus.CANCELLED,
    },
    ApplicationTaskStatus.RUNNING: {
        ApplicationTaskStatus.WAITING_FOR_HUMAN,
        ApplicationTaskStatus.READY_FOR_REVIEW,
        ApplicationTaskStatus.FAILED,
        ApplicationTaskStatus.CANCELLED,
    },
    ApplicationTaskStatus.WAITING_FOR_HUMAN: {
        ApplicationTaskStatus.RUNNING,
        ApplicationTaskStatus.READY_FOR_REVIEW,
        ApplicationTaskStatus.FAILED,
        ApplicationTaskStatus.CANCELLED,
    },
    ApplicationTaskStatus.READY_FOR_REVIEW: {
        ApplicationTaskStatus.OBSERVING_USER_SUBMISSION,
        ApplicationTaskStatus.CANCELLED,
    },
    ApplicationTaskStatus.OBSERVING_USER_SUBMISSION: {
        ApplicationTaskStatus.SUBMITTED_SUCCESS,
        ApplicationTaskStatus.SUBMITTED_FAILED,
        ApplicationTaskStatus.RESULT_UNKNOWN,
    },
    ApplicationTaskStatus.SUBMITTED_SUCCESS: set(),
    ApplicationTaskStatus.SUBMITTED_FAILED: set(),
    ApplicationTaskStatus.RESULT_UNKNOWN: set(),
    ApplicationTaskStatus.FAILED: set(),
    ApplicationTaskStatus.CANCELLED: set(),
}

FORBIDDEN_AUDIT_KEYS = {
    "password",
    "token",
    "cookie",
    "captcha",
    "id_card",
    "form_values",
    "resume_text",
}
MAX_AUDIT_STRING_LENGTH = 500


def _validate_redacted_value(value: object) -> None:
    if isinstance(value, str):
        if len(value) > MAX_AUDIT_STRING_LENGTH:
            raise UnsafeAuditPayloadError(
                "audit payload string values must not exceed 500 characters"
            )
        return
    if isinstance(value, Mapping):
        for key, nested_value in value.items():
            if isinstance(key, str) and key.lower() in FORBIDDEN_AUDIT_KEYS:
                raise UnsafeAuditPayloadError(
                    f"audit payload key {key!r} is not allowed"
                )
            _validate_redacted_value(nested_value)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _validate_redacted_value(item)


class ApplicationService:
    def transition(
        self,
        db: Session,
        *,
        task_id: str,
        expected_version: int,
        target: ApplicationTaskStatus,
        actor: TaskActor,
        event_type: str,
        redacted_payload: dict[str, object],
    ) -> ApplicationTask:
        _validate_redacted_value(redacted_payload)
        task = applications.get_authoritative(db, task_id, lock=True)
        if task is None:
            raise TaskNotFoundError(task_id)
        if task.state_version != expected_version:
            raise StaleTaskVersionError(task_id)
        if target not in ALLOWED_TRANSITIONS[task.status]:
            logger.warning("application transition rejected")
            raise InvalidTransitionError(
                f"transition from {task.status.value} to {target.value} is not allowed"
            )
        if (
            target is ApplicationTaskStatus.OBSERVING_USER_SUBMISSION
            and actor is not TaskActor.HUMAN
        ):
            logger.warning("application transition rejected")
            raise InvalidTransitionError(
                "only a human can start observation of the user's final submission"
            )
        return applications.transition(
            db,
            task_id=task_id,
            source=task.status,
            expected_version=expected_version,
            target=target,
            actor=actor,
            event_type=event_type,
            redacted_payload=redacted_payload,
        )


__all__ = [
    "ALLOWED_TRANSITIONS",
    "ApplicationService",
    "InvalidTransitionError",
    "StaleTaskVersionError",
    "TaskNotFoundError",
    "UnsafeAuditPayloadError",
]
