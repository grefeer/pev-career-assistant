from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json

from sqlalchemy.orm import Session

from backend.app.db.base import utc_now
from backend.app.db.models import JobFeedback, JobFeedbackEvent, User
from backend.app.domain.job_feedback import (
    ADMIN_TRANSITIONS,
    FEEDBACK_NOTE_MAX_LENGTH,
    STUDENT_WITHDRAW_FROM,
    FeedbackAdminDecision,
    FeedbackStudentAction,
    JobFeedbackAction,
    JobFeedbackCategory,
    JobFeedbackStatus,
)
from backend.app.repositories import job_feedback as repository


class FeedbackNotFoundError(LookupError):
    pass


class FeedbackJobNotFoundError(LookupError):
    pass


class StaleFeedbackError(RuntimeError):
    pass


class InvalidFeedbackTransitionError(ValueError):
    pass


class InvalidFeedbackNoteError(ValueError):
    pass


class IdempotencyKeyConflictError(RuntimeError):
    pass


@dataclass(frozen=True)
class FeedbackMutationResult:
    id: str
    job_id: str
    category: JobFeedbackCategory
    status: JobFeedbackStatus
    version: int
    updated_at: datetime


def _normalise_note(note: str | None) -> str | None:
    value = note.strip() if note is not None else ""
    if len(value) > FEEDBACK_NOTE_MAX_LENGTH:
        raise InvalidFeedbackNoteError
    return value or None


def _fingerprint(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _result(feedback: JobFeedback, updated_at: datetime) -> FeedbackMutationResult:
    return FeedbackMutationResult(
        id=feedback.id,
        job_id=feedback.job_id,
        category=feedback.category,
        status=feedback.status,
        version=feedback.version,
        updated_at=_utc(updated_at),
    )


def _snapshot(fingerprint: str, result: FeedbackMutationResult) -> dict[str, object]:
    response = asdict(result)
    response["category"] = result.category.value
    response["status"] = result.status.value
    response["updated_at"] = result.updated_at.isoformat()
    return {"request_fingerprint": fingerprint, "response": response}


def _replay(event: JobFeedbackEvent, fingerprint: str) -> FeedbackMutationResult:
    if event.redacted_snapshot.get("request_fingerprint") != fingerprint:
        raise IdempotencyKeyConflictError
    response = event.redacted_snapshot.get("response")
    if not isinstance(response, dict):
        raise RuntimeError("invalid feedback event response snapshot")
    return FeedbackMutationResult(
        id=str(response["id"]),
        job_id=str(response["job_id"]),
        category=JobFeedbackCategory(str(response["category"])),
        status=JobFeedbackStatus(str(response["status"])),
        version=int(response["version"]),
        updated_at=_utc(datetime.fromisoformat(str(response["updated_at"]))),
    )


class JobFeedbackService:
    def __init__(self, *, now: Callable[[], datetime] = utc_now) -> None:
        self._now = now

    @staticmethod
    def _lock_actor(db: Session, actor_user_id: str) -> None:
        db.get(User, actor_user_id, with_for_update=True)

    def mutate_student(
        self,
        db: Session,
        *,
        job_id: str,
        actor_user_id: str,
        idempotency_key: str,
        action: FeedbackStudentAction,
        category: JobFeedbackCategory,
        expected_version: int | None,
        note: str | None,
    ) -> FeedbackMutationResult:
        normalized_note = _normalise_note(note)
        fingerprint = _fingerprint(
            {
                "operation": "student_mutation",
                "job_id": job_id,
                "action": action.value,
                "category": category.value,
                "expected_version": expected_version,
                "note": normalized_note,
            }
        )
        if repository.lock_verified_job(db, job_id) is None:
            raise FeedbackJobNotFoundError
        self._lock_actor(db, actor_user_id)
        prior_event = repository.lock_actor_event(
            db, actor_user_id=actor_user_id, idempotency_key=idempotency_key
        )
        if prior_event is not None:
            return _replay(prior_event, fingerprint)
        feedback = repository.lock_user_feedback(
            db, user_id=actor_user_id, job_id=job_id, category=category
        )
        previous_status = feedback.status if feedback is not None else None
        if action is FeedbackStudentAction.UPSERT:
            if feedback is None:
                if expected_version is not None:
                    raise StaleFeedbackError
                feedback = JobFeedback(
                    user_id=actor_user_id,
                    job_id=job_id,
                    category=category,
                    status=JobFeedbackStatus.OPEN,
                    note=normalized_note,
                    version=1,
                )
                db.add(feedback)
                event_action = JobFeedbackAction.SUBMITTED
            else:
                if feedback.version != expected_version:
                    raise StaleFeedbackError
                feedback.note = normalized_note
                feedback.status = JobFeedbackStatus.OPEN
                feedback.version += 1
                event_action = JobFeedbackAction.UPDATED
        else:
            if feedback is None:
                raise FeedbackNotFoundError
            if feedback.version != expected_version:
                raise StaleFeedbackError
            if feedback.status not in STUDENT_WITHDRAW_FROM:
                raise InvalidFeedbackTransitionError
            feedback.status = JobFeedbackStatus.WITHDRAWN
            feedback.version += 1
            event_action = JobFeedbackAction.WITHDRAWN
        changed_at = self._now()
        feedback.updated_at = changed_at
        db.flush()
        result = _result(feedback, changed_at)
        db.add(
            JobFeedbackEvent(
                feedback_id=feedback.id,
                actor_user_id=actor_user_id,
                action=event_action,
                from_status=previous_status.value if previous_status else None,
                to_status=feedback.status.value,
                feedback_version=feedback.version,
                redacted_snapshot=_snapshot(fingerprint, result),
                idempotency_key=idempotency_key,
                created_at=changed_at,
            )
        )
        db.flush()
        return result

    def decide_admin(
        self,
        db: Session,
        *,
        feedback_id: str,
        actor_user_id: str,
        idempotency_key: str,
        decision: FeedbackAdminDecision,
        expected_version: int,
    ) -> FeedbackMutationResult:
        fingerprint = _fingerprint(
            {
                "operation": "admin_decision",
                "feedback_id": feedback_id,
                "decision": decision.value,
                "expected_version": expected_version,
            }
        )
        feedback = repository.lock_feedback(db, feedback_id)
        if feedback is None:
            raise FeedbackNotFoundError
        self._lock_actor(db, actor_user_id)
        prior_event = repository.lock_actor_event(
            db, actor_user_id=actor_user_id, idempotency_key=idempotency_key
        )
        if prior_event is not None:
            return _replay(prior_event, fingerprint)
        if feedback.version != expected_version:
            raise StaleFeedbackError
        allowed, target, event_action = ADMIN_TRANSITIONS[decision]
        if feedback.status not in allowed:
            raise InvalidFeedbackTransitionError
        previous = feedback.status
        changed_at = self._now()
        feedback.status = target
        feedback.version += 1
        feedback.updated_at = changed_at
        result = _result(feedback, changed_at)
        db.add(
            JobFeedbackEvent(
                feedback_id=feedback.id,
                actor_user_id=actor_user_id,
                action=event_action,
                from_status=previous.value,
                to_status=target.value,
                feedback_version=feedback.version,
                redacted_snapshot=_snapshot(fingerprint, result),
                idempotency_key=idempotency_key,
                created_at=changed_at,
            )
        )
        db.flush()
        return result
