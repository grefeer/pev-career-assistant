from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os
from typing import Any

import fakeredis
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
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
    JobVerification,
    RawJobRecord,
    User,
    UserRole,
)
from backend.app.repositories import jobs as job_repository
from backend.app.repositories.jobs import (
    SourceDisabledError,
    SourceNotFoundError,
    SyncConflictError,
)
from backend.app.services.auth import AuthService
from backend.app.services.job_mappers import NormalizedJobCandidate
from backend.app.services.job_review import JobCompletionInput, JobReviewService
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

COMPLETION_BODY = {
    "expected_version": 0,
    "company_name": "Acme",
    "title": "后端实习生",
    "description_text": "负责服务端功能开发。",
    "locations": ["上海"],
    "recruitment_types": ["实习"],
    "industries": ["互联网"],
    "apply_url": "https://example.com/apply/1",
    "referral_code": None,
    "deadline_text": "2026-09-01",
}

ADMIN_DETAIL_FIELDS = {
    "id",
    "company_name",
    "title",
    "locations",
    "recruitment_types",
    "industries",
    "apply_url",
    "deadline_text",
    "status",
    "gui_eligible",
    "source_key",
    "source_name",
    "updated_at",
    "description_text",
    "referral_code",
    "source_candidate",
    "source_changed_since_review",
    "review_version",
}

SOURCE_CANDIDATE_FIELDS = {
    "company_name",
    "title",
    "locations",
    "recruitment_types",
    "industries",
    "apply_url",
    "referral_code",
    "deadline_text",
}


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
                source_candidate={
                    "company_name": company,
                    "title": f"Role {index}",
                    "locations": ["深圳"],
                    "recruitment_types": recruitment_types,
                    "industries": ["互联网"],
                    "apply_url": f"https://example.com/apply/{index}",
                    "referral_code": f"REF-{index}",
                    "deadline_text": "2026-12-31",
                    "raw_fields": [{"token": "nested secret token"}],
                    "payload_hash": "nested secret payload hash",
                    "mcp_trace": "nested secret trace",
                    "upstream_response": "nested upstream response",
                },
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


def _completion_values(posting: JobPosting) -> JobCompletionInput:
    return JobCompletionInput(
        company_name=posting.company_name,
        title=posting.title,
        description_text=posting.description_text or f"{posting.title} 完整 JD",
        locations=list(posting.locations),
        recruitment_types=list(posting.recruitment_types),
        industries=list(posting.industries),
        apply_url=posting.apply_url,
        referral_code=posting.referral_code,
        deadline_text=posting.deadline_text,
    )


def _verify_postings(
    client: TestClient,
    seeded: dict[str, Any],
    *posting_ids: str,
    gui_eligible: bool = True,
) -> None:
    service = JobReviewService(now=lambda: NOW + timedelta(hours=1))
    with client.session_factory() as db:  # type: ignore[attr-defined]
        for posting_id in posting_ids:
            posting = db.get(JobPosting, posting_id)
            assert posting is not None
            saved = service.save_completion(
                db,
                job_id=posting.id,
                actor_user_id=seeded["admin_id"],
                expected_version=posting.review_version,
                values=_completion_values(posting),
            )
            service.verify(
                db,
                job_id=posting.id,
                actor_user_id=seeded["admin_id"],
                expected_version=saved.review_version,
                gui_eligible=gui_eligible,
            )
        db.commit()


def _events_for(db: Session, job_id: str) -> list[JobVerification]:
    return list(
        db.scalars(
            select(JobVerification)
            .where(JobVerification.job_id == job_id)
            .order_by(JobVerification.review_version)
        )
    )


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


def test_anonymous_user_cannot_read_verified_jobs(client: TestClient) -> None:
    assert client.get("/api/jobs").status_code == 401
    assert client.get("/api/jobs/not-a-job").status_code == 401


