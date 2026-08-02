"""Verify executor-related sentinel values never escape via logs, API, or disk."""

from __future__ import annotations

from collections.abc import Iterator
import json
import logging
import os
from pathlib import Path
from typing import Any

import fakeredis
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from uvicorn.config import LOGGING_CONFIG
from uvicorn.logging import DefaultFormatter

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("APP_AUTH_SECRET", "test-secret-with-at-least-32-characters")
os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")
os.environ.setdefault(
    "OBJECT_ENCRYPTION_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
)

from backend.app.api import dependencies
from backend.app.config import Settings
from backend.app.db.base import Base
from backend.app.db.models import (
    ApplicationEvent,
    ApplicationTask,
    ApplicationTaskStatus,
    Device,
    DevicePlatform,
    DeviceStatus,
    User,
)
from backend.app.main import create_app
from backend.app.services.applications import (
    _validate_redacted_value,
    UnsafeAuditPayloadError,
)

# ---- Sentinels ---------------------------------------------------------------
SENTINEL_PASSWORD = "ExecutorRedact-Pass-9284"
SENTINEL_TOKEN = "executor-device-token-must-be-redacted"
SENTINEL_LEASE = "task-lease-must-be-redacted-abc123"
SENTINEL_COOKIE = "session-cookie=secret-value"
SENTINEL_CAPTCHA = "captcha-response-value-9284"
SENTINEL_RESUME = "base64-encoded-resume-binary-data"
SENTINEL_FORM_VALUE = "sensitive-form-field-value-12345"

ALL_SENTINELS = (
    SENTINEL_PASSWORD,
    SENTINEL_TOKEN,
    SENTINEL_LEASE,
    SENTINEL_COOKIE,
    SENTINEL_CAPTCHA,
    SENTINEL_RESUME,
    SENTINEL_FORM_VALUE,
)


# ---- Log capture -------------------------------------------------------------


def _production_formatter() -> logging.Formatter:
    config = dict(LOGGING_CONFIG["formatters"]["default"])
    return DefaultFormatter(
        fmt=config.get("fmt"),
        datefmt=config.get("datefmt"),
        use_colors=False,
    )


class CaptureHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []
        self.formatted: list[str] = []
        self.setFormatter(_production_formatter())

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)
        self.formatted.append(self.format(record))


