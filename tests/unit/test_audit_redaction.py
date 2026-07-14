from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
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
    ApplicationService,
    UnsafeAuditPayloadError,
)


@pytest.fixture
def db() -> Iterator[Session]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session
    engine.dispose()


@pytest.fixture
def task(db: Session) -> ApplicationTask:
    user = User(account="audit-user", nickname="Audit User", password_hash="argon")
    db.add(user)
    db.flush()
    item = ApplicationTask(
        user_id=user.id,
        target_job_id="job-1",
        status=ApplicationTaskStatus.CREATED,
        state_version=0,
    )
    db.add(item)
    db.commit()
    return item


@pytest.mark.parametrize(
    "forbidden_key",
    [
        "password",
        "TOKEN",
        "Cookie",
        "captcha",
        "ID_CARD",
        "form_values",
        "resume_text",
    ],
)
def test_forbidden_audit_keys_are_rejected_recursively_and_case_insensitively(
    db: Session, task: ApplicationTask, forbidden_key: str
) -> None:
    with pytest.raises(UnsafeAuditPayloadError):
        ApplicationService().transition(
            db,
            task_id=task.id,
            expected_version=0,
            target=ApplicationTaskStatus.WAITING_FOR_DEVICE,
            actor=TaskActor.SYSTEM,
            event_type="device_requested",
            redacted_payload={"safe": [{"nested": {forbidden_key: "secret"}}]},
        )

    db.refresh(task)
    assert task.status is ApplicationTaskStatus.CREATED
    assert task.state_version == 0
    assert db.scalars(select(ApplicationEvent)).all() == []


def test_string_values_longer_than_500_characters_are_rejected_recursively(
    db: Session, task: ApplicationTask
) -> None:
    with pytest.raises(UnsafeAuditPayloadError):
        ApplicationService().transition(
            db,
            task_id=task.id,
            expected_version=0,
            target=ApplicationTaskStatus.WAITING_FOR_DEVICE,
            actor=TaskActor.SYSTEM,
            event_type="device_requested",
            redacted_payload={"safe": [{"nested": "x" * 501}]},
        )

    db.refresh(task)
    assert task.status is ApplicationTaskStatus.CREATED
    assert task.state_version == 0
    assert db.scalars(select(ApplicationEvent)).all() == []


def test_string_value_at_500_character_limit_is_allowed(
    db: Session, task: ApplicationTask
) -> None:
    updated = ApplicationService().transition(
        db,
        task_id=task.id,
        expected_version=0,
        target=ApplicationTaskStatus.WAITING_FOR_DEVICE,
        actor=TaskActor.SYSTEM,
        event_type="device_requested",
        redacted_payload={"summary": "x" * 500},
    )

    assert updated.state_version == 1
    event = db.scalar(select(ApplicationEvent))
    assert event is not None
    assert event.redacted_payload == {"summary": "x" * 500}
