from __future__ import annotations

import os
import uuid
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
    JobPosting,
    JobPostingStatus,
    JobSource,
    JobSourceProvider,
    RawJobRecord,
    User,
    UserRole,
)
from backend.app.services.auth import AuthService
from backend.app.main import create_app
from tests.conftest import settings_override


PASSWORD = "test-password-1234"


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


def _seed_job(db: Session, *, job_id: str, source_id: str, raw_id: str) -> None:
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
        status=JobPostingStatus.VERIFIED,
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
        auth = AuthService(client.app.state.settings)
        token = auth.issue_user_token(user)
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
        auth = AuthService(client.app.state.settings)
        token = auth.issue_user_token(admin)
        return {"Authorization": f"Bearer {token}"}


def _idem_key() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex  # 64 chars


class TestStudentFeedbackApi:
    def test_create_feedback_success(self, client: TestClient) -> None:
        job_id = str(uuid.uuid4())
        with client.session_factory() as db:
            _seed_job(db, job_id=job_id, source_id=str(uuid.uuid4()), raw_id=str(uuid.uuid4()))

        headers, _user_id = _student_headers(client)
        idem_key = _idem_key()
        response = client.post(
            "/api/feedbacks",
            json={"job_id": job_id, "category": "closed", "note": "Job is closed"},
            headers={**headers, "Idempotency-Key": idem_key},
        )
        assert response.status_code == 201
        data = response.json()
        assert data["job_id"] == job_id
        assert data["category"] == "closed"
        assert data["note"] == "Job is closed"
        assert "id" in data
        assert "created_at" in data

    def test_create_feedback_missing_idempotency_key(self, client: TestClient) -> None:
        headers, _user_id = _student_headers(client)
        response = client.post(
            "/api/feedbacks",
            json={"job_id": str(uuid.uuid4()), "category": "closed"},
            headers=headers,
        )
        assert response.status_code == 400
        assert "invalid_idempotency_key" in response.text

    def test_create_feedback_short_idempotency_key(self, client: TestClient) -> None:
        headers, _user_id = _student_headers(client)
        response = client.post(
            "/api/feedbacks",
            json={"job_id": str(uuid.uuid4()), "category": "closed"},
            headers={**headers, "Idempotency-Key": "short"},
        )
        assert response.status_code == 400

    def test_idempotency_key_dedup(self, client: TestClient) -> None:
        job_id = str(uuid.uuid4())
        with client.session_factory() as db:
            _seed_job(db, job_id=job_id, source_id=str(uuid.uuid4()), raw_id=str(uuid.uuid4()))

        headers, _user_id = _student_headers(client)
        idem_key = _idem_key()
        response1 = client.post(
            "/api/feedbacks",
            json={"job_id": job_id, "category": "content_changed"},
            headers={**headers, "Idempotency-Key": idem_key},
        )
        assert response1.status_code == 201

        response2 = client.post(
            "/api/feedbacks",
            json={"job_id": job_id, "category": "content_changed"},
            headers={**headers, "Idempotency-Key": idem_key},
        )
        assert response2.status_code == 409

    def test_list_feedbacks(self, client: TestClient) -> None:
        job_id = str(uuid.uuid4())
        with client.session_factory() as db:
            _seed_job(db, job_id=job_id, source_id=str(uuid.uuid4()), raw_id=str(uuid.uuid4()))

        headers, user_id = _student_headers(client)
        for i in range(3):
            client.post(
                "/api/feedbacks",
                json={"job_id": job_id, "category": "closed", "note": f"note-{i}"},
                headers={**headers, "Idempotency-Key": _idem_key() + str(i)},
            )

        response = client.get("/api/feedbacks", headers=headers)
        assert response.status_code == 200
        data = response.json()
        assert data["total"] >= 3
        assert len(data["feedbacks"]) >= 3

    def test_list_feedbacks_filter_by_job(self, client: TestClient) -> None:
        job_a = str(uuid.uuid4())
        job_b = str(uuid.uuid4())
        with client.session_factory() as db:
            _seed_job(db, job_id=job_a, source_id=str(uuid.uuid4()), raw_id=str(uuid.uuid4()))
            _seed_job(db, job_id=job_b, source_id=str(uuid.uuid4()), raw_id=str(uuid.uuid4()))

        headers, _user_id = _student_headers(client)
        client.post(
            "/api/feedbacks",
            json={"job_id": job_a, "category": "closed"},
            headers={**headers, "Idempotency-Key": _idem_key()},
        )
        client.post(
            "/api/feedbacks",
            json={"job_id": job_b, "category": "closed"},
            headers={**headers, "Idempotency-Key": _idem_key()},
        )

        response = client.get(f"/api/feedbacks?job_id={job_a}", headers=headers)
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert data["feedbacks"][0]["job_id"] == job_a

    def test_student_cannot_see_other_student_feedback(self, client: TestClient) -> None:
        """Student can only see own feedback via get by id."""
        job_id = str(uuid.uuid4())
        with client.session_factory() as db:
            _seed_job(db, job_id=job_id, source_id=str(uuid.uuid4()), raw_id=str(uuid.uuid4()))

        headers1, _user1 = _student_headers(client)
        resp = client.post(
            "/api/feedbacks",
            json={"job_id": job_id, "category": "closed"},
            headers={**headers1, "Idempotency-Key": _idem_key()},
        )
        feedback_id = resp.json()["id"]

        headers2, _user2 = _student_headers(client)
        response = client.get(f"/api/feedbacks/{feedback_id}", headers=headers2)
        assert response.status_code == 404


