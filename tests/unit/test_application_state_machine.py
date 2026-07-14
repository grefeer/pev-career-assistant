from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, delete, event, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

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
    TaskNotFoundError,
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
FORBIDDEN_EDGES = [
    (source, target)
    for source in ApplicationTaskStatus
    for target in ApplicationTaskStatus
    if target not in EXPECTED_TRANSITIONS[source]
]


@pytest.fixture
def session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'applications.db'}")
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


@pytest.mark.parametrize(("source", "target"), FORBIDDEN_EDGES)
def test_every_forbidden_transition_is_rejected_without_mutation(
    db: Session,
    application_service: ApplicationService,
    source: ApplicationTaskStatus,
    target: ApplicationTaskStatus,
) -> None:
    task = create_task(db, source)

    with pytest.raises(InvalidTransitionError):
        application_service.transition(
            db,
            task_id=task.id,
            expected_version=0,
            target=target,
            actor=TaskActor.HUMAN,
            event_type="forbidden_attempt",
            redacted_payload={},
        )

    db.refresh(task)
    assert task.status is source
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


def test_stale_identity_map_cannot_validate_against_old_status_with_current_version(
    session_factory: sessionmaker[Session], application_service: ApplicationService
) -> None:
    with session_factory() as setup:
        task = create_task(setup, ApplicationTaskStatus.CREATED)
        task_id = task.id

    with session_factory() as cached, session_factory() as writer:
        cached_task = cached.get(ApplicationTask, task_id)
        assert cached_task is not None
        application_service.transition(
            writer,
            task_id=task_id,
            expected_version=0,
            target=ApplicationTaskStatus.WAITING_FOR_DEVICE,
            actor=TaskActor.SYSTEM,
            event_type="device_requested",
            redacted_payload={},
        )
        writer.commit()

        with pytest.raises(InvalidTransitionError):
            application_service.transition(
                cached,
                task_id=task_id,
                expected_version=1,
                target=ApplicationTaskStatus.WAITING_FOR_DEVICE,
                actor=TaskActor.SYSTEM,
                event_type="false_self_transition",
                redacted_payload={},
            )

        cached.expire_all()
        authoritative = cached.get(ApplicationTask, task_id)
        assert authoritative is not None
        assert authoritative.status is ApplicationTaskStatus.WAITING_FOR_DEVICE
        assert authoritative.state_version == 1
        events = cached.scalars(select(ApplicationEvent)).all()
        assert len(events) == 1
        assert events[0].from_status == ApplicationTaskStatus.CREATED.value


def test_stale_version_is_reported_before_transition_legality(
    session_factory: sessionmaker[Session], application_service: ApplicationService
) -> None:
    with session_factory() as setup:
        task = create_task(setup, ApplicationTaskStatus.CREATED)
        task_id = task.id

    with session_factory() as cached, session_factory() as writer:
        assert cached.get(ApplicationTask, task_id) is not None
        application_service.transition(
            writer,
            task_id=task_id,
            expected_version=0,
            target=ApplicationTaskStatus.WAITING_FOR_DEVICE,
            actor=TaskActor.SYSTEM,
            event_type="device_requested",
            redacted_payload={},
        )
        writer.commit()

        with pytest.raises(StaleTaskVersionError):
            application_service.transition(
                cached,
                task_id=task_id,
                expected_version=0,
                target=ApplicationTaskStatus.CREATED,
                actor=TaskActor.SYSTEM,
                event_type="stale_invalid_attempt",
                redacted_payload={},
            )


def test_missing_task_raises_stable_not_found_error(
    db: Session, application_service: ApplicationService
) -> None:
    with pytest.raises(TaskNotFoundError, match="missing-task"):
        application_service.transition(
            db,
            task_id="missing-task",
            expected_version=0,
            target=ApplicationTaskStatus.WAITING_FOR_DEVICE,
            actor=TaskActor.SYSTEM,
            event_type="device_requested",
            redacted_payload={},
        )


def test_concurrent_delete_is_classified_as_not_found(
    session_factory: sessionmaker[Session], application_service: ApplicationService
) -> None:
    with session_factory() as setup:
        task = create_task(setup, ApplicationTaskStatus.CREATED)
        task_id = task.id

    with session_factory() as cached, session_factory() as deleter:
        assert cached.get(ApplicationTask, task_id) is not None
        deleter.execute(delete(ApplicationTask).where(ApplicationTask.id == task_id))
        deleter.commit()

        with pytest.raises(TaskNotFoundError, match=task_id):
            application_service.transition(
                cached,
                task_id=task_id,
                expected_version=0,
                target=ApplicationTaskStatus.WAITING_FOR_DEVICE,
                actor=TaskActor.SYSTEM,
                event_type="device_requested",
                redacted_payload={},
            )


