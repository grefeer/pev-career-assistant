from __future__ import annotations

from collections.abc import Iterator
import os

import fakeredis
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker  # noqa: F811
from sqlalchemy.pool import StaticPool

from backend.app.api import dependencies
from backend.app.api.executor_schemas import (
    ExecutorProgressRequest,
    ExecutorResultRequest,
    ExecutorTaskPayload,
    ExecutorTaskState,
    ExecutorTaskSummary,
)
from backend.app.config import Settings
from backend.app.db.base import Base
from backend.app.db.models import (
    ApplicationTask,
    ApplicationTaskStatus,
    Device,
)
from backend.app.services.executor_tasks import ExecutorPayloadProvider

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("APP_AUTH_SECRET", "test-secret-with-at-least-32-characters")
os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")
os.environ.setdefault(
    "OBJECT_ENCRYPTION_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
)

from backend.app.main import create_app

PUBLIC_KEY = "-----BEGIN PUBLIC KEY-----\nwindows-test\n-----END PUBLIC KEY-----"


class FixturePayloadProvider:
    """Test provider that returns the fixture payload for any task."""

    def __init__(self, fixture: ExecutorTaskPayload) -> None:
        self.fixture = fixture

    def payload_for(self, task: ApplicationTask) -> ExecutorTaskPayload:
        return self.fixture


@pytest.fixture
def settings() -> Settings:
    return Settings(
        app_env="test",
        app_auth_secret="test-secret-with-at-least-32-characters",
        object_encryption_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        database_url="sqlite+pysqlite:///:memory:",
        redis_url="redis://localhost:6379/15",
        checkpoint_backend="sqlite",
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    redis = fakeredis.FakeRedis()

    def override_db() -> Iterator[Session]:
        with session_factory() as db:
            yield db

    app = create_app(settings)
    app.dependency_overrides[dependencies._get_db] = override_db
    app.dependency_overrides[dependencies.get_redis] = lambda: redis
    with TestClient(app) as value:
        value.session_factory = session_factory  # type: ignore[attr-defined]
        yield value


def register(client: TestClient, account: str) -> str:
    response = client.post(
        "/api/auth/register",
        json={"account": account, "nickname": account, "password": "secret12"},
    )
    assert response.status_code == 200
    return response.json()["token"]


def pair(client: TestClient, user_token: str) -> dict[str, object]:
    ticket = client.post(
        "/api/devices/pairing-tickets",
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert ticket.status_code == 200
    response = client.post(
        "/api/devices/pair",
        json={
            "code": ticket.json()["code"],
            "name": "Alice Windows",
            "public_key_pem": PUBLIC_KEY,
        },
    )
    assert response.status_code == 200
    return response.json()


def issue_lease(client: TestClient, device_token: str, task_id: str) -> str:
    response = client.post(
        "/api/devices/task-lease",
        headers={"X-Device-Token": device_token},
        json={"task_id": task_id},
    )
    assert response.status_code == 200
    return response.json()["lease"]


@pytest.fixture
def paired_device(client: TestClient) -> dict[str, object]:
    token = register(client, "alice")
    return pair(client, token)


@pytest.fixture
def seeded_task(client: TestClient, paired_device: dict[str, object]) -> Iterator[ApplicationTask]:
    with client.session_factory() as session:  # type: ignore[attr-defined]
        device = session.query(Device).filter(
            Device.id == paired_device["device"]["id"]
        ).one()
        task = ApplicationTask(
            user_id=device.user_id,
            target_job_id="simulation-job",
            device_id=device.id,
            status=ApplicationTaskStatus.DISPATCHED,
        )
        session.add(task)
        session.commit()
        session.refresh(task)
        yield task


@pytest.fixture
def payload_provider(
    seeded_task: ApplicationTask,
) -> ExecutorPayloadProvider:
    fixture = ExecutorTaskPayload(
        task_id=seeded_task.id,
        state_version=seeded_task.state_version,
        target_url="http://127.0.0.1:8765/single-page",
        fields=[
            {
                "field_key": "full_name",
                "label": "\u59d3\u540d",
                "value": "Alice Example",
                "confidence": "confirmed",
                "required": True,
                "sensitive": False,
            },
        ],
    )
    return FixturePayloadProvider(fixture)


def test_executor_list_is_device_isolated_and_detail_requires_matching_lease(
    client, paired_device, seeded_task, payload_provider
) -> None:
    # Override the executor payload provider in app state
    client.app.state.executor_payload_provider = payload_provider  # type: ignore[attr-defined]

    headers = {"X-Device-Token": paired_device["device_token"]}
    listed = client.get("/api/executor/tasks", headers=headers)
    assert listed.status_code == 200
    assert [item["task_id"] for item in listed.json()["tasks"]] == [seeded_task.id]
    assert "payload" not in listed.text

    no_lease = client.get(
        f"/api/executor/tasks/{seeded_task.id}", headers=headers
    )
    assert no_lease.status_code == 401

    lease = issue_lease(client, paired_device["device_token"], seeded_task.id)
    detail = client.get(
        f"/api/executor/tasks/{seeded_task.id}",
        headers={
            **headers,
            "X-Task-ID": seeded_task.id,
            "X-Task-Lease": lease,
        },
    )
    assert detail.status_code == 200
    assert detail.json()["payload"]["protocol_version"] == "executor.v1"


def test_progress_without_valid_lease_returns_401(
    client, paired_device, seeded_task, payload_provider
) -> None:
    client.app.state.executor_payload_provider = payload_provider
    headers = {"X-Device-Token": paired_device["device_token"]}
    body = {
        "protocol_version": "executor.v1",
        "expected_version": 0,
        "target_status": "running",
        "page_fingerprint": "sha256:abc123",
        "page_index": 1,
        "field_counts": {"confirmed": 1, "defaulted": 0, "missing": 0, "low": 0},
    }
    # No lease headers at all
    response = client.post(
        f"/api/executor/tasks/{seeded_task.id}/progress",
        headers=headers,
        json=body,
    )
    assert response.status_code == 401


def test_openapi_has_no_submit_operation_or_scope(client) -> None:
    schema_text = client.get("/openapi.json").text.lower()
    assert "task:progress" not in schema_text
    assert "task:result" not in schema_text
    assert "task:submit" not in schema_text
    assert "/api/executor/tasks/{task_id}/progress" in schema_text
    assert "/api/executor/tasks/{task_id}/result" in schema_text
