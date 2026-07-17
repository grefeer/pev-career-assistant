from __future__ import annotations

from collections.abc import Iterator
import os
import uuid

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
    JobPosting,
    JobPostingStatus,
    JobSource,
    JobSourceProvider,
    RawJobRecord,
    User,
    UserRole,
)
from backend.app.main import create_app
from backend.app.services.auth import AuthService
from backend.app.services.rate_limit import RateLimitExceededError, RateLimitUnavailableError
from tests.conftest import settings_override


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
        test_client.session_factory = session_factory
        yield test_client
    engine.dispose()


def _seed_job(
    db: Session,
    *,
    job_id: str,
    source_id: str,
    raw_id: str,
    posting_status: JobPostingStatus = JobPostingStatus.VERIFIED,
) -> None:
    source = JobSource(
        id=source_id,
        source_key=f"feedback-test-source-{source_id[:8]}",
        provider=JobSourceProvider.TENCENT_SMARTSHEET,
        name="Feedback Test Source",
        file_id=f"ft-file-{source_id[:8]}",
        sheet_id=f"ft-sheet-{source_id[:8]}",
        mapper_version="ft-v1",
        enabled=True,
    )
    raw = RawJobRecord(
        id=raw_id,
        source_id=source_id,
        external_record_id="feedback-test-record",
        payload_hash="f" * 64,
        raw_fields=[{"field": "test", "value": "test"}],
    )
    posting = JobPosting(
        id=job_id,
        source_id=source_id,
        external_record_id="feedback-test-record",
        raw_record_id=raw_id,
        status=posting_status,
        company_name="Feedback Test Corp",
        title="Test Role",
        locations=[],
        recruitment_types=[],
        industries=[],
        apply_url="https://example.com/feedback-test",
        mapper_version="ft-v1",
        source_candidate={},
    )
    db.add_all([source, raw, posting])
    db.commit()


