from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
import os
from typing import Any

import fakeredis
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.api import dependencies
from backend.app.config import Settings
from backend.app.db.base import Base
from backend.app.db.models import (
    JobPosting,
    JobPostingStatus,
    JobSource,
    JobSourceProvider,
    JobSyncRunStatus,
    RawJobRecord,
    User,
    UserRole,
)
from backend.app.repositories.jobs import (
    SourceDisabledError,
    SourceNotFoundError,
    SyncConflictError,
)
from backend.app.services.auth import AuthService
from backend.app.services.job_sync import JobSyncFailedError

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("APP_AUTH_SECRET", "test-secret-with-at-least-32-characters")
os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")
os.environ.setdefault(
    "OBJECT_ENCRYPTION_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
)

from backend.app.main import create_app


NOW = datetime(2026, 7, 15, 10, tzinfo=timezone.utc)


@pytest.fixture
def client() -> Iterator[TestClient]:
    settings = Settings(
        app_env="test",
        app_auth_secret="test-secret-with-at-least-32-characters",
        object_encryption_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        database_url="sqlite+pysqlite:///:memory:",
        redis_url="redis://localhost:6379/15",
        checkpoint_backend="sqlite",
    )
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

    app = create_app(settings, session_factory=session_factory)
    app.state.redis = redis
    app.dependency_overrides[dependencies._get_db] = override_db
    app.dependency_overrides[dependencies.get_redis] = lambda: redis
    with TestClient(app) as value:
        value.session_factory = session_factory  # type: ignore[attr-defined]
        yield value
    engine.dispose()


@dataclass(frozen=True)
class FakeSyncOutcome:
    run_id: str = "run-1"
    source_key: str = "tencent-intern-referrals"
    status: JobSyncRunStatus = JobSyncRunStatus.SUCCEEDED
    pages_read: int = 1
    records_read: int = 1
    raw_snapshots_created: int = 1
    postings_created: int = 1
    postings_updated: int = 0
    records_skipped_incomplete: int = 0
    started_at: datetime = NOW
    finished_at: datetime = NOW + timedelta(seconds=1)


class FakeSyncService:
    def __init__(self, admin_id: str) -> None:
        self.admin_id = admin_id
        self.calls: list[dict[str, str]] = []
        self.error: Exception | None = None

    def sync(self, _db: Any, *, source_key: str, actor_user_id: str) -> FakeSyncOutcome:
        self.calls.append({"source_key": source_key, "actor_user_id": actor_user_id})
        if self.error is not None:
            raise self.error
        return FakeSyncOutcome(source_key=source_key)


@pytest.fixture
def seeded(client: TestClient) -> dict[str, Any]:
    with client.session_factory() as db:  # type: ignore[attr-defined]
        admin = User(
            account="admin",
            nickname="Admin",
            password_hash="unused",
            role=UserRole.ADMIN,
        )
        student = User(
            account="student",
            nickname="Student",
            password_hash="unused",
            role=UserRole.STUDENT,
        )
        source = JobSource(
            source_key="tencent-intern-referrals",
            provider=JobSourceProvider.TENCENT_SMARTSHEET,
            name="Intern Referrals",
            file_id="file",
            sheet_id="sheet",
            mapper_version="intern-v1",
            enabled=True,
        )
        other_source = JobSource(
            source_key="other-source",
            provider=JobSourceProvider.TENCENT_SMARTSHEET,
            name="Other Source",
            file_id="other-file",
            sheet_id="other-sheet",
            mapper_version="other-v1",
            enabled=True,
        )
        db.add_all([admin, student, source, other_source])
        db.flush()

        postings: list[JobPosting] = []
        for index, (job_source, company, recruitment_types, updated_at) in enumerate(
            [
                (source, "Acme_One", ["实习", "校招"], NOW),
                (source, "Acme Two", ["校招"], NOW),
                (other_source, "Beta Corp", ["社招"], NOW - timedelta(days=1)),
            ],
            start=1,
        ):
            raw = RawJobRecord(
                source_id=job_source.id,
                external_record_id=f"secret-record-{index}",
                payload_hash=str(index) * 64,
                raw_fields=[{"mcp_trace": "secret trace", "token": "secret token"}],
                observed_at=NOW,
                source_updated_at=NOW - timedelta(hours=index),
            )
            db.add(raw)
            db.flush()
            posting = JobPosting(
                source_id=job_source.id,
                external_record_id=f"secret-record-{index}",
                raw_record_id=raw.id,
                status=JobPostingStatus.PENDING_COMPLETION,
                company_name=company,
                title=f"Role {index}",
                locations=["深圳"],
                recruitment_types=recruitment_types,
                industries=["互联网"],
                apply_url=f"https://example.com/apply/{index}",
                referral_code=f"REF-{index}",
                deadline_text="2026-12-31",
                source_updated_at=NOW - timedelta(hours=index),
                mapper_version=job_source.mapper_version,
                updated_at=updated_at,
            )
            db.add(posting)
            postings.append(posting)
        db.commit()

        auth = AuthService(client.app.state.settings)
        result = {
            "admin_headers": {
                "Authorization": f"Bearer {auth.issue_user_token(admin)}"
            },
            "student_headers": {
                "Authorization": f"Bearer {auth.issue_user_token(student)}"
            },
            "admin_id": admin.id,
            "postings": postings,
        }

    service = FakeSyncService(result["admin_id"])
    client.app.state.job_sync_service = service
    result["service"] = service
    return result


