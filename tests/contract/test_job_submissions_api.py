import os
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

import fakeredis
import pytest

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("APP_AUTH_SECRET", "test-secret-with-at-least-32-characters")
os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")
os.environ.setdefault(
    "OBJECT_ENCRYPTION_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
)
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.api import dependencies
from backend.app.config import Settings
from backend.app.db.base import Base
from backend.app.db.models import (
    DeduplicationStatus, JobDuplicateCandidate, JobPosting, JobPostingStatus,
    JobSource, JobSourceProvider, RawJobRecord, SubmissionInputType,
    SubmissionStatus, User, UserJobSubmission, UserRole,
)
from backend.app.main import create_app
from backend.app.services.auth import AuthService


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
        "sqlite+pysqlite:///:memory:", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    redis = fakeredis.FakeRedis()

    def override_db() -> Iterator[Session]:
        with factory() as db:
            yield db

    app = create_app(settings, session_factory=factory)
    app.state.redis = redis
    app.dependency_overrides[dependencies._get_db] = override_db
    app.dependency_overrides[dependencies.get_redis] = lambda: redis
    with TestClient(app) as test_client:
        test_client.session_factory = factory  # type: ignore[attr-defined]
        yield test_client
    engine.dispose()


@pytest.fixture
def submission_seed(client: TestClient) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    with client.session_factory() as db:  # type: ignore[attr-defined]
        owner = User(account="submission-owner", nickname="Owner", password_hash="hash")
        other = User(account="submission-other", nickname="Other", password_hash="hash")
        admin = User(
            account="submission-admin", nickname="Admin", password_hash="hash",
            role=UserRole.ADMIN,
        )
        source = JobSource(
            source_key="submission-contract-source",
            provider=JobSourceProvider.TENCENT_SMARTSHEET,
            name="Contract Source", file_id="contract-file", sheet_id="contract-sheet",
            mapper_version="contract-v1", enabled=True,
        )
        db.add_all([owner, other, admin, source])
        db.flush()
        postings: list[JobPosting] = []
        for index, job_status in enumerate(
            [JobPostingStatus.VERIFIED, JobPostingStatus.PENDING_COMPLETION], start=1
        ):
            raw = RawJobRecord(
                source_id=source.id, external_record_id=f"contract-{index}",
                payload_hash=str(index) * 64, raw_fields=[], observed_at=now,
            )
            db.add(raw)
            db.flush()
            posting = JobPosting(
                source_id=source.id, external_record_id=f"contract-{index}", raw_record_id=raw.id,
                status=job_status, company_name="示例科技", title=f"后端实习生 {index}",
                description_text="负责 FastAPI 与 MySQL 后端服务开发",
                locations=["上海"], recruitment_types=["实习"], industries=["软件"],
                apply_url=f"https://jobs.example.com/{index}", mapper_version=source.mapper_version,
                source_candidate={},
            )
            db.add(posting)
            postings.append(posting)
        db.flush()
        item = UserJobSubmission(
            user_id=owner.id, input_type=SubmissionInputType.JD_TEXT,
            original_url=None, original_jd="PRIVATE CONTRACT JD",
            input_preview="示例科技招聘后端实习生", normalized_url=None,
            content_sha256="a" * 64, status=SubmissionStatus.SUBMITTED, version=2,
            deduplication_status=DeduplicationStatus.SUCCEEDED,
        )
        db.add(item)
        db.flush()
        db.add_all([JobDuplicateCandidate(
            submission_id=item.id, candidate_job_id=posting.id,
            generated_for_version=item.version, score_basis_points=9000 - index,
            reasons=["jd_token_overlap"], score_components={"jd_token_jaccard": 9000 - index},
            algorithm_version="manual-job-dedup-v1",
        ) for index, posting in enumerate(postings)])
        db.commit()
        auth = AuthService(client.app.state.settings)
        return {
            "owner_headers": {"Authorization": f"Bearer {auth.issue_user_token(owner)}"},
            "other_headers": {"Authorization": f"Bearer {auth.issue_user_token(other)}"},
            "admin_headers": {"Authorization": f"Bearer {auth.issue_user_token(admin)}"},
            "owner_account": owner.account, "submission_id": item.id,
            "version": item.version, "verified_job_id": postings[0].id,
        }


def test_student_create_list_and_cross_user_404(client, submission_seed) -> None:
    owner_headers = submission_seed["owner_headers"]
    other_headers = submission_seed["other_headers"]
    secret_jd = "PRIVATE-JD-DO-NOT-RETURN " + "负责 FastAPI 与 MySQL。" * 30
    created = client.post(
        "/api/job-submissions",
        headers=owner_headers,
        json={"input_type": "jd_text", "jd_text": secret_jd},
    )
    assert created.status_code == 201
    body = created.json()
    assert set(body) == {
        "id", "input_type", "input_preview", "normalized_url", "status", "version",
        "deduplication_status", "deduplication_error_code", "promoted_job_id",
        "created_at", "updated_at",
    }
    assert secret_jd not in created.text
    assert "user_id" not in created.text
    assert client.get(f"/api/job-submissions/{body['id']}", headers=other_headers).status_code == 404
    listed = client.get("/api/job-submissions", headers=owner_headers).json()
    assert body["id"] in {item["id"] for item in listed["submissions"]}


def test_student_duplicate_candidates_only_expose_verified_jobs(client, submission_seed) -> None:
    response = client.get(
        f"/api/job-submissions/{submission_seed['submission_id']}/duplicate-candidates",
        headers=submission_seed["owner_headers"],
    )
    assert response.status_code == 200
    assert {item["job"]["status"] for item in response.json()["candidates"]} <= {"verified"}
    assert "submission_id" not in response.text


def test_stale_update_returns_stable_409_without_input(client, submission_seed) -> None:
    response = client.patch(
        f"/api/job-submissions/{submission_seed['submission_id']}",
        headers=submission_seed["owner_headers"],
        json={
            "expected_version": 99, "input_type": "url",
            "url": "https://jobs.example.com/new",
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "stale_job_submission", "message": "提交版本已过期，请重新加载。"
    }


def test_admin_queue_and_decision_never_return_submitter_identity(client, submission_seed) -> None:
    response = client.get(
        "/api/admin/job-submissions?status=submitted&limit=20&offset=0",
        headers=submission_seed["admin_headers"],
    )
    assert response.status_code == 200
    assert "user_id" not in response.text
    assert submission_seed["owner_account"] not in response.text
    decision = client.post(
        f"/api/admin/job-submissions/{submission_seed['submission_id']}/decision",
        headers=submission_seed["admin_headers"],
        json={
            "expected_version": submission_seed["version"],
            "action": "link_existing", "job_id": submission_seed["verified_job_id"],
        },
    )
    assert decision.status_code == 200
    assert decision.json()["status"] == "promoted"


def test_student_cannot_use_admin_submission_queue(client, submission_seed) -> None:
    response = client.get(
        "/api/admin/job-submissions?status=submitted",
        headers=submission_seed["owner_headers"],
    )
    assert response.status_code == 403


def test_admin_candidates_can_include_non_public_jobs_without_submitter_identity(client, submission_seed) -> None:
    response = client.get(
        f"/api/admin/job-submissions/{submission_seed['submission_id']}/duplicate-candidates",
        headers=submission_seed["admin_headers"],
    )
    assert response.status_code == 200
    assert {item["job"]["status"] for item in response.json()["candidates"]} == {
        "verified", "pending_completion"
    }
    assert "user_id" not in response.text
    assert submission_seed["owner_account"] not in response.text