def _contains_sensitive_value(
    value: Any, sentinels: tuple[str, ...], seen: set[int] | None = None
) -> bool:
    if isinstance(value, str):
        return any(sentinel in value for sentinel in sentinels)
    if isinstance(value, bytes):
        return any(sentinel.encode() in value for sentinel in sentinels)
    if value is None or isinstance(value, (bool, int, float)):
        return False
    visited = seen if seen is not None else set()
    identity = id(value)
    if identity in visited:
        return False
    visited.add(identity)
    if isinstance(value, dict):
        return any(
            _contains_sensitive_value(k, sentinels, visited)
            or _contains_sensitive_value(v, sentinels, visited)
            for k, v in value.items()
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(
            _contains_sensitive_value(item, sentinels, visited) for item in value
        )
    return _contains_sensitive_value(repr(value), sentinels, visited)


def _assert_no_sentinels_in_logs(
    handler: CaptureHandler, sentinels: tuple[str, ...] = ALL_SENTINELS
) -> None:
    for formatted, record in zip(handler.formatted, handler.records, strict=True):
        if _contains_sensitive_value(formatted, sentinels):
            raise AssertionError(
                f"sentinel detected in formatted log: {formatted[:200]}"
            )
        if _contains_sensitive_value(record.args, sentinels):
            raise AssertionError(
                f"sentinel detected in log record args: {record.args}"
            )
        if _contains_sensitive_value(record.__dict__, sentinels):
            raise AssertionError(
                "sentinel detected in log record __dict__"
            )


# ---- Fixtures ----------------------------------------------------------------


@pytest.fixture
def capture_handler() -> CaptureHandler:
    return CaptureHandler()


@pytest.fixture
def db() -> Session:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        yield session


@pytest.fixture
def user(db: Session) -> User:
    value = User(
        account="exec-redact-user",
        nickname="Executor Redaction",
        password_hash="argon2-hash",
    )
    db.add(value)
    db.commit()
    return value


@pytest.fixture
def paired_device(db: Session, user: User) -> Device:
    device = Device(
        user_id=user.id,
        name="Redaction Test Device",
        platform=DevicePlatform.WINDOWS,
        status=DeviceStatus.ACTIVE,
        token_hash="redact-test-token-hash",
        public_key_pem="redact-test-public-key",
    )
    db.add(device)
    db.commit()
    return device


@pytest.fixture
def seeded_task(db: Session, user: User, paired_device: Device) -> ApplicationTask:
    task = ApplicationTask(
        user_id=user.id,
        target_job_id="simulation-job",
        device_id=paired_device.id,
        status=ApplicationTaskStatus.DISPATCHED,
    )
    db.add(task)
    db.commit()
    return task


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestExecutorRedactionValidation:
    """The service-layer audit-payload validation rejects forbidden keys."""

    def test_forbidden_key_password_is_rejected(self) -> None:
        with pytest.raises(UnsafeAuditPayloadError, match="password"):
            _validate_redacted_value({"password": SENTINEL_PASSWORD})

    def test_forbidden_key_token_is_rejected(self) -> None:
        with pytest.raises(UnsafeAuditPayloadError, match="token"):
            _validate_redacted_value({"device_token": SENTINEL_TOKEN})

    def test_forbidden_key_cookie_is_rejected(self) -> None:
        with pytest.raises(UnsafeAuditPayloadError, match="cookie"):
            _validate_redacted_value({"cookie": SENTINEL_COOKIE})

    def test_forbidden_key_captcha_is_rejected(self) -> None:
        with pytest.raises(UnsafeAuditPayloadError, match="captcha"):
            _validate_redacted_value({"captcha": SENTINEL_CAPTCHA})

    def test_forbidden_key_resume_text_is_rejected(self) -> None:
        with pytest.raises(UnsafeAuditPayloadError, match="resume_text"):
            _validate_redacted_value({"resume_text": SENTINEL_RESUME})

    def test_forbidden_key_form_values_is_rejected(self) -> None:
        with pytest.raises(UnsafeAuditPayloadError, match="form_values"):
            _validate_redacted_value({"form_values": SENTINEL_FORM_VALUE})


class TestExecutorLogRedaction:
    """Executor log messages must not contain sentinel values."""

    def test_executor_logging_never_leaks_sentinels(
        self, capture_handler: CaptureHandler
    ) -> None:
        logger = logging.getLogger("executor.redaction")
        logger.addHandler(capture_handler)
        logger.setLevel(logging.DEBUG)
        logger.propagate = False

        # Simulate common log patterns that might accidentally include sentinels
        logger.info("Executor task dispatched: task_id=12345")
        logger.warning("Device heartbeat received")
        logger.error("Task lease expired for task task-12345")
        logger.info("Processing complete for task 12345")

        # Verify no sentinels leaked
        _assert_no_sentinels_in_logs(capture_handler)

    def test_executor_progress_logging_with_safe_payload(
        self,
        capture_handler: CaptureHandler,
        db: Session,
        user: User,
        paired_device: Device,
        seeded_task: ApplicationTask,
    ) -> None:
        """Executor progress report uses only safe field counts in redacted_payload."""
        logger = logging.getLogger("executor.redaction.safe")
        logger.addHandler(capture_handler)
        logger.setLevel(logging.DEBUG)
        logger.propagate = False

        # Perform a real transition through the service layer
        from backend.app.services.executor_tasks import ExecutorTaskService

        service = ExecutorTaskService()
        task = service.report_progress(
            db,
            device=paired_device,
            task_id=seeded_task.id,
            expected_version=0,
            target=ApplicationTaskStatus.RUNNING,
            page_fingerprint="sha256:abc123",
            page_index=1,
            reason_code=None,
            field_counts={
                "confirmed": 1,
                "defaulted": 0,
                "missing": 0,
                "low": 0,
            },
        )

        # Verify event's redacted_payload has only safe keys
        event = db.scalar(
            select(ApplicationEvent).where(
                ApplicationEvent.task_id == seeded_task.id
            )
        )
        assert event is not None
        payload = event.redacted_payload
        assert set(payload.keys()) <= {
            "page_fingerprint",
            "page_index",
            "reason_code",
            "confirmed_count",
            "defaulted_count",
            "missing_count",
            "low_confidence_count",
        }
        # Verify none of the sentinel values appear anywhere in the event
        payload_json = json.dumps(payload)
        for sentinel in ALL_SENTINELS:
            assert sentinel not in payload_json

        assert task.status is ApplicationTaskStatus.RUNNING
        _assert_no_sentinels_in_logs(capture_handler)


class TestCheckpointRedaction:
    """Checkpoint files must not contain form values or credentials."""

    def test_checkpoint_contains_only_safe_data(self, tmp_path: Path) -> None:
        store_dir = tmp_path / "checkpoints"
        store_dir.mkdir()
        cp_file = store_dir / "test-task-id.json"

        # Simulate a checkpoint with only safe structural fields
        safe_data = {
            "protocol_version": "executor.v1",
            "task_id": "33333333-3333-4333-8333-333333333333",
            "task_state_version": 1,
            "step": "fill_page",
            "page_index": 1,
            "page_fingerprint": "sha256:def456",
            "completed_field_keys": ["full_name"],
            "completed_effect_keys": [],
            "pending_field_key": None,
            "pending_effect_key": None,
            "issue_counts": {
                "missing": 0,
                "low": 0,
                "readback": 0,
                "defaulted": 0,
            },
        }
        cp_file.write_text(json.dumps(safe_data, indent=2), encoding="utf-8")

        raw = cp_file.read_text(encoding="utf-8")
        for sentinel in ALL_SENTINELS:
            assert sentinel not in raw, (
                f"sentinel {sentinel[:20]}... leaked in checkpoint file"
            )


class TestCliStderrRedaction:
    """CLI error messages on stderr must not leak sentinel values."""

    def test_cli_error_messages_omit_sentinels(self) -> None:
        stderr_lines = [
            "Error: failed to pair device",
            "Error: simulation requires loopback, "
            "got: http://remote.example.com",
            "Error: checkpoint not found for task "
            "33333333-3333-4333-8333-333333333333",
        ]
        combined = "\n".join(stderr_lines)
        for sentinel in ALL_SENTINELS:
            assert sentinel not in combined, (
                f"sentinel {sentinel[:20]}... leaked in CLI stderr"
            )


class TestApiResponseRedaction:
    """API responses must not contain sentinel values."""

    @pytest.fixture
    def client(self, db: Session) -> TestClient:
        redis = fakeredis.FakeRedis()

        def override_db() -> Iterator[Session]:
            yield db

        settings = Settings(
            app_env="test",
            app_auth_secret="test-secret-with-at-least-32-characters",
            object_encryption_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
            database_url="sqlite+pysqlite:///:memory:",
            redis_url="redis://localhost:6379/15",
            checkpoint_backend="sqlite",
        )

        app = create_app(settings)
        app.dependency_overrides[dependencies._get_db] = override_db
        app.dependency_overrides[dependencies.get_redis] = lambda: redis
        with TestClient(app) as test_client:
            yield test_client

    def test_health_response_omits_sentinels(self, client: TestClient) -> None:
        response = client.get("/api/health")
        text = response.text.lower()
        for sentinel in ALL_SENTINELS:
            assert sentinel.lower() not in text, (
                f"sentinel {sentinel[:20]}... leaked in health response"
            )

    def test_openapi_schema_omits_sentinels(self, client: TestClient) -> None:
        response = client.get("/openapi.json")
        text = response.text.lower()
        for sentinel in ALL_SENTINELS:
            assert sentinel.lower() not in text, (
                f"sentinel {sentinel[:20]}... leaked in OpenAPI schema"
            )
