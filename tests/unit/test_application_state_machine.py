from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db.base import Base
from backend.app.db.models import (
    ApplicationEvent,
    ApplicationTask,
    ApplicationTaskStatus,
    TaskActor,
    User,
)
from backend.app.services.applications import (
    ALLOWED_TRANSITIONS,
    ApplicationService,
    InvalidTransitionError,
    StaleTaskVersionError,
)


EXPECTED_TRANSITIONS = {
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
TERMINAL_STATUSES = {
    ApplicationTaskStatus.SUBMITTED_SUCCESS,
    ApplicationTaskStatus.SUBMITTED_FAILED,
    ApplicationTaskStatus.RESULT_UNKNOWN,
    ApplicationTaskStatus.FAILED,
    ApplicationTaskStatus.CANCELLED,
}
ALLOWED_EDGES = [
    (source, target)
    for source, targets in EXPECTED_TRANSITIONS.items()
    for target in targets
]


@pytest.fixture
def session_factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    yield factory
    engine.dispose()


@pytest.fixture
def db(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_factory() as session:
        yield session


@pytest.fixture
def application_service() -> ApplicationService:
    return ApplicationService()


def create_task(db: Session, status: ApplicationTaskStatus) -> ApplicationTask:
    user = User(
        account=f"user-{status.value}-{id(db)}",
        nickname="Test User",
        password_hash="argon",
    )
    db.add(user)
    db.flush()
    task = ApplicationTask(
        user_id=user.id,
        target_job_id="job-1",
        status=status,
        state_version=0,
    )
    db.add(task)
    db.commit()
    return task


@pytest.fixture
def ready_task(db: Session) -> ApplicationTask:
    return create_task(db, ApplicationTaskStatus.READY_FOR_REVIEW)


def actor_for(target: ApplicationTaskStatus) -> TaskActor:
    if target is ApplicationTaskStatus.OBSERVING_USER_SUBMISSION:
        return TaskActor.HUMAN
    return TaskActor.SYSTEM


def test_transition_matrix_is_explicit_and_complete() -> None:
    assert ALLOWED_TRANSITIONS == EXPECTED_TRANSITIONS


@pytest.mark.parametrize(("source", "target"), ALLOWED_EDGES)
def test_every_allowed_transition_updates_once_and_appends_event(
    db: Session,
    application_service: ApplicationService,
    source: ApplicationTaskStatus,
    target: ApplicationTaskStatus,
) -> None:
    task = create_task(db, source)

    updated = application_service.transition(
        db,
        task_id=task.id,
        expected_version=0,
        target=target,
        actor=actor_for(target),
        event_type="state_changed",
        redacted_payload={"result": "summary"},
    )

    event = db.scalar(
        select(ApplicationEvent).where(ApplicationEvent.task_id == task.id)
    )
    assert updated.status is target
    assert updated.state_version == 1
    assert event is not None
    assert event.actor is actor_for(target)
    assert event.event_type == "state_changed"
    assert event.from_status == source.value
    assert event.to_status == target.value
    assert event.redacted_payload == {"result": "summary"}


@pytest.mark.parametrize("terminal", TERMINAL_STATUSES)
def test_every_terminal_status_rejects_all_outgoing_transitions(
    db: Session,
    application_service: ApplicationService,
    terminal: ApplicationTaskStatus,
) -> None:
    task = create_task(db, terminal)

    with pytest.raises(InvalidTransitionError):
        application_service.transition(
            db,
            task_id=task.id,
            expected_version=0,
            target=ApplicationTaskStatus.RUNNING,
            actor=TaskActor.SYSTEM,
            event_type="invalid_attempt",
            redacted_payload={},
        )

    db.refresh(task)
    assert task.status is terminal
    assert task.state_version == 0
    assert db.scalars(select(ApplicationEvent)).all() == []


def test_executor_cannot_start_final_submission(
    application_service: ApplicationService, db: Session, ready_task: ApplicationTask
) -> None:
    with pytest.raises(InvalidTransitionError, match="human"):
        application_service.transition(
            db,
            task_id=ready_task.id,
            expected_version=ready_task.state_version,
            target=ApplicationTaskStatus.OBSERVING_USER_SUBMISSION,
            actor=TaskActor.EXECUTOR,
            event_type="submission_started",
            redacted_payload={},
        )

    db.refresh(ready_task)
    assert ready_task.status is ApplicationTaskStatus.READY_FOR_REVIEW
    assert ready_task.state_version == 0
    assert db.scalars(select(ApplicationEvent)).all() == []


def test_system_cannot_start_final_submission(
    application_service: ApplicationService, db: Session, ready_task: ApplicationTask
) -> None:
    with pytest.raises(InvalidTransitionError, match="human"):
        application_service.transition(
            db,
            task_id=ready_task.id,
            expected_version=0,
            target=ApplicationTaskStatus.OBSERVING_USER_SUBMISSION,
            actor=TaskActor.SYSTEM,
            event_type="submission_started",
            redacted_payload={},
        )


def test_human_can_start_observation_and_executor_can_report_result(
    application_service: ApplicationService, db: Session, ready_task: ApplicationTask
) -> None:
    observing = application_service.transition(
        db,
        task_id=ready_task.id,
        expected_version=0,
        target=ApplicationTaskStatus.OBSERVING_USER_SUBMISSION,
        actor=TaskActor.HUMAN,
        event_type="user_clicked_submit",
        redacted_payload={"page_kind": "final_review"},
    )
    completed = application_service.transition(
        db,
        task_id=ready_task.id,
        expected_version=observing.state_version,
        target=ApplicationTaskStatus.SUBMITTED_SUCCESS,
        actor=TaskActor.EXECUTOR,
        event_type="submission_result_observed",
        redacted_payload={"result": "success"},
    )

    assert completed.status is ApplicationTaskStatus.SUBMITTED_SUCCESS
    assert completed.state_version == 2


def test_stale_session_cannot_overwrite_authoritative_state(
    session_factory: sessionmaker[Session], application_service: ApplicationService
) -> None:
    with session_factory() as setup:
        task = create_task(setup, ApplicationTaskStatus.CREATED)
        task_id = task.id

    with session_factory() as first, session_factory() as stale:
        first_task = first.get(ApplicationTask, task_id)
        stale_task = stale.get(ApplicationTask, task_id)
        assert first_task is not None and stale_task is not None
        application_service.transition(
            first,
            task_id=task_id,
            expected_version=first_task.state_version,
            target=ApplicationTaskStatus.WAITING_FOR_DEVICE,
            actor=TaskActor.SYSTEM,
            event_type="device_requested",
            redacted_payload={},
        )
        first.commit()

        with pytest.raises(StaleTaskVersionError):
            application_service.transition(
                stale,
                task_id=task_id,
                expected_version=stale_task.state_version,
                target=ApplicationTaskStatus.CANCELLED,
                actor=TaskActor.HUMAN,
                event_type="cancelled",
                redacted_payload={},
            )

        stale.expire_all()
        authoritative = stale.get(ApplicationTask, task_id)
        assert authoritative is not None
        assert authoritative.status is ApplicationTaskStatus.WAITING_FOR_DEVICE
        assert authoritative.state_version == 1
        assert len(stale.scalars(select(ApplicationEvent)).all()) == 1