def test_student_list_only_returns_verified_jobs(
    client: TestClient, seeded: dict[str, Any]
) -> None:
    verified = seeded["postings"][0]
    _verify_postings(client, seeded, verified.id)

    response = client.get("/api/jobs", headers=seeded["student_headers"])

    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert [item["id"] for item in response.json()["jobs"]] == [verified.id]
    assert set(response.json()["jobs"][0]) == {
        "id",
        "company_name",
        "title",
        "locations",
        "recruitment_types",
        "industries",
        "apply_url",
        "deadline_text",
        "status",
        "gui_eligible",
        "source_key",
        "source_name",
        "updated_at",
    }


def test_job_detail_whitelists_fields(
    client: TestClient, seeded: dict[str, Any]
) -> None:
    posting = seeded["postings"][0]
    _verify_postings(client, seeded, posting.id)
    response = client.get(f"/api/jobs/{posting.id}", headers=seeded["student_headers"])

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "verified"
    assert payload["gui_eligible"] is True
    assert payload["description_text"] == "Role 1 完整 JD"
    assert payload["referral_code"] == "REF-1"
    assert payload["updated_at"].endswith("Z")
    assert payload["verified_at"] == "2026-07-15T11:00:00Z"
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
        "gui_eligible",
        "source_key",
        "source_name",
        "updated_at",
        "description_text",
        "referral_code",
        "verified_at",
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
        "nested secret",
        "upstream_response",
    ):
        assert forbidden not in serialized


def test_job_api_serializes_naive_database_datetimes_as_utc(
    client: TestClient, seeded: dict[str, Any]
) -> None:
    posting = seeded["postings"][0]
    _verify_postings(client, seeded, posting.id)
    naive = datetime(2026, 7, 15, 10, 30)
    with client.session_factory() as db:  # type: ignore[attr-defined]
        persisted = db.get(JobPosting, posting.id)
        assert persisted is not None
        persisted.updated_at = naive
        persisted.verified_at = naive
        db.commit()

    detail = client.get(f"/api/jobs/{posting.id}", headers=seeded["student_headers"])
    listed = client.get("/api/jobs", headers=seeded["student_headers"])

    assert detail.status_code == 200
    assert detail.json()["updated_at"] == "2026-07-15T10:30:00Z"
    assert detail.json()["verified_at"] == "2026-07-15T10:30:00Z"
    listed_posting = next(
        job for job in listed.json()["jobs"] if job["id"] == posting.id
    )
    assert listed_posting["updated_at"] == "2026-07-15T10:30:00Z"


def test_unknown_job_returns_404(client: TestClient, seeded: dict[str, Any]) -> None:
    response = client.get("/api/jobs/not-a-job", headers=seeded["student_headers"])
    assert response.status_code == 404


def test_unverified_job_detail_returns_404(
    client: TestClient, seeded: dict[str, Any]
) -> None:
    posting = seeded["postings"][0]
    response = client.get(f"/api/jobs/{posting.id}", headers=seeded["student_headers"])

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
        ("job_sync_unexpected_error", 503),
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


def test_admin_review_queue_uses_strict_whitelist(
    client: TestClient, seeded: dict[str, Any]
) -> None:
    response = client.get(
        "/api/admin/jobs/review-queue", headers=seeded["admin_headers"]
    )

    assert response.status_code == 200
    assert response.json()["total"] == 3
    assert len(response.json()["jobs"]) == 3
    first = response.json()["jobs"][0]
    assert set(first) == ADMIN_DETAIL_FIELDS
    assert set(first["source_candidate"]) == SOURCE_CANDIDATE_FIELDS
    serialized = repr(response.json())
    for forbidden in (
        "raw_fields",
        "payload_hash",
        "external_record_id",
        "mcp_trace",
        "secret-record",
        "secret token",
        "secret trace",
        "upstream_response",
    ):
        assert forbidden not in serialized