class TestAdminFeedbackApi:
    def test_admin_list_all_feedbacks(self, client: TestClient) -> None:
        job_id = str(uuid.uuid4())
        with client.session_factory() as db:
            _seed_job(db, job_id=job_id, source_id=str(uuid.uuid4()), raw_id=str(uuid.uuid4()))
        headers, _user_id = _student_headers(client)
        client.post(
            "/api/feedbacks",
            json={"job_id": job_id, "category": "incorrect_information"},
            headers={**headers, "Idempotency-Key": _idem_key()},
        )

        admin_headers = _admin_headers(client)
        response = client.get("/api/admin/feedbacks", headers=admin_headers)
        assert response.status_code == 200
        data = response.json()
        assert data["total"] >= 1
        # Admin DTO must NOT expose user_id, account, nickname, or idempotency_key
        admin_feedback = data["feedbacks"][0]
        assert "user_id" not in admin_feedback
        assert "account" not in admin_feedback
        assert "nickname" not in admin_feedback
        assert "idempotency_key" not in admin_feedback

    def test_admin_get_feedback(self, client: TestClient) -> None:
        job_id = str(uuid.uuid4())
        with client.session_factory() as db:
            _seed_job(db, job_id=job_id, source_id=str(uuid.uuid4()), raw_id=str(uuid.uuid4()))
        headers, _user_id = _student_headers(client)
        resp = client.post(
            "/api/feedbacks",
            json={"job_id": job_id, "category": "content_changed"},
            headers={**headers, "Idempotency-Key": _idem_key()},
        )
        feedback_id = resp.json()["id"]

        admin_headers = _admin_headers(client)
        response = client.get(f"/api/admin/feedbacks/{feedback_id}", headers=admin_headers)
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == feedback_id
        assert "user_id" not in data

    def test_admin_requires_admin_role(self, client: TestClient) -> None:
        headers, _user_id = _student_headers(client)
        response = client.get("/api/admin/feedbacks", headers=headers)
        assert response.status_code == 403


class TestFeedbackCategories:
    def test_all_categories_accepted(self, client: TestClient) -> None:
        job_id = str(uuid.uuid4())
        with client.session_factory() as db:
            _seed_job(db, job_id=job_id, source_id=str(uuid.uuid4()), raw_id=str(uuid.uuid4()))

        headers, _user_id = _student_headers(client)
        categories = [
            "closed",
            "application_channel_unavailable",
            "content_changed",
            "incorrect_information",
        ]
        for category in categories:
            response = client.post(
                "/api/feedbacks",
                json={"job_id": job_id, "category": category},
                headers={**headers, "Idempotency-Key": _idem_key() + category[:8]},
            )
            assert response.status_code == 201, f"category {category} failed"

    def test_invalid_category_rejected(self, client: TestClient) -> None:
        headers, _user_id = _student_headers(client)
        response = client.post(
            "/api/feedbacks",
            json={"job_id": str(uuid.uuid4()), "category": "invalid_category"},
            headers={**headers, "Idempotency-Key": _idem_key()},
        )
        assert response.status_code == 422
