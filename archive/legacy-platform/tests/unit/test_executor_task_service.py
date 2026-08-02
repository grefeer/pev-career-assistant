from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db.base import Base
from backend.app.db.models import (
    ApplicationEvent,
    ApplicationTask,
    ApplicationTaskStatus,
    Device,
    DevicePlatform,
    DeviceStatus,
    TaskActor,
    User,
)
from backend.app.services.applications import InvalidTransitionError
from backend.app.services.executor_tasks import (
    ExecutorTaskNotFoundError,
    ExecutorTaskService,
)
from backend.app.repositories import executor_tasks


@pytest.fixture
def db() -> Session:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as value:
        yield value


def create_user_and_device(db: Session, account: str) -> tuple[User, Device]:
    user = User(account=account, nickname=account, password_hash="hash")
    db.add(user)
    db.flush()
    device = Device(
        user_id=user.id,
        name=f"{account}-windows",
        platform=DevicePlatform.WINDOWS,
        status=DeviceStatus.ACTIVE,
        token_hash=f"hash-{account}",
        public_key_pem="test-public-key",
    )
    db.add(device)
    db.commit()
    return user, device


@pytest.fixture
def alice_user(db: Session) -> User:
    return create_user_and_device(db, "alice")[0]


@pytest.fixture
def alice_device(db: Session, alice_user: User) -> Device:
    return db.query(Device).filter(Device.user_id == alice_user.id).one()


@pytest.fixture
def bob_device(db: Session) -> Device:
    return create_user_and_device(db, "bob")[1]


def test_list_only_returns_tasks_assigned_to_authenticated_device(
    db, alice_device, bob_device, alice_user
) -> None:
    own = ApplicationTask(
        user_id=alice_user.id,
        target_job_id="simulation-job",
        device_id=alice_device.id,
        status=ApplicationTaskStatus.DISPATCHED,
    )
    other = ApplicationTask(
        user_id=bob_device.user_id,
        target_job_id="other-job",
        device_id=bob_device.id,
        status=ApplicationTaskStatus.DISPATCHED,
    )
    db.add_all([own, other])
    db.commit()

    listed = ExecutorTaskService().list_assigned(db, device=alice_device)

    assert [item.id for item in listed] == [own.id]


def test_detail_hides_task_owned_by_another_device(
    db, alice_device, bob_device
) -> None:
    task = ApplicationTask(
        user_id=bob_device.user_id,
        target_job_id="other-job",
        device_id=bob_device.id,
        status=ApplicationTaskStatus.DISPATCHED,
    )
    db.add(task)
    db.commit()

    with pytest.raises(ExecutorTaskNotFoundError):
        ExecutorTaskService().get_assigned(
            db, device=alice_device, task_id=task.id
        )


def test_progress_uses_executor_actor_and_appends_only_redacted_counts(
    db, alice_device, alice_user
) -> None:
    task = ApplicationTask(
        user_id=alice_user.id,
        target_job_id="simulation-job",
        device_id=alice_device.id,
        status=ApplicationTaskStatus.DISPATCHED,
    )
    db.add(task)
    db.commit()

    updated = ExecutorTaskService().report_progress(
        db,
        device=alice_device,
        task_id=task.id,
        expected_version=0,
        target=ApplicationTaskStatus.RUNNING,
        page_fingerprint="sha256:abc123",
        page_index=1,
        reason_code=None,
        field_counts={"confirmed": 1, "defaulted": 0, "missing": 1, "low": 0},
    )
    event = db.scalar(select(ApplicationEvent).where(ApplicationEvent.task_id == task.id))
    assert updated.status is ApplicationTaskStatus.RUNNING
    assert event.actor is TaskActor.EXECUTOR
    assert event.redacted_payload == {
        "page_fingerprint": "sha256:abc123",
        "page_index": 1,
        "reason_code": "",
        "confirmed_count": 1,
        "defaulted_count": 0,
        "missing_count": 1,
        "low_confidence_count": 0,
    }


def test_executor_result_is_rejected_until_human_started_observation(
    db, alice_device, alice_user
) -> None:
    task = ApplicationTask(
        user_id=alice_user.id,
        target_job_id="simulation-job",
        device_id=alice_device.id,
        status=ApplicationTaskStatus.READY_FOR_REVIEW,
    )
    db.add(task)
    db.commit()

    with pytest.raises(InvalidTransitionError):
        ExecutorTaskService().report_result(
            db,
            device=alice_device,
            task_id=task.id,
            expected_version=0,
            target=ApplicationTaskStatus.SUBMITTED_SUCCESS,
            page_fingerprint="sha256:result",
            reason_code="success_marker",
        )


def test_progress_rechecks_device_assignment_under_authoritative_lock(
    db, alice_device, bob_device, alice_user, monkeypatch
) -> None:
    task = ApplicationTask(
        user_id=alice_user.id,
        target_job_id="simulation-job",
        device_id=alice_device.id,
        status=ApplicationTaskStatus.DISPATCHED,
    )
    db.add(task)
    db.commit()
    original_get_assigned = executor_tasks.get_assigned

    def reassign_after_initial_check(*args, **kwargs):
        checked = original_get_assigned(*args, **kwargs)
        assert checked is not None
        checked.device_id = bob_device.id
        checked.user_id = bob_device.user_id
        db.commit()
        return checked

    monkeypatch.setattr(executor_tasks, "get_assigned", reassign_after_initial_check)

    with pytest.raises(ExecutorTaskNotFoundError):
        ExecutorTaskService().report_progress(
            db,
            device=alice_device,
            task_id=task.id,
            expected_version=0,
            target=ApplicationTaskStatus.RUNNING,
            page_fingerprint="sha256:abc123",
            page_index=1,
            reason_code=None,
            field_counts={"confirmed": 0, "defaulted": 0, "missing": 0, "low": 0},
        )

    db.refresh(task)
    assert task.status is ApplicationTaskStatus.DISPATCHED
    assert db.scalar(
        select(ApplicationEvent).where(ApplicationEvent.task_id == task.id)
    ) is None