@pytest.mark.parametrize("review_status", ["verified", "expired", "unknown"])
def test_admin_review_queue_rejects_terminal_or_unknown_status(
    client: TestClient, seeded: dict[str, Any], review_status: str
) -> None:
    response = client.get(
        f"/api/admin/jobs/review-queue?review_status={review_status}",
        headers=seeded["admin_headers"],
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    "review_status", ["pending_completion", "pending_review", "rejected"]
)
def test_admin_review_queue_accepts_only_nonterminal_statuses(
    client: TestClient, seeded: dict[str, Any], review_status: str
) -> None:
    response = client.get(
        f"/api/admin/jobs/review-queue?review_status={review_status}",
        headers=seeded["admin_headers"],
    )

    assert response.status_code == 200


def test_admin_verified_jobs_requires_admin(
    client: TestClient, seeded: dict[str, Any]
) -> None:
    anonymous = client.get("/api/admin/jobs/verified")
    student = client.get("/api/admin/jobs/verified", headers=seeded["student_headers"])

    assert anonymous.status_code == 401
    assert student.status_code == 403


def test_admin_verified_jobs_are_verified_only_with_strict_current_detail(
    client: TestClient, seeded: dict[str, Any]
) -> None:
    verified = seeded["postings"][0]
    _verify_postings(client, seeded, verified.id)

    response = client.get("/api/admin/jobs/verified", headers=seeded["admin_headers"])

    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert [item["id"] for item in response.json()["jobs"]] == [verified.id]
    detail = response.json()["jobs"][0]
    assert detail["status"] == "verified"
    assert detail["review_version"] == 2
    assert set(detail) == ADMIN_DETAIL_FIELDS
    assert set(detail["source_candidate"]) == SOURCE_CANDIDATE_FIELDS
    serialized = repr(detail)
    for forbidden in (
        "raw_fields",
        "payload_hash",
        "external_record_id",
        "mcp_trace",
        "secret-record",
        "secret token",
        "secret trace",
        "upstream_response",
        "credential",
    ):
        assert forbidden not in serialized


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "offset=-1"])
def test_admin_verified_jobs_reject_invalid_pagination(
    client: TestClient, seeded: dict[str, Any], query: str
) -> None:
    response = client.get(
        f"/api/admin/jobs/verified?{query}", headers=seeded["admin_headers"]
    )

    assert response.status_code == 422


def test_admin_verified_jobs_have_public_total_order_and_pagination(
    client: TestClient, seeded: dict[str, Any]
) -> None:
    _verify_postings(
        client,
        seeded,
        *(posting.id for posting in seeded["postings"]),
    )

    first = client.get(
        "/api/admin/jobs/verified?limit=2&offset=0",
        headers=seeded["admin_headers"],
    ).json()
    second = client.get(
        "/api/admin/jobs/verified?limit=2&offset=2",
        headers=seeded["admin_headers"],
    ).json()
    with client.session_factory() as db:  # type: ignore[attr-defined]
        expected = sorted(
            [db.get(JobPosting, posting.id) for posting in seeded["postings"]],
            key=lambda posting: (posting.updated_at, posting.id),
            reverse=True,
        )

    assert first["total"] == 3
    assert second["total"] == 3
    assert [item["id"] for item in first["jobs"] + second["jobs"]] == [
        posting.id for posting in expected
    ]


def test_admin_can_reload_verified_version_and_expire_job(
    client: TestClient, seeded: dict[str, Any]
) -> None:
    posting = seeded["postings"][0]
    saved = client.patch(
        f"/api/admin/jobs/{posting.id}/completion",
        headers=seeded["admin_headers"],
        json=COMPLETION_BODY,
    )
    verified = client.post(
        f"/api/admin/jobs/{posting.id}/decision",
        headers=seeded["admin_headers"],
        json={
            "expected_version": saved.json()["review_version"],
            "decision": "verify",
            "gui_eligible": True,
        },
    )
    reloaded = client.get("/api/admin/jobs/verified", headers=seeded["admin_headers"])

    assert saved.status_code == 200
    assert verified.status_code == 200
    assert reloaded.status_code == 200
    lifecycle_job = reloaded.json()["jobs"][0]
    assert lifecycle_job["id"] == posting.id
    assert lifecycle_job["review_version"] == verified.json()["review_version"] == 2

    expired = client.post(
        f"/api/admin/jobs/{posting.id}/decision",
        headers=seeded["admin_headers"],
        json={
            "expected_version": lifecycle_job["review_version"],
            "decision": "expire",
            "reason_code": "closed_on_official_site",
        },
    )
    after_expiry = client.get(
        "/api/admin/jobs/verified", headers=seeded["admin_headers"]
    )

    assert expired.status_code == 200
    assert expired.json()["status"] == "expired"
    assert after_expiry.json() == {"total": 0, "jobs": []}