def test_admin_can_sync(client: TestClient, seeded: dict[str, Any]) -> None:
    response = client.post(
        "/api/admin/job-sources/tencent-intern-referrals/sync",
        headers=seeded["admin_headers"],
    )

    assert response.status_code == 200
    assert response.json()["status"] == "succeeded"
    assert seeded["service"].calls == [
        {
            "source_key": "tencent-intern-referrals",
            "actor_user_id": seeded["service"].admin_id,
        }
    ]


def test_student_cannot_sync(client: TestClient, seeded: dict[str, Any]) -> None:
    response = client.post(
        "/api/admin/job-sources/tencent-intern-referrals/sync",
        headers=seeded["student_headers"],
    )

    assert response.status_code == 403
    assert seeded["service"].calls == []


def test_anonymous_user_cannot_list_jobs(client: TestClient) -> None:
    assert client.get("/api/jobs").status_code == 401


def test_job_detail_whitelists_fields(
    client: TestClient, seeded: dict[str, Any]
) -> None:
    posting = seeded["postings"][0]
    response = client.get(f"/api/jobs/{posting.id}", headers=seeded["student_headers"])

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "pending_completion"
    assert payload["referral_code"] == "REF-1"
    assert set(payload) == {
        "id",
        "company_name",
        "title",
        "locations",
        "recruitment_types",
        "industries",
        "apply_url",
        "deadline_text",
        "status",
        "source_key",
        "source_name",
        "updated_at",
        "referral_code",
        "source_updated_at",
        "mapper_version",
    }
    serialized = repr(payload)
    for forbidden in (
        "raw_fields",
        "payload_hash",
        "external_record_id",
        "mcp_trace",
        "secret-record",
        "secret token",
        "secret trace",
    ):
        assert forbidden not in serialized


def test_unknown_job_returns_404(client: TestClient, seeded: dict[str, Any]) -> None:
    response = client.get("/api/jobs/not-a-job", headers=seeded["student_headers"])
    assert response.status_code == 404


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (SourceNotFoundError("missing"), 404),
        (SyncConflictError("busy"), 409),
        (SourceDisabledError("disabled"), 409),
    ],
)
def test_sync_maps_source_errors(
    client: TestClient,
    seeded: dict[str, Any],
    error: Exception,
    expected_status: int,
) -> None:
    seeded["service"].error = error
    response = client.post(
        "/api/admin/job-sources/missing/sync", headers=seeded["admin_headers"]
    )
    assert response.status_code == expected_status


@pytest.mark.parametrize(
    ("error_code", "expected_status"),
    [
        ("tencent_protocol_error", 502),
        ("source_schema_changed", 502),
        ("tencent_token_missing", 503),
        ("tencent_auth_failed", 503),
        ("tencent_rate_limited", 503),
        ("tencent_unavailable", 503),
        ("database_write_failed", 503),
        ("tencent_timeout", 504),
    ],
)
def test_sync_failure_has_stable_safe_detail(
    client: TestClient,
    seeded: dict[str, Any],
    error_code: str,
    expected_status: int,
) -> None:
    seeded["service"].error = JobSyncFailedError(
        "run-failed", JobSyncRunStatus.FAILED, error_code
    )
    response = client.post(
        "/api/admin/job-sources/tencent-intern-referrals/sync",
        headers=seeded["admin_headers"],
    )
    assert response.status_code == expected_status
    assert response.json()["detail"] == {
        "error_code": error_code,
        "run_id": "run-failed",
    }
    assert "upstream" not in response.text


@pytest.mark.parametrize(
    "query",
    ["limit=0", "limit=101", "offset=-1"],
)
def test_list_rejects_invalid_pagination(
    client: TestClient, seeded: dict[str, Any], query: str
) -> None:
    response = client.get(f"/api/jobs?{query}", headers=seeded["student_headers"])
    assert response.status_code == 422


@pytest.mark.parametrize(
    ("query", "expected_companies"),
    [
        ("source_key=other-source", ["Beta Corp"]),
        ("company=Acme", ["Acme Two", "Acme_One"]),
        ("company=Acme%25", []),
        ("company=Acme_", ["Acme_One"]),
        ("recruitment_type=实习", ["Acme_One"]),
        ("recruitment_type=实", []),
    ],
)
def test_list_filters_jobs(
    client: TestClient,
    seeded: dict[str, Any],
    query: str,
    expected_companies: list[str],
) -> None:
    response = client.get(f"/api/jobs?{query}", headers=seeded["student_headers"])
    assert response.status_code == 200
    assert sorted(job["company_name"] for job in response.json()["jobs"]) == sorted(
        expected_companies
    )
    assert response.json()["total"] == len(expected_companies)


def test_list_has_stable_order_and_pagination(
    client: TestClient, seeded: dict[str, Any]
) -> None:
    first = client.get("/api/jobs?limit=2", headers=seeded["student_headers"]).json()
    second = client.get(
        "/api/jobs?limit=2&offset=2", headers=seeded["student_headers"]
    ).json()

    expected = sorted(
        seeded["postings"],
        key=lambda posting: (posting.updated_at, posting.id),
        reverse=True,
    )
    assert first["total"] == 3
    assert [job["id"] for job in first["jobs"] + second["jobs"]] == [
        posting.id for posting in expected
    ]


def test_missing_token_does_not_affect_ready_or_job_reads(
    client: TestClient, seeded: dict[str, Any]
) -> None:
    assert client.app.state.settings.tencent_docs_token is None

    class AvailableBlobStore:
        def check_bucket(self) -> None:
            return None

    client.app.state.blob_store = AvailableBlobStore()
    ready = client.get("/api/health/ready")
    listed = client.get("/api/jobs", headers=seeded["student_headers"])

    assert ready.status_code == 200
    assert ready.json()["dependencies"] == {
        "mysql": "up",
        "redis": "up",
        "object_store": "up",
    }
    assert listed.status_code == 200