def test_source_status_race_cannot_bypass_validated_source(
    session_factory: sessionmaker[Session], application_service: ApplicationService
) -> None:
    with session_factory() as setup:
        task = create_task(setup, ApplicationTaskStatus.CREATED)
        task_id = task.id

    engine = session_factory.kw["bind"]
    raced = False

    def change_status_before_update(
        _connection, _cursor, statement, _parameters, _context, _executemany
    ) -> None:
        nonlocal raced
        if raced or not statement.lstrip().upper().startswith(
            "UPDATE APPLICATION_TASKS"
        ):
            return
        raced = True
        with sqlite3.connect(engine.url.database) as raw:
            raw.execute(
                "UPDATE application_tasks SET status = ? WHERE id = ?",
                (ApplicationTaskStatus.WAITING_FOR_DEVICE.value, task_id),
            )

    event.listen(engine, "before_cursor_execute", change_status_before_update)
    try:
        with session_factory() as db:
            with pytest.raises(StaleTaskVersionError):
                application_service.transition(
                    db,
                    task_id=task_id,
                    expected_version=0,
                    target=ApplicationTaskStatus.WAITING_FOR_DEVICE,
                    actor=TaskActor.SYSTEM,
                    event_type="device_requested",
                    redacted_payload={},
                )
    finally:
        event.remove(engine, "before_cursor_execute", change_status_before_update)

    assert raced
    with session_factory() as verification:
        authoritative = verification.get(ApplicationTask, task_id)
        assert authoritative is not None
        assert authoritative.status is ApplicationTaskStatus.WAITING_FOR_DEVICE
        assert authoritative.state_version == 0
        assert verification.scalars(select(ApplicationEvent)).all() == []


def test_delete_between_validation_and_update_is_classified_as_not_found(
    session_factory: sessionmaker[Session], application_service: ApplicationService
) -> None:
    with session_factory() as setup:
        task = create_task(setup, ApplicationTaskStatus.CREATED)
        task_id = task.id

    engine = session_factory.kw["bind"]
    raced = False

    def delete_before_update(
        _connection, _cursor, statement, _parameters, _context, _executemany
    ) -> None:
        nonlocal raced
        if raced or not statement.lstrip().upper().startswith(
            "UPDATE APPLICATION_TASKS"
        ):
            return
        raced = True
        with sqlite3.connect(engine.url.database) as raw:
            raw.execute("DELETE FROM application_tasks WHERE id = ?", (task_id,))

    event.listen(engine, "before_cursor_execute", delete_before_update)
    try:
        with session_factory() as db:
            with pytest.raises(TaskNotFoundError, match=task_id):
                application_service.transition(
                    db,
                    task_id=task_id,
                    expected_version=0,
                    target=ApplicationTaskStatus.WAITING_FOR_DEVICE,
                    actor=TaskActor.SYSTEM,
                    event_type="device_requested",
                    redacted_payload={},
                )
    finally:
        event.remove(engine, "before_cursor_execute", delete_before_update)

    assert raced
    with session_factory() as verification:
        assert verification.get(ApplicationTask, task_id) is None
        assert verification.scalars(select(ApplicationEvent)).all() == []


def test_event_insert_failure_rolls_back_task_update_atomically(
    session_factory: sessionmaker[Session], application_service: ApplicationService
) -> None:
    with session_factory() as db:
        task = create_task(db, ApplicationTaskStatus.CREATED)
        task_id = task.id
        db.execute(
            text(
                """
                CREATE TRIGGER reject_application_event
                BEFORE INSERT ON application_events
                BEGIN
                    SELECT RAISE(ABORT, 'forced application event failure');
                END
                """
            )
        )
        db.commit()

        with pytest.raises(IntegrityError, match="forced application event failure"):
            application_service.transition(
                db,
                task_id=task_id,
                expected_version=0,
                target=ApplicationTaskStatus.WAITING_FOR_DEVICE,
                actor=TaskActor.SYSTEM,
                event_type="device_requested",
                redacted_payload={},
            )
        db.rollback()

    with session_factory() as verification:
        authoritative = verification.get(ApplicationTask, task_id)
        assert authoritative is not None
        assert authoritative.status is ApplicationTaskStatus.CREATED
        assert authoritative.state_version == 0
        assert verification.scalars(select(ApplicationEvent)).all() == []