def test_source_sync_makes_reloaded_expire_version_stale_without_extra_event(
    client: TestClient, seeded: dict[str, Any]
) -> None:
    posting = seeded["postings"][0]
    saved = client.patch(
        f"/api/admin/jobs/{posting.id}/completion",
        headers=seeded["admin_headers"],
        json={**COMPLETION_BODY, "company_name": "Reviewed Company"},
    )
    verified = client.post(
        f"/api/admin/jobs/{posting.id}/decision",
        headers=seeded["admin_headers"],
        json={
            "expected_version": saved.json()["review_version"],
            "decision": "verify",
            "gui_eligible": True,
        },
    )
    assert verified.status_code == 200
    reloaded = client.get("/api/admin/jobs/verified", headers=seeded["admin_headers"])
    read_version = reloaded.json()["jobs"][0]["review_version"]
    assert read_version == 2

    with client.session_factory() as db:  # type: ignore[attr-defined]
        persisted = db.get(JobPosting, posting.id)
        assert persisted is not None
        source = db.get(JobSource, persisted.source_id)
        assert source is not None
        raw, created = job_repository.insert_raw_snapshot(
            db,
            source_id=source.id,
            external_record_id=persisted.external_record_id,
            raw_fields=[{"source": "changed-before-expiry"}],
            payload_hash="e" * 64,
            source_updated_at=NOW + timedelta(days=2),
            observed_at=NOW + timedelta(days=2),
        )
        assert created is True
        job_repository.upsert_posting(
            db,
            source=source,
            raw_record=raw,
            candidate=NormalizedJobCandidate(
                company_name="Upstream Company",
                title="Upstream Role",
                locations=["北京"],
                recruitment_types=["校招"],
                industries=["硬件"],
                apply_url="https://upstream.example.com/apply",
                referral_code="UPSTREAM",
                deadline_text="2027-01-01",
                source_updated_at=NOW + timedelta(days=2),
            ),
        )
        db.commit()

    stale = client.post(
        f"/api/admin/jobs/{posting.id}/decision",
        headers=seeded["admin_headers"],
        json={
            "expected_version": read_version,
            "decision": "expire",
            "reason_code": "closed_on_official_site",
        },
    )

    assert stale.status_code == 409
    assert stale.json() == {
        "detail": {
            "error_code": "stale_job_review",
            "message": "职位审核版本已过期，请重新加载。",
        }
    }
    with client.session_factory() as db:  # type: ignore[attr-defined]
        persisted = db.get(JobPosting, posting.id)
        assert persisted is not None
        assert persisted.status is JobPostingStatus.VERIFIED
        assert persisted.review_version == read_version + 1
        assert persisted.company_name == "Reviewed Company"
        assert persisted.source_changed_since_review is True
        assert [event.action for event in _events_for(db, posting.id)] == [
            "completion_saved",
            "verified",
        ]


def test_student_cannot_use_any_job_review_endpoint(
    client: TestClient, seeded: dict[str, Any]
) -> None:
    posting = seeded["postings"][0]
    responses = [
        client.get("/api/admin/jobs/review-queue", headers=seeded["student_headers"]),
        client.patch(
            f"/api/admin/jobs/{posting.id}/completion",
            headers=seeded["student_headers"],
            json=COMPLETION_BODY,
        ),
        client.post(
            f"/api/admin/jobs/{posting.id}/decision",
            headers=seeded["student_headers"],
            json={
                "expected_version": 0,
                "decision": "reject",
                "gui_eligible": False,
                "reason_code": "invalid_source",
            },
        ),
    ]

    assert [response.status_code for response in responses] == [403, 403, 403]


