from __future__ import annotations

import base64
from contextlib import contextmanager
import logging
import os
from collections.abc import Iterator, Mapping
from typing import Any

import fakeredis
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
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
from backend.app.db.base import Base
from backend.app.db.models import (
    ApplicationTask,
    ApplicationTaskStatus,
    JobPosting,
    JobPostingStatus,
    JobSource,
    JobSourceProvider,
    RawJobRecord,
    TaskActor,
    User,
    UserRole,
)
from backend.app.services.auth import AuthService
from backend.app.main import create_app
from backend.app.services.applications import ApplicationService, InvalidTransitionError
from backend.app.services.job_sync import JobSyncService
from backend.app.services.storage import EncryptedObjectStore
from backend.app.services.tencent_smartsheet import TencentUnavailableError
from tests.conftest import settings_override


PASSWORD = "LogSafety-Password-9284"
DEVICE_TOKEN = "device-token-must-never-be-logged-in-full"
PAIRING_CODE = "pair-code-must-never-be-logged"
OBJECT_PLAINTEXT = b"private resume plaintext must stay out of logs"
CONFIG_SECRET = "object-config-secret-must-never-be-logged"
SENSITIVE_VALUES = (
    PASSWORD,
    DEVICE_TOKEN,
    PAIRING_CODE,
    OBJECT_PLAINTEXT.decode(),
    CONFIG_SECRET,
)


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


@contextmanager
def _capture_application_logs() -> Iterator[CaptureHandler]:
    logger = logging.getLogger("backend.app")
    handler = CaptureHandler()
    previous_level = logger.level
    previous_propagate = logger.propagate
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    logger.addHandler(handler)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate


def _contains_sensitive_value(
    value: Any, sensitive_values: tuple[str, ...], seen: set[int] | None = None
) -> bool:
    if isinstance(value, str):
        return any(sensitive in value for sensitive in sensitive_values)
    if isinstance(value, bytes):
        return any(sensitive.encode() in value for sensitive in sensitive_values)
    if value is None or isinstance(value, (bool, int, float)):
        return False
    if isinstance(value, BaseException):
        return _contains_sensitive_value(str(value), sensitive_values, seen)

    visited = seen if seen is not None else set()
    identity = id(value)
    if identity in visited:
        return False
    visited.add(identity)

    if isinstance(value, Mapping):
        return any(
            _contains_sensitive_value(key, sensitive_values, visited)
            or _contains_sensitive_value(nested, sensitive_values, visited)
            for key, nested in value.items()
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(
            _contains_sensitive_value(item, sensitive_values, visited) for item in value
        )
    return _contains_sensitive_value(repr(value), sensitive_values, visited)


def _assert_safe_log(
    capture: CaptureHandler,
    marker: str,
    sensitive_values: tuple[str, ...] = SENSITIVE_VALUES,
) -> None:
    assert capture.records
    assert marker in "\n".join(capture.formatted)
    for formatted, record in zip(capture.formatted, capture.records, strict=True):
        if _contains_sensitive_value(formatted, sensitive_values):
            raise AssertionError("sensitive value detected")
        if _contains_sensitive_value(record.args, sensitive_values):
            raise AssertionError("sensitive value detected")
        if _contains_sensitive_value(record.__dict__, sensitive_values):
            raise AssertionError("sensitive value detected")


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

    app = create_app(settings_override(app_auth_secret=CONFIG_SECRET))
    app.dependency_overrides[dependencies._get_db] = override_db
    app.dependency_overrides[dependencies.get_redis] = lambda: redis
    with TestClient(app) as test_client:
        test_client.session_factory = session_factory  # type: ignore[attr-defined]
        yield test_client
    engine.dispose()


def test_registration_failure_logs_outcome_without_password(
    client: TestClient,
) -> None:
    payload = {"account": "duplicate", "nickname": "Student", "password": PASSWORD}
    assert client.post("/api/auth/register", json=payload).status_code == 200
    with _capture_application_logs() as capture:
        response = client.post("/api/auth/register", json=payload)

    assert response.status_code == 409
    _assert_safe_log(capture, "registration rejected")


def test_login_failure_logs_outcome_without_password_or_config_secret(
    client: TestClient,
) -> None:
    with _capture_application_logs() as capture:
        response = client.post(
            "/api/auth/login",
            json={"account": "unknown", "password": PASSWORD},
        )

    assert response.status_code == 401
    _assert_safe_log(capture, "login rejected")


def test_invalid_device_token_logs_outcome_without_token_or_pairing_code(
    client: TestClient,
) -> None:
    with _capture_application_logs() as capture:
        response = client.get(
            "/api/devices/me",
            headers={"X-Device-Token": DEVICE_TOKEN, "X-Pairing-Code": PAIRING_CODE},
        )

    assert response.status_code == 401
    _assert_safe_log(capture, "device authentication rejected")


def test_object_store_failure_logs_operation_without_plaintext_or_config_secret() -> (
    None
):
    class FailingBlobStore:
        def put_bytes(self, **_kwargs: object) -> None:
            raise RuntimeError(
                f"upload failed for {OBJECT_PLAINTEXT.decode()} using {CONFIG_SECRET}"
            )

    encryption_key = base64.b64encode(bytes(range(32))).decode("ascii")
    store = EncryptedObjectStore(FailingBlobStore(), encryption_key)  # type: ignore[arg-type]

    with _capture_application_logs() as capture:
        with pytest.raises(RuntimeError, match="upload failed"):
            store.put(
                key="users/u1/resume.pdf",
                plaintext=OBJECT_PLAINTEXT,
                content_type="application/pdf",
            )

    _assert_safe_log(capture, "encrypted object write failed")


def test_state_transition_failure_logs_outcome_without_payload_or_config_secret() -> (
    None
):
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

            with _capture_application_logs() as capture:
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
            capture,
            "application transition rejected",
        )
    finally:
        engine.dispose()


