from __future__ import annotations

import base64
import logging
import os
from collections.abc import Iterator

import fakeredis
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("APP_AUTH_SECRET", "test-secret-with-at-least-32-characters")
os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")
os.environ.setdefault(
    "OBJECT_ENCRYPTION_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
)

from backend.app.api import dependencies
from backend.app.db.base import Base
from backend.app.db.models import (
    ApplicationTask,
    ApplicationTaskStatus,
    TaskActor,
    User,
)
from backend.app.main import create_app
from backend.app.services.applications import ApplicationService, InvalidTransitionError
from backend.app.services.storage import EncryptedObjectStore
from tests.conftest import settings_override


PASSWORD = "LogSafety-Password-9284"
DEVICE_TOKEN = "device-token-must-never-be-logged-in-full"
PAIRING_CODE = "pair-code-must-never-be-logged"
OBJECT_PLAINTEXT = b"private resume plaintext must stay out of logs"
CONFIG_SECRET = "object-config-secret-must-never-be-logged"


def _messages(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(record.getMessage() for record in caplog.records)


def _assert_safe_log(
    caplog: pytest.LogCaptureFixture, marker: str, *secrets: str | bytes
) -> None:
    messages = _messages(caplog)
    assert marker in messages
    for secret in secrets:
        text = secret.decode() if isinstance(secret, bytes) else secret
        assert text not in messages


@pytest.fixture
def client() -> Iterator[TestClient]:
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

    app = create_app(settings_override())
    app.dependency_overrides[dependencies._get_db] = override_db
    app.dependency_overrides[dependencies.get_redis] = lambda: redis
    with TestClient(app) as test_client:
        yield test_client
    engine.dispose()


def test_registration_failure_logs_outcome_without_password(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    payload = {"account": "duplicate", "nickname": "Student", "password": PASSWORD}
    assert client.post("/api/auth/register", json=payload).status_code == 200
    caplog.clear()

    with caplog.at_level(logging.WARNING, logger="backend.app"):
        response = client.post("/api/auth/register", json=payload)

    assert response.status_code == 409
    _assert_safe_log(caplog, "registration rejected", PASSWORD)


def test_login_failure_logs_outcome_without_password_or_config_secret(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="backend.app"):
        response = client.post(
            "/api/auth/login",
            json={"account": "unknown", "password": PASSWORD},
        )

    assert response.status_code == 401
    _assert_safe_log(caplog, "login rejected", PASSWORD, CONFIG_SECRET)


def test_invalid_device_token_logs_outcome_without_token_or_pairing_code(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="backend.app"):
        response = client.get(
            "/api/devices/me",
            headers={"X-Device-Token": DEVICE_TOKEN, "X-Pairing-Code": PAIRING_CODE},
        )

    assert response.status_code == 401
    _assert_safe_log(caplog, "device authentication rejected", DEVICE_TOKEN, PAIRING_CODE)


def test_object_store_failure_logs_operation_without_plaintext_or_config_secret(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class FailingBlobStore:
        def put_bytes(self, **_kwargs: object) -> None:
            raise RuntimeError(
                f"upload failed for {OBJECT_PLAINTEXT.decode()} using {CONFIG_SECRET}"
            )

    encryption_key = base64.b64encode(bytes(range(32))).decode("ascii")
    store = EncryptedObjectStore(FailingBlobStore(), encryption_key)  # type: ignore[arg-type]

    with caplog.at_level(logging.ERROR, logger="backend.app"):
        with pytest.raises(RuntimeError, match="upload failed"):
            store.put(
                key="users/u1/resume.pdf",
                plaintext=OBJECT_PLAINTEXT,
                content_type="application/pdf",
            )

    _assert_safe_log(caplog, "encrypted object write failed", OBJECT_PLAINTEXT, CONFIG_SECRET)


def test_state_transition_failure_logs_outcome_without_payload_or_config_secret(
    caplog: pytest.LogCaptureFixture,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            user = User(account="state-user", nickname="State", password_hash="argon")
            db.add(user)
            db.flush()
            task = ApplicationTask(
                user_id=user.id,
                target_job_id="job-1",
                status=ApplicationTaskStatus.READY_FOR_REVIEW,
            )
            db.add(task)
            db.commit()
            sensitive_summary = f"{OBJECT_PLAINTEXT.decode()} {CONFIG_SECRET}"

            with caplog.at_level(logging.WARNING, logger="backend.app"):
                with pytest.raises(InvalidTransitionError):
                    ApplicationService().transition(
                        db,
                        task_id=task.id,
                        expected_version=0,
                        target=ApplicationTaskStatus.OBSERVING_USER_SUBMISSION,
                        actor=TaskActor.EXECUTOR,
                        event_type="executor_attempt",
                        redacted_payload={"summary": sensitive_summary},
                    )

        _assert_safe_log(
            caplog,
            "application transition rejected",
            OBJECT_PLAINTEXT,
            CONFIG_SECRET,
        )
    finally:
        engine.dispose()