def test_anonymous_user_cannot_use_any_job_review_endpoint(
    client: TestClient, seeded: dict[str, Any]
) -> None:
    posting = seeded["postings"][0]
    responses = [
        client.get("/api/admin/jobs/review-queue"),
        client.patch(f"/api/admin/jobs/{posting.id}/completion", json=COMPLETION_BODY),
        client.post(
            f"/api/admin/jobs/{posting.id}/decision",
            json={
                "expected_version": 0,
                "decision": "reject",
                "gui_eligible": False,
                "reason_code": "invalid_source",
            },
        ),
    ]

    assert [response.status_code for response in responses] == [401, 401, 401]


def test_admin_can_save_and_verify_job_with_authenticated_actor(
    client: TestClient, seeded: dict[str, Any]
) -> None:
    posting = seeded["postings"][0]
    original_updated_at = posting.updated_at
    completion_body = {**COMPLETION_BODY, "actor_user_id": "forged-user-id"}
    saved = client.patch(
        f"/api/admin/jobs/{posting.id}/completion",
        headers=seeded["admin_headers"],
        json=completion_body,
    )

    assert saved.status_code == 200
    assert saved.json()["status"] == "pending_review"
    assert saved.json()["review_version"] == 1
    assert set(saved.json()) == ADMIN_DETAIL_FIELDS
    assert saved.json()["updated_at"].endswith("Z")
    assert saved.json()["updated_at"] != original_updated_at.isoformat().replace(
        "+00:00", "Z"
    )
    with client.session_factory() as db:  # type: ignore[attr-defined]
        persisted_after_save = db.get(JobPosting, posting.id)
        assert persisted_after_save is not None
        persisted_updated_at = persisted_after_save.updated_at
        if persisted_updated_at.tzinfo is None:
            persisted_updated_at = persisted_updated_at.replace(tzinfo=timezone.utc)
        assert saved.json()["updated_at"] == persisted_updated_at.isoformat().replace(
            "+00:00", "Z"
        )

    verified = client.post(
        f"/api/admin/jobs/{posting.id}/decision",
        headers=seeded["admin_headers"],
        json={
            "expected_version": saved.json()["review_version"],
            "decision": "verify",
            "gui_eligible": True,
            "reason_code": None,
            "actor_user_id": "forged-user-id",
        },
    )

    assert verified.status_code == 200
    assert verified.json()["status"] == "verified"
    assert verified.json()["gui_eligible"] is True
    with client.session_factory() as db:  # type: ignore[attr-defined]
        persisted = db.get(JobPosting, posting.id)
        assert persisted is not None
        assert [event.actor_user_id for event in _events_for(db, posting.id)] == [
            seeded["admin_id"],
            seeded["admin_id"],
        ]


def test_admin_can_save_and_verify_qr_application_without_gui(
    client: TestClient, seeded: dict[str, Any]
) -> None:
    posting = seeded["postings"][0]
    saved = client.patch(
        f"/api/admin/jobs/{posting.id}/completion",
        headers=seeded["admin_headers"],
        json={**COMPLETION_BODY, "apply_url": "qr:campus-scan-2026"},
    )

    assert saved.status_code == 200
    assert saved.json()["status"] == "pending_review"
    assert saved.json()["apply_url"] == "qr:campus-scan-2026"
    assert saved.json()["gui_eligible"] is False

    verified = client.post(
        f"/api/admin/jobs/{posting.id}/decision",
        headers=seeded["admin_headers"],
        json={
            "expected_version": saved.json()["review_version"],
            "decision": "verify",
            "gui_eligible": False,
        },
    )

    assert verified.status_code == 200
    assert verified.json()["status"] == "verified"
    assert verified.json()["apply_url"] == "qr:campus-scan-2026"
    assert verified.json()["gui_eligible"] is False


