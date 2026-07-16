from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db.base import Base
from backend.app.db.models import (
    ApplicationTask,
    ApplicationTaskStatus,
    Device,
    DevicePlatform,
    DeviceStatus,
    User,
)
from backend.app.services.executor_tasks import (
    ExecutorTaskNotFoundError,
    ExecutorTaskService,
)


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
