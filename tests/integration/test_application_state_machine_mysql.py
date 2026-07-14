from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from backend.app.db.base import Base
from backend.app.db.models import (
    ApplicationEvent,
    ApplicationTask,
    ApplicationTaskStatus,
    TaskActor,
    User,
)
from backend.app.services.applications import ApplicationService, InvalidTransitionError


@pytest.mark.skipif(
    "TEST_MYSQL_URL" not in os.environ, reason="requires TEST_MYSQL_URL"
)
def test_repeatable_read_uses_current_task_state_not_cached_snapshot() -> None:
    engine = create_engine(os.environ["TEST_MYSQL_URL"])
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    service = ApplicationService()

    try:
        with Session(engine, expire_on_commit=False) as setup:
            user = User(
                account="mysql-state-user",
                nickname="MySQL State User",
                password_hash="argon",
            )
            setup.add(user)
            setup.flush()
            task = ApplicationTask(
                user_id=user.id,
                target_job_id="mysql-job-1",
                status=ApplicationTaskStatus.CREATED,
                state_version=0,
            )
            setup.add(task)
            setup.commit()
            task_id = task.id

        with (
            Session(engine, expire_on_commit=False) as cached,
            Session(engine, expire_on_commit=False) as writer,
        ):
            isolation = cached.scalar(text("SELECT @@transaction_isolation"))
            assert str(isolation).replace("-", " ").upper() == "REPEATABLE READ"
            cached_task = cached.get(ApplicationTask, task_id)
            assert cached_task is not None
            assert cached_task.status is ApplicationTaskStatus.CREATED
            assert cached_task.state_version == 0

            service.transition(
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
                service.transition(
                    cached,
                    task_id=task_id,
                    expected_version=1,
                    target=ApplicationTaskStatus.WAITING_FOR_DEVICE,
                    actor=TaskActor.SYSTEM,
                    event_type="false_self_transition",
                    redacted_payload={},
                )
            cached.rollback()

        with Session(engine) as verification:
            authoritative = verification.get(ApplicationTask, task_id)
            assert authoritative is not None
            assert authoritative.status is ApplicationTaskStatus.WAITING_FOR_DEVICE
            assert authoritative.state_version == 1
            event_count = verification.scalar(
                select(func.count()).select_from(ApplicationEvent)
            )
            assert event_count == 1
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()