def test_admin_can_reject_and_expire_jobs(
    client: TestClient, seeded: dict[str, Any]
) -> None:
    rejected_posting, expired_posting = seeded["postings"][:2]
    rejected = client.post(
        f"/api/admin/jobs/{rejected_posting.id}/decision",
        headers=seeded["admin_headers"],
        json={
            "expected_version": 0,
            "decision": "reject",
            "reason_code": "invalid_source",
        },
    )
    _verify_postings(client, seeded, expired_posting.id)
    expired = client.post(
        f"/api/admin/jobs/{expired_posting.id}/decision",
        headers=seeded["admin_headers"],
        json={
            "expected_version": 2,
            "decision": "expire",
            "reason_code": "closed_on_official_site",
        },
    )

    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    assert expired.status_code == 200
    assert expired.json()["status"] == "expired"
    assert expired.json()["gui_eligible"] is False


def test_missing_admin_review_job_returns_stable_404(
    client: TestClient, seeded: dict[str, Any]
) -> None:
    completion = client.patch(
        "/api/admin/jobs/not-a-job/completion",
        headers=seeded["admin_headers"],
        json=COMPLETION_BODY,
    )
    decision = client.post(
        "/api/admin/jobs/not-a-job/decision",
        headers=seeded["admin_headers"],
        json={
            "expected_version": 0,
            "decision": "reject",
            "reason_code": "invalid_source",
        },
    )

    expected = {"detail": {"error_code": "job_not_found", "message": "职位不存在。"}}
    assert completion.status_code == 404
    assert completion.json() == expected
    assert decision.status_code == 404
    assert decision.json() == expected


@pytest.mark.parametrize(
    "body",
    [
        {"expected_version": 0, "decision": "reject"},
        {"expected_version": 0, "decision": "reject", "reason_code": ""},
        {"expected_version": 0, "decision": "expire", "reason_code": "   "},
        {"expected_version": 0, "decision": "verify", "reason_code": "unexpected"},
        {"expected_version": 0, "decision": "verify", "reason_code": ""},
        {"expected_version": 0, "decision": "reject", "reason_code": "unknown"},
        {
            "expected_version": 0,
            "decision": "reject",
            "reason_code": "closed_on_official_site",
        },
        {
            "expected_version": 0,
            "decision": "expire",
            "reason_code": "invalid_source",
        },
    ],
)
def test_admin_decision_rejects_missing_or_mismatched_reason(
    client: TestClient,
    seeded: dict[str, Any],
    body: dict[str, object],
) -> None:
    posting = seeded["postings"][0]

    response = client.post(
        f"/api/admin/jobs/{posting.id}/decision",
        headers=seeded["admin_headers"],
        json=body,
    )

    assert response.status_code == 422
    with client.session_factory() as db:  # type: ignore[attr-defined]
        persisted = db.get(JobPosting, posting.id)
        assert persisted is not None
        assert persisted.status is JobPostingStatus.PENDING_COMPLETION
        assert persisted.review_version == 0
        assert _events_for(db, posting.id) == []


def test_stale_admin_review_returns_409_without_mutation_or_event(
    client: TestClient, seeded: dict[str, Any]
) -> None:
    posting = seeded["postings"][0]
    response = client.patch(
        f"/api/admin/jobs/{posting.id}/completion",
        headers=seeded["admin_headers"],
        json={**COMPLETION_BODY, "expected_version": 99},
    )

    assert response.status_code == 409
    assert response.json() == {
        "detail": {
            "error_code": "stale_job_review",
            "message": "职位审核版本已过期，请重新加载。",
        }
    }
    with client.session_factory() as db:  # type: ignore[attr-defined]
        persisted = db.get(JobPosting, posting.id)
        assert persisted is not None
        assert persisted.status is JobPostingStatus.PENDING_COMPLETION
        assert persisted.review_version == 0
        assert persisted.company_name == "Acme_One"
        assert persisted.description_text is None
        assert _events_for(db, posting.id) == []