def _student_headers(client: TestClient) -> tuple[dict[str, str], str]:
    with client.session_factory() as db:
        user = User(
            account=f"fb-student-{uuid.uuid4().hex[:8]}",
            nickname="FB Student",
            password_hash="hash",
            role=UserRole.STUDENT,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        token = AuthService(client.app.state.settings).issue_user_token(user)
        return {"Authorization": f"Bearer {token}"}, user.id


def _admin_headers(client: TestClient) -> dict[str, str]:
    with client.session_factory() as db:
        admin = User(
            account=f"fb-admin-{uuid.uuid4().hex[:8]}",
            nickname="FB Admin",
            password_hash="hash",
            role=UserRole.ADMIN,
        )
        db.add(admin)
        db.commit()
        db.refresh(admin)
        token = AuthService(client.app.state.settings).issue_user_token(admin)
        return {"Authorization": f"Bearer {token}"}


class TestAuthoritativeJobFeedbackApi:
    def test_non_verified_job_is_hidden(self, client: TestClient) -> None:
        job_id = str(uuid.uuid4())
        with client.session_factory() as db:
            _seed_job(
                db,
                job_id=job_id,
                source_id=str(uuid.uuid4()),
                raw_id=str(uuid.uuid4()),
                posting_status=JobPostingStatus.PENDING_REVIEW,
            )
        headers, _ = _student_headers(client)
        response = client.post(
            f"/api/jobs/{job_id}/feedback",
            headers={**headers, "Idempotency-Key": "student-api-key-0010"},
            json={
                "action": "upsert",
                "category": "closed",
                "expected_version": None,
                "note": None,
            },
        )
        assert response.status_code == 404

    @pytest.mark.parametrize(
        ("failure", "expected_status"),
        [(RateLimitExceededError(), 429), (RateLimitUnavailableError(), 503)],
    )
    def test_student_write_rate_limit_mapping(
        self,
        client: TestClient,
        failure: Exception,
        expected_status: int,
    ) -> None:
        class FailingLimiter:
            def check(self, **_: object) -> None:
                raise failure

        job_id = str(uuid.uuid4())
        with client.session_factory() as db:
            _seed_job(
                db,
                job_id=job_id,
                source_id=str(uuid.uuid4()),
                raw_id=str(uuid.uuid4()),
            )
        headers, _ = _student_headers(client)
        client.app.state.job_feedback_rate_limiter = FailingLimiter()
        try:
            response = client.post(
                f"/api/jobs/{job_id}/feedback",
                headers={**headers, "Idempotency-Key": "student-api-key-0011"},
                json={
                    "action": "upsert",
                    "category": "closed",
                    "expected_version": None,
                    "note": None,
                },
            )
        finally:
            del client.app.state.job_feedback_rate_limiter
        assert response.status_code == expected_status

    def test_student_create_replay_conflict_list_and_withdraw(
        self, client: TestClient
    ) -> None:
        job_id = str(uuid.uuid4())
        with client.session_factory() as db:
            _seed_job(
                db,
                job_id=job_id,
                source_id=str(uuid.uuid4()),
                raw_id=str(uuid.uuid4()),
            )
        headers, _ = _student_headers(client)
        write_headers = {**headers, "Idempotency-Key": "student-api-key-0001"}
        payload = {
            "action": "upsert",
            "category": "closed",
            "expected_version": None,
            "note": "官网已关闭",
        }
        created = client.post(
            f"/api/jobs/{job_id}/feedback", headers=write_headers, json=payload
        )
        replayed = client.post(
            f"/api/jobs/{job_id}/feedback", headers=write_headers, json=payload
        )
        conflict = client.post(
            f"/api/jobs/{job_id}/feedback",
            headers=write_headers,
            json={**payload, "note": "different"},
        )
        assert created.status_code == replayed.status_code == 200
        assert created.json() == replayed.json()
        assert conflict.status_code == 409
        assert conflict.json()["detail"]["error_code"] == "idempotency_key_reused"
        listed = client.get(f"/api/jobs/{job_id}/feedback", headers=headers)
        assert listed.json()["feedback"][0]["note"] == "官网已关闭"
        withdrawn = client.post(
            f"/api/jobs/{job_id}/feedback",
            headers={**headers, "Idempotency-Key": "student-api-key-0002"},
            json={
                "action": "withdraw",
                "category": "closed",
                "expected_version": created.json()["version"],
                "note": None,
            },
        )
        assert withdrawn.status_code == 200
        assert withdrawn.json()["status"] == "withdrawn"

    def test_admin_queue_and_decision_are_identity_free(self, client: TestClient) -> None:
        job_id = str(uuid.uuid4())
        with client.session_factory() as db:
            _seed_job(
                db,
                job_id=job_id,
                source_id=str(uuid.uuid4()),
                raw_id=str(uuid.uuid4()),
            )
        student_headers, _ = _student_headers(client)
        created = client.post(
            f"/api/jobs/{job_id}/feedback",
            headers={**student_headers, "Idempotency-Key": "student-api-key-0003"},
            json={
                "action": "upsert",
                "category": "incorrect_information",
                "expected_version": None,
                "note": "信息错误",
            },
        )
        admin_headers = _admin_headers(client)
        queue = client.get("/api/admin/job-feedback", headers=admin_headers)
        assert queue.status_code == 200
        assert queue.json()["aggregates"][0]["total_count"] == 1
        assert set(queue.json()["feedback"][0]).isdisjoint(
            {"user_id", "account", "nickname", "idempotency_key"}
        )
        decided = client.post(
            f"/api/admin/job-feedback/{created.json()['id']}/decision",
            headers={**admin_headers, "Idempotency-Key": "admin-api-key-0001"},
            json={"decision": "resolve", "expected_version": 1},
        )
        assert decided.status_code == 200
        assert decided.json()["status"] == "resolved"

    def test_student_is_forbidden_from_admin_queue(self, client: TestClient) -> None:
        headers, _ = _student_headers(client)
        assert client.get("/api/admin/job-feedback", headers=headers).status_code == 403

    def test_invalid_category_rejected(self, client: TestClient) -> None:
        headers, _ = _student_headers(client)
        response = client.post(
            f"/api/jobs/{uuid.uuid4()}/feedback",
            json={
                "action": "upsert",
                "category": "invalid_category",
                "expected_version": None,
                "note": None,
            },
            headers={**headers, "Idempotency-Key": uuid.uuid4().hex},
        )
        assert response.status_code == 422
