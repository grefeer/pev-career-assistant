from __future__ import annotations

from collections.abc import Mapping
from datetime import timezone
import logging
import re

from sqlalchemy.orm import Session

from backend.app.db.base import utc_now
from backend.app.db.models import (
    ApplicationTask,
    ApplicationTaskStatus,
    TaskActor,
)
from backend.app.repositories import applications
from backend.app.repositories import devices as devices_repo
from backend.app.repositories.applications import (
    StaleTaskVersionError,
    TaskNotFoundError,
)
from backend.app.repositories.snapshots import get_by_id as get_snapshot_by_id
from backend.app.services.task_eligibility_service import check_task_eligibility


class InvalidTransitionError(ValueError):
    pass


class UnsafeAuditPayloadError(ValueError):
    pass


logger = logging.getLogger(__name__)


S = ApplicationTaskStatus
A = TaskActor
ALLOWED_TRANSITION_ACTORS = {
    (S.CREATED, S.WAITING_FOR_DEVICE): {A.SYSTEM},
    (S.CREATED, S.CANCELLED): {A.HUMAN},
    (S.WAITING_FOR_DEVICE, S.DISPATCHED): {A.SYSTEM},
    (S.WAITING_FOR_DEVICE, S.CANCELLED): {A.HUMAN},
    (S.DISPATCHED, S.RUNNING): {A.EXECUTOR},
    (S.DISPATCHED, S.WAITING_FOR_HUMAN): {A.EXECUTOR},
    (S.DISPATCHED, S.FAILED): {A.EXECUTOR},
    (S.DISPATCHED, S.CANCELLED): {A.HUMAN},
    (S.RUNNING, S.WAITING_FOR_HUMAN): {A.EXECUTOR},
    (S.RUNNING, S.READY_FOR_REVIEW): {A.EXECUTOR},
    (S.RUNNING, S.FAILED): {A.EXECUTOR},
    (S.RUNNING, S.CANCELLED): {A.HUMAN},
    (S.WAITING_FOR_HUMAN, S.RUNNING): {A.EXECUTOR},
    (S.WAITING_FOR_HUMAN, S.READY_FOR_REVIEW): {A.EXECUTOR},
    (S.WAITING_FOR_HUMAN, S.FAILED): {A.EXECUTOR},
    (S.WAITING_FOR_HUMAN, S.CANCELLED): {A.HUMAN},
    (S.READY_FOR_REVIEW, S.OBSERVING_USER_SUBMISSION): {A.HUMAN},
    (S.READY_FOR_REVIEW, S.CANCELLED): {A.HUMAN},
    (S.OBSERVING_USER_SUBMISSION, S.SUBMITTED_SUCCESS): {A.EXECUTOR},
    (S.OBSERVING_USER_SUBMISSION, S.SUBMITTED_FAILED): {A.EXECUTOR},
    (S.OBSERVING_USER_SUBMISSION, S.RESULT_UNKNOWN): {A.EXECUTOR},
}
ALLOWED_TRANSITIONS = {
    source: {
        target
        for (edge_source, target) in ALLOWED_TRANSITION_ACTORS
        if edge_source is source
    }
    for source in ApplicationTaskStatus
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
FORBIDDEN_AUDIT_KEY_PARTS = {
    "authorization",
    "credential",
    "secret",
    "cookie",
    "token",
    "password",
    "captcha",
    "otp",
    "verificationcode",
    "pairingcode",
    "idcard",
    "formvalues",
    "resumetext",
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
            normalized = (
                re.sub(r"[^a-z0-9]", "", key.lower()) if isinstance(key, str) else ""
            )
            if isinstance(key, str) and (
                key.lower() in FORBIDDEN_AUDIT_KEYS
                or any(part in normalized for part in FORBIDDEN_AUDIT_KEY_PARTS)
            ):
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
        required_device_id: str | None = None,
        required_user_id: str | None = None,
    ) -> ApplicationTask:
        _validate_redacted_value(redacted_payload)
        task = applications.get_authoritative(db, task_id, lock=True)
        if task is None:
            raise TaskNotFoundError(task_id)
        if (
            required_device_id is not None
            and task.device_id != required_device_id
        ) or (
            required_user_id is not None
            and task.user_id != required_user_id
        ):
            raise TaskNotFoundError(task_id)
        if task.state_version != expected_version:
            raise StaleTaskVersionError(task_id)
        allowed_actors = ALLOWED_TRANSITION_ACTORS.get((task.status, target))
        if allowed_actors is None:
            logger.warning("application transition rejected")
            raise InvalidTransitionError(
                f"transition from {task.status.value} to {target.value} is not allowed"
            )
        if actor not in allowed_actors:
            logger.warning("application transition rejected")
            raise InvalidTransitionError(
                f"actor {actor.value} is not allowed for transition "
                f"from {task.status.value} to {target.value}"
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


# ---------------------------------------------------------------------------
# assign_and_dispatch_task — convenience for the dispatch endpoint
# ---------------------------------------------------------------------------


def assign_and_dispatch_task(
    db: Session,
    user_id: str,
    task_id: str,
    device_id: str,
    expected_version: int,
) -> ApplicationTask:
    """Assign an existing ``CREATED`` task to *device_id* and dispatch it.

    Performs two explicit state-machine transitions:
        1. ``CREATED`` -> ``WAITING_FOR_DEVICE``  (actor: SYSTEM,
           event: device_requested)
        2. ``WAITING_FOR_DEVICE`` -> ``DISPATCHED`` (actor: SYSTEM,
           event: device_dispatched)

    Each transition validates ``state_version``; the second uses the version
    returned by the first.  Device ownership and eligibility are re-checked
    before the first transition.  The device is bound to the task between
    the two transitions.  Both transitions share a single database
    transaction -- if the second fails, the full operation rolls back.

    There is deliberately no ``CREATED`` -> ``DISPATCHED`` shortcut; the task
    always passes through ``WAITING_FOR_DEVICE``.

    Returns:
        The task in ``DISPATCHED`` status.

    Raises:
        TaskNotFoundError: Task does not exist or does not belong to
            *user_id*.
        StaleTaskVersionError: ``expected_version`` does not match.
        InvalidTransitionError: Task is not in ``CREATED`` status.
        ValueError: Snapshot not found, eligibility check failed, or
            device is not available (not found, not active, expired, or
            not owned by *user_id*).
    """
    # ── 1. Load task with row-level lock, verify ownership + snapshot ────
    task = applications.get_authoritative(db, task_id, lock=True)
    if task is None or task.user_id != user_id:
        raise TaskNotFoundError(task_id)

    snapshot = (
        get_snapshot_by_id(db, task.snapshot_id, user_id)
        if task.snapshot_id is not None
        else None
    )
    if snapshot is None:
        raise ValueError("snapshot_not_found")

    # ── 2. Re-check eligibility via TaskEligibilityService ─────────────
    can_create, reason = check_task_eligibility(db, user_id, task.snapshot_id)
    if not can_create:
        raise ValueError(f"task_not_eligible: {reason}")

    # ── 3. Validate device: active, not expired, owned by user ─────────
    device = devices_repo.get_active_owned(
        db, user_id=user_id, device_id=device_id
    )
    if device is None:
        raise ValueError("device_not_available")

    expires_at = device.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= utc_now():
        raise ValueError("device_not_available")

    svc = ApplicationService()

    # ── 4. Transition CREATED -> WAITING_FOR_DEVICE ────────────────────
    task = svc.transition(
        db,
        task_id=task_id,
        expected_version=expected_version,
        target=ApplicationTaskStatus.WAITING_FOR_DEVICE,
        actor=TaskActor.SYSTEM,
        event_type="device_requested",
        redacted_payload={"device_id": device_id},
        required_user_id=user_id,
    )

    # ── 5. Bind device to task ─────────────────────────────────────────
    task.device_id = device_id
    db.flush()

    # ── 6. Transition WAITING_FOR_DEVICE -> DISPATCHED ─────────────────
    task = svc.transition(
        db,
        task_id=task_id,
        expected_version=task.state_version,
        target=ApplicationTaskStatus.DISPATCHED,
        actor=TaskActor.SYSTEM,
        event_type="device_dispatched",
        redacted_payload={"device_id": device_id},
        required_user_id=user_id,
        required_device_id=device_id,
    )

    db.commit()
    return task


__all__ = [
    "ALLOWED_TRANSITIONS",
    "ALLOWED_TRANSITION_ACTORS",
    "ApplicationService",
    "InvalidTransitionError",
    "StaleTaskVersionError",
    "TaskNotFoundError",
    "UnsafeAuditPayloadError",
    "assign_and_dispatch_task",
]