def test_invalid_transition_returns_409_without_mutation_or_event(
    client: TestClient, seeded: dict[str, Any]
) -> None:
    posting = seeded["postings"][0]
    response = client.post(
        f"/api/admin/jobs/{posting.id}/decision",
        headers=seeded["admin_headers"],
        json={"expected_version": 0, "decision": "verify", "gui_eligible": True},
    )

    assert response.status_code == 409
    assert response.json() == {
        "detail": {
            "error_code": "invalid_job_transition",
            "message": "当前职位状态不允许执行此审核操作。",
        }
    }
    with client.session_factory() as db:  # type: ignore[attr-defined]
        persisted = db.get(JobPosting, posting.id)
        assert persisted is not None
        assert persisted.status is JobPostingStatus.PENDING_COMPLETION
        assert persisted.review_version == 0
        assert _events_for(db, posting.id) == []


def test_invalid_application_data_returns_422_without_mutation_or_event(
    client: TestClient, seeded: dict[str, Any]
) -> None:
    posting = seeded["postings"][0]
    response = client.patch(
        f"/api/admin/jobs/{posting.id}/completion",
        headers=seeded["admin_headers"],
        json={**COMPLETION_BODY, "apply_url": "not a valid application URL"},
    )

    assert response.status_code == 422
    assert response.json() == {
        "detail": {
            "error_code": "incomplete_job",
            "message": "职位信息不完整或投递方式无效。",
        }
    }
    with client.session_factory() as db:  # type: ignore[attr-defined]
        persisted = db.get(JobPosting, posting.id)
        assert persisted is not None
        assert persisted.status is JobPostingStatus.PENDING_COMPLETION
        assert persisted.review_version == 0
        assert persisted.apply_url == "https://example.com/apply/1"
        assert _events_for(db, posting.id) == []


def test_source_sync_makes_read_version_stale_without_overwriting_canonical_fields(
    client: TestClient, seeded: dict[str, Any]
) -> None:
    posting = seeded["postings"][0]
    saved = client.patch(
        f"/api/admin/jobs/{posting.id}/completion",
        headers=seeded["admin_headers"],
        json={**COMPLETION_BODY, "company_name": "Reviewed Company"},
    )
    assert saved.status_code == 200

    queue = client.get(
        "/api/admin/jobs/review-queue?review_status=pending_review",
        headers=seeded["admin_headers"],
    )
    read_version = queue.json()["jobs"][0]["review_version"]
    assert read_version == 1

    with client.session_factory() as db:  # type: ignore[attr-defined]
        persisted = db.get(JobPosting, posting.id)
        assert persisted is not None
        source = db.get(JobSource, persisted.source_id)
        assert source is not None
        raw, created = job_repository.insert_raw_snapshot(
            db,
            source_id=source.id,
            external_record_id=persisted.external_record_id,
            raw_fields=[{"source": "changed"}],
            payload_hash="f" * 64,
            source_updated_at=NOW + timedelta(days=1),
            observed_at=NOW + timedelta(days=1),
        )
        assert created is True
        job_repository.upsert_posting(
            db,
            source=source,
            raw_record=raw,
            candidate=NormalizedJobCandidate(
                company_name="Upstream Company",
                title="Upstream Role",
                locations=["北京"],
                recruitment_types=["校招"],
                industries=["硬件"],
                apply_url="https://upstream.example.com/apply",
                referral_code="UPSTREAM",
                deadline_text="2027-01-01",
                source_updated_at=NOW + timedelta(days=1),
            ),
        )
        db.commit()

    response = client.patch(
        f"/api/admin/jobs/{posting.id}/completion",
        headers=seeded["admin_headers"],
        json={
            **COMPLETION_BODY,
            "expected_version": read_version,
            "company_name": "Stale Overwrite",
        },
    )

    assert response.status_code == 409
    assert response.json() == {
        "detail": {
            "error_code": "stale_job_review",
            "message": "职位审核版本已过期，请重新加载。",
        }
    }
    with client.session_factory() as db:  # type: ignore[attr-defined]
        persisted = db.get(JobPosting, posting.id)
        assert persisted is not None
        assert persisted.status is JobPostingStatus.PENDING_REVIEW
        assert persisted.review_version == read_version + 1
        assert persisted.company_name == "Reviewed Company"
        assert persisted.title == "后端实习生"
        assert persisted.source_changed_since_review is True
        assert [event.action for event in _events_for(db, posting.id)] == [
            "completion_saved"
        ]