def test_job_sync_failure_redacts_gateway_details_from_logs_and_response(
    client: TestClient,
) -> None:
    secret_token = "tdoc-super-secret-token"
    raw_payload = "raw-company-private-payload"
    upstream_body = "upstream-debug-response"
    sensitive_values = (secret_token, raw_payload, upstream_body)

    class FailingGateway:
        def list_fields(self, _file_id: str, _sheet_id: str) -> list[object]:
            raise TencentUnavailableError(" | ".join(sensitive_values))

        def list_records(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("record listing must not follow a field-list failure")

    with client.session_factory() as db:  # type: ignore[attr-defined]
        admin = User(
            account="sync-log-admin",
            nickname="Sync Log Admin",
            password_hash="unused",
            role=UserRole.ADMIN,
        )
        db.add(admin)
        db.commit()
        db.refresh(admin)
        headers = {
            "Authorization": (
                f"Bearer {AuthService(client.app.state.settings).issue_user_token(admin)}"
            )
        }

    client.app.state.job_sync_service = JobSyncService(FailingGateway())  # type: ignore[arg-type]
    with _capture_application_logs() as capture:
        response = client.post(
            "/api/admin/job-sources/tencent-intern-referrals/sync",
            headers=headers,
        )

    combined_output = response.text + "\n" + "\n".join(capture.formatted)
    assert response.status_code == 503
    assert "tencent_unavailable" in combined_output
    assert not any(value in combined_output for value in sensitive_values)
    for record in capture.records:
        assert not _contains_sensitive_value(record.args, sensitive_values)
        assert not _contains_sensitive_value(record.__dict__, sensitive_values)


def test_unexpected_job_sync_failure_redacts_details_from_logs_and_response(
    client: TestClient,
) -> None:
    sensitive_detail = "ordinary-exception-secret-upstream-detail"

    class FailingGateway:
        def list_fields(self, _file_id: str, _sheet_id: str) -> list[object]:
            raise RuntimeError(sensitive_detail)

        def list_records(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("record listing must not follow a field-list failure")

    with client.session_factory() as db:  # type: ignore[attr-defined]
        admin = User(
            account="unexpected-sync-log-admin",
            nickname="Unexpected Sync Log Admin",
            password_hash="unused",
            role=UserRole.ADMIN,
        )
        db.add(admin)
        db.commit()
        db.refresh(admin)
        headers = {
            "Authorization": (
                f"Bearer {AuthService(client.app.state.settings).issue_user_token(admin)}"
            )
        }

    client.app.state.job_sync_service = JobSyncService(FailingGateway())  # type: ignore[arg-type]
    with _capture_application_logs() as capture:
        response = client.post(
            "/api/admin/job-sources/tencent-intern-referrals/sync",
            headers=headers,
        )

    combined_output = response.text + "\n" + "\n".join(capture.formatted)
    assert response.status_code == 503
    assert response.json()["detail"]["error_code"] == "job_sync_unexpected_error"
    assert sensitive_detail not in combined_output
    for record in capture.records:
        assert not _contains_sensitive_value(record.args, (sensitive_detail,))
        assert not _contains_sensitive_value(record.__dict__, (sensitive_detail,))


def test_admin_completion_never_leaks_raw_source_or_credentials(
    client: TestClient,
) -> None:
    business_sentinels = (
        "business-company-sentinel",
        "business-title-sentinel",
        "business-jd-sentinel",
        "business-location-sentinel",
        "business-recruitment-sentinel",
        "business-industry-sentinel",
        "business-referral-sentinel",
        "business-deadline-sentinel",
    )
    source_sentinels = (
        "source-company-sentinel",
        "source-title-sentinel",
        "source-location-sentinel",
        "source-recruitment-sentinel",
        "source-industry-sentinel",
        "source-referral-sentinel",
        "source-deadline-sentinel",
    )
    raw_value = "raw-record-value-sentinel"
    payload_hash = "a" * 64
    external_record_id = "external-record-sentinel"
    mcp_trace = "mcp-trace-sentinel"
    upstream_response = "complete-upstream-response-sentinel"
    tencent_token = "tencent-token-sentinel"
    authorization_value = "authorization-secret-sentinel"

    with client.session_factory() as db:  # type: ignore[attr-defined]
        admin = User(
            account="completion-log-admin",
            nickname="Completion Log Admin",
            password_hash="unused",
            role=UserRole.ADMIN,
        )
        source = JobSource(
            source_key="completion-log-source",
            provider=JobSourceProvider.TENCENT_SMARTSHEET,
            name="Completion Log Source",
            file_id="completion-log-file",
            sheet_id="completion-log-sheet",
            mapper_version="completion-log-v1",
            enabled=True,
        )
        db.add_all([admin, source])
        db.flush()
        raw = RawJobRecord(
            source_id=source.id,
            external_record_id=external_record_id,
            payload_hash=payload_hash,
            raw_fields=[
                {
                    "raw_value": raw_value,
                    "mcp_trace": mcp_trace,
                    "upstream_response": upstream_response,
                    "token": tencent_token,
                }
            ],
        )
        db.add(raw)
        db.flush()
        posting = JobPosting(
            source_id=source.id,
            external_record_id=external_record_id,
            raw_record_id=raw.id,
            status=JobPostingStatus.PENDING_COMPLETION,
            company_name="Unreviewed Company",
            title="Unreviewed Role",
            locations=[],
            recruitment_types=[],
            industries=[],
            apply_url="https://example.com/unreviewed",
            mapper_version=source.mapper_version,
            source_candidate={
                "company_name": source_sentinels[0],
                "title": source_sentinels[1],
                "locations": [source_sentinels[2]],
                "recruitment_types": [source_sentinels[3]],
                "industries": [source_sentinels[4]],
                "apply_url": "https://source.example.com/apply",
                "referral_code": source_sentinels[5],
                "deadline_text": source_sentinels[6],
                "raw_fields": raw_value,
                "payload_hash": payload_hash,
                "external_record_id": external_record_id,
                "mcp_trace": mcp_trace,
                "authorization": authorization_value,
            },
        )
        db.add(posting)
        db.commit()
        db.refresh(admin)
        db.refresh(posting)
        bearer_token = AuthService(client.app.state.settings).issue_user_token(admin)
        job_id = posting.id

    request_body = {
        "expected_version": 0,
        "company_name": business_sentinels[0],
        "title": business_sentinels[1],
        "description_text": business_sentinels[2],
        "locations": [business_sentinels[3]],
        "recruitment_types": [business_sentinels[4]],
        "industries": [business_sentinels[5]],
        "apply_url": "https://business.example.com/apply",
        "referral_code": business_sentinels[6],
        "deadline_text": business_sentinels[7],
        "raw_fields": raw_value,
        "payload_hash": payload_hash,
        "external_record_id": external_record_id,
        "mcp_trace": mcp_trace,
        "authorization": authorization_value,
        "tencent_token": tencent_token,
        "upstream_response": upstream_response,
    }
    log_forbidden = (
        *business_sentinels,
        *source_sentinels,
        raw_value,
        payload_hash,
        external_record_id,
        mcp_trace,
        upstream_response,
        tencent_token,
        authorization_value,
        bearer_token,
        "raw_fields",
        "payload_hash",
        "external_record_id",
        "mcp_trace",
        "authorization",
    )
    response_forbidden = (
        raw_value,
        payload_hash,
        external_record_id,
        mcp_trace,
        upstream_response,
        tencent_token,
        authorization_value,
        bearer_token,
        "raw_fields",
        "payload_hash",
        "external_record_id",
        "mcp_trace",
        "authorization",
        "tencent_token",
        "upstream_response",
    )

    with _capture_application_logs() as capture:
        response = client.patch(
            f"/api/admin/jobs/{job_id}/completion",
            headers={"Authorization": f"Bearer {bearer_token}"},
            json=request_body,
        )

    assert response.status_code == 200
    assert response.json()["company_name"] == business_sentinels[0]
    assert response.json()["source_candidate"]["title"] == source_sentinels[1]
    assert not _contains_sensitive_value(response.text, response_forbidden)
    for formatted, record in zip(capture.formatted, capture.records, strict=True):
        assert not _contains_sensitive_value(formatted, log_forbidden)
        assert not _contains_sensitive_value(record.args, log_forbidden)
        assert not _contains_sensitive_value(record.__dict__, log_forbidden)


@pytest.mark.parametrize("mutation", ["exception", "args", "nested_extra"])
def test_sensitive_log_capture_detects_indirect_leaks_without_echoing_value(
    mutation: str,
) -> None:
    logger = logging.getLogger("backend.app.capture_self_test")
    with _capture_application_logs() as capture:
        if mutation == "exception":
            try:
                raise RuntimeError(CONFIG_SECRET)
            except RuntimeError:
                logger.exception("capture mutation")
        elif mutation == "args":
            logger.warning("capture mutation %s", CONFIG_SECRET)
        else:
            logger.warning(
                "capture mutation",
                extra={"security_context": {"nested": [CONFIG_SECRET]}},
            )

    with pytest.raises(AssertionError, match="sensitive value detected") as error:
        _assert_safe_log(capture, "capture mutation")
    assert str(error.value) == "sensitive value detected"