def test_pending_source_change_makes_loaded_version_zero_stale(
    client: TestClient, seeded: dict[str, Any]
) -> None:
    posting = seeded["postings"][0]
    queue = client.get(
        "/api/admin/jobs/review-queue?review_status=pending_completion",
        headers=seeded["admin_headers"],
    )
    loaded = next(item for item in queue.json()["jobs"] if item["id"] == posting.id)
    assert loaded["review_version"] == 0

    with client.session_factory() as db:  # type: ignore[attr-defined]
        persisted = db.get(JobPosting, posting.id)
        assert persisted is not None
        source = db.get(JobSource, persisted.source_id)
        assert source is not None
        raw, created = job_repository.insert_raw_snapshot(
            db,
            source_id=source.id,
            external_record_id=persisted.external_record_id,
            raw_fields=[{"source": "changed-before-first-review"}],
            payload_hash="d" * 64,
            source_updated_at=NOW + timedelta(days=3),
            observed_at=NOW + timedelta(days=3),
        )
        assert created is True
        job_repository.upsert_posting(
            db,
            source=source,
            raw_record=raw,
            candidate=NormalizedJobCandidate(
                company_name="Fresh Source Company",
                title="Fresh Source Role",
                locations=["北京"],
                recruitment_types=["校招"],
                industries=["硬件"],
                apply_url="https://fresh-source.example.com/apply",
                referral_code="FRESH",
                deadline_text="2027-03-01",
                source_updated_at=NOW + timedelta(days=3),
            ),
        )
        db.commit()

    stale = client.patch(
        f"/api/admin/jobs/{posting.id}/completion",
        headers=seeded["admin_headers"],
        json={**COMPLETION_BODY, "expected_version": loaded["review_version"]},
    )

    assert stale.status_code == 409
    assert stale.json()["detail"]["error_code"] == "stale_job_review"
    with client.session_factory() as db:  # type: ignore[attr-defined]
        persisted = db.get(JobPosting, posting.id)
        assert persisted is not None
        assert persisted.status is JobPostingStatus.PENDING_COMPLETION
        assert persisted.company_name == "Fresh Source Company"
        assert persisted.title == "Fresh Source Role"
        assert persisted.review_version == 1
        assert persisted.source_changed_since_review is False
        assert _events_for(db, posting.id) == []


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
    _verify_postings(
        client,
        seeded,
        *(posting.id for posting in seeded["postings"]),
    )
    response = client.get(f"/api/jobs?{query}", headers=seeded["student_headers"])
    assert response.status_code == 200
    assert sorted(job["company_name"] for job in response.json()["jobs"]) == sorted(
        expected_companies
    )
    assert response.json()["total"] == len(expected_companies)


def test_list_has_stable_order_and_pagination(
    client: TestClient, seeded: dict[str, Any]
) -> None:
    _verify_postings(
        client,
        seeded,
        *(posting.id for posting in seeded["postings"]),
    )
    first = client.get("/api/jobs?limit=2", headers=seeded["student_headers"]).json()
    second = client.get(
        "/api/jobs?limit=2&offset=2", headers=seeded["student_headers"]
    ).json()

    with client.session_factory() as db:  # type: ignore[attr-defined]
        expected = sorted(
            [db.get(JobPosting, posting.id) for posting in seeded["postings"]],
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
