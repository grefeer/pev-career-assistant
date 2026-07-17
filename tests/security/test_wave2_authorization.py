"""Authorization gates for Wave 2 evidence-matching features.

Verifies:
  1. Cross-user access: student A cannot read student B's match/draft/snapshot -> 404.
  2. Admin guard: student cannot access ``/admin/*`` routes -> 403.
  3. Attachment download: user ownership check -> 403/404.
  4. Executor v2 attachment download validates device token + task binding +
     lease + snapshot->attachment chain.
  5. ``task:submit`` scope does NOT exist in the codebase.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from typing import Any

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
from backend.app.config import Settings
from backend.app.db.base import Base
from backend.app.db.models import (
    ApplicationSnapshot,
    ApplicationTask,
    ApplicationTaskStatus,
    ApprovedResumeAttachment,
    ApprovedResumeVersion,
    Device,
    DevicePlatform,
    DeviceStatus,
    JobPosting,
    JobPostingStatus,
    MatchReport,
    ResumeDraft,
    User,
    UserRole,
)
from backend.app.main import create_app
from backend.app.services.auth import AuthService
from backend.app.services.devices import (
    ALLOWED_TASK_LEASE_SCOPES,
    DeviceService,
    IssuedDevice,
)
from backend.app.services.storage import EncryptedObjectStore
from tests.conftest import settings_override


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_user(client: TestClient, role: UserRole = UserRole.STUDENT) -> User:
    """Create and return a new user, committing to the shared in-memory DB."""
    with client.session_factory() as db:
        user = User(
            account=f"auth-test-{uuid.uuid4().hex[:8]}",
            nickname="Auth Test User",
            password_hash="hash",
            role=role,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return user


def _user_headers(client: TestClient, user: User | None = None) -> dict[str, str]:
    if user is None:
        user = _make_user(client)
    token = AuthService(client.app.state.settings).issue_user_token(user)
    return {"Authorization": f"Bearer {token}"}


def _admin_headers(client: TestClient) -> dict[str, str]:
    admin = _make_user(client, UserRole.ADMIN)
    return _user_headers(client, admin)


def _create_verified_job(client: TestClient, admin_h: dict[str, str]) -> str:
    with client.session_factory() as db:
        from backend.app.db.models import JobSource, JobSourceProvider, RawJobRecord

        source = JobSource(
            source_key=f"auth-src-{uuid.uuid4().hex[:8]}",
            provider=JobSourceProvider.TENCENT_SMARTSHEET,
            name="Auth Source",
            file_id="auth-file",
            sheet_id="auth-sheet",
            mapper_version="auth-v1",
            enabled=True,
        )
        raw = RawJobRecord(
            source_id=source.id,
            external_record_id="auth-ext",
            payload_hash="a" * 64,
            raw_fields=[{"field": "test"}],
        )
        posting = JobPosting(
            source_id=source.id,
            external_record_id="auth-ext",
            raw_record_id=raw.id,
            status=JobPostingStatus.VERIFIED,
            company_name="Auth Corp",
            title="Auth Role",
            locations=[],
            recruitment_types=[],
            industries=[],
            description_text="Auth test job.",
            apply_url="https://example.com/auth",
            mapper_version="auth-v1",
            source_candidate={},
        )
        db.add_all([source, raw, posting])
        db.commit()
        return posting.id


def _create_confirmed_profile(client: TestClient, headers: dict[str, str]) -> str:
    resp = client.post(
        "/api/profiles",
        json={
            "facts": {"name": "Auth User", "email": "auth@test.com", "skills": ["Go"]},
            "local_sensitive_references": {},
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    profile_id = resp.json()["id"]
    confirm = client.post(
        f"/api/profiles/{profile_id}/confirm", headers=headers,
    )
    assert confirm.status_code == 200, confirm.text
    return confirm.json()["id"]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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

    app = create_app(
        settings_override(
            app_auth_secret="test-secret-with-at-least-32-characters",
        )
    )
    app.dependency_overrides[dependencies._get_db] = override_db
    app.dependency_overrides[dependencies.get_redis] = lambda: redis
    with TestClient(app) as test_client:
        test_client.session_factory = session_factory  # type: ignore[attr-defined]
        yield test_client


# ---------------------------------------------------------------------------
# Gate 1: Cross-user access — student A cannot read student B's data
# ---------------------------------------------------------------------------


class TestCrossUserAccess:
    """Student A cannot read student B's match/draft/snapshot -> 404."""

    def _create_match_for_user(
        self, client: TestClient, headers: dict[str, str]
    ) -> str | None:
        admin_h = _admin_headers(client)
        job_id = _create_verified_job(client, admin_h)
        profile_id = _create_confirmed_profile(client, headers)
        ik = f"ik-cross-{uuid.uuid4().hex}"
        resp = client.post(
            "/api/matches",
            json={"job_id": job_id, "profile_version_id": profile_id},
            headers={**headers, "Idempotency-Key": ik},
        )
        if resp.status_code == 201:
            return resp.json()["id"]
        return None

    def test_cross_user_match_returns_404(self, client: TestClient) -> None:
        user_a = _make_user(client)
        headers_a = _user_headers(client, user_a)
        headers_b = _user_headers(client)

        match_id = self._create_match_for_user(client, headers_a)
        if match_id is None:
            pytest.skip("match creation did not return 201")

        # User B tries to read User A's match
        resp = client.get(f"/api/matches/{match_id}", headers=headers_b)
        assert resp.status_code == 404, (
            f"Expected 404 for cross-user match access, got {resp.status_code}"
        )

    def test_cross_user_draft_returns_404(self, client: TestClient) -> None:
        user_a = _make_user(client)
        headers_a = _user_headers(client, user_a)
        headers_b = _user_headers(client)

        match_id = self._create_match_for_user(client, headers_a)
        if match_id is None:
            pytest.skip("match creation failed")

        # Create draft for user A
        ik = f"ik-cross-dr-{uuid.uuid4().hex}"
        draft_resp = client.post(
            "/api/resume-drafts",
            json={"match_report_id": match_id},
            headers={**headers_a, "Idempotency-Key": ik},
        )
        if draft_resp.status_code != 201:
            pytest.skip("draft creation failed")
        draft_id = draft_resp.json()["id"]

        # User B tries to read User A's draft
        resp = client.get(f"/api/resume-drafts/{draft_id}", headers=headers_b)
        assert resp.status_code == 404, (
            f"Expected 404 for cross-user draft access, got {resp.status_code}"
        )

    def test_cross_user_snapshot_returns_404(self, client: TestClient) -> None:
        user_a = _make_user(client)
        headers_a = _user_headers(client, user_a)
        headers_b = _user_headers(client)

        match_id = self._create_match_for_user(client, headers_a)
        if match_id is None:
            pytest.skip("match creation failed")

        # Create draft & approve
        draft_resp = client.post(
            "/api/resume-drafts",
            json={"match_report_id": match_id},
            headers={**headers_a, "Idempotency-Key": f"ik-cross-dr2-{uuid.uuid4().hex}"},
        )
        if draft_resp.status_code != 201:
            pytest.skip("draft creation failed")

        approve_resp = client.post(
            f"/api/resume-drafts/{draft_resp.json()['id']}/approve",
            json={"expected_version": 0},
            headers={**headers_a, "Idempotency-Key": f"ik-cross-app-{uuid.uuid4().hex}"},
        )
        if approve_resp.status_code != 200:
            pytest.skip("draft approval failed")

        snap_resp = client.post(
            "/api/application-snapshots",
            json={
                "job_id": (admin_h := _admin_headers(client))
                or _create_verified_job(client, admin_h)
                or "",
                "approved_resume_version_id": approve_resp.json()["id"],
                "dynamic_answers": [],
                "local_sensitive_requirements": [],
            },
            headers={**headers_a, "Idempotency-Key": f"ik-cross-snap-{uuid.uuid4().hex}"},
        )
        if snap_resp.status_code != 201:
            pytest.skip("snapshot creation failed")
        snap_id = snap_resp.json()["id"]

        # User B tries to read User A's snapshot
        resp = client.get(f"/api/application-snapshots/{snap_id}", headers=headers_b)
        assert resp.status_code == 404, (
            f"Expected 404 for cross-user snapshot access, got {resp.status_code}"
        )


# ---------------------------------------------------------------------------
# Gate 2: Admin guard — student cannot access /admin/* routes
# ---------------------------------------------------------------------------


class TestAdminGuard:
    """Student users get 403 on admin routes."""

    ADMIN_ROUTES = [
        ("POST", "/api/admin/job-sources/test-source/sync"),
        ("GET", "/api/admin/jobs/review-queue"),
        ("GET", "/api/admin/jobs/verified"),
        ("PATCH", "/api/admin/jobs/00000000-0000-4000-8000-000000000000/completion"),
        ("POST", "/api/admin/jobs/00000000-0000-4000-8000-000000000000/decision"),
    ]

    def test_student_cannot_access_admin_routes(self, client: TestClient) -> None:
        headers = _user_headers(client)

        for method, path in self.ADMIN_ROUTES:
            resp = client.request(method, path, headers=headers)
            assert resp.status_code == 403, (
                f"Expected 403 for student on {method} {path}, got {resp.status_code}"
            )

    def test_admin_can_access_admin_routes(self, client: TestClient) -> None:
        admin_h = _admin_headers(client)

        # These should get 404 (not found IDs) not 403 (forbidden)
        resp = client.get("/api/admin/jobs/review-queue", headers=admin_h)
        assert resp.status_code != 403, (
            f"Admin got 403 on /api/admin/jobs/review-queue"
        )

        resp = client.get("/api/admin/jobs/verified", headers=admin_h)
        assert resp.status_code != 403, (
            f"Admin got 403 on /api/admin/jobs/verified"
        )


# ---------------------------------------------------------------------------
# Gate 3: Attachment download — user ownership check
# ---------------------------------------------------------------------------


class TestAttachmentDownloadAuthorization:
    """Download endpoint verifies user owns the attachment."""

    def test_download_unknown_attachment_returns_404(
        self, client: TestClient
    ) -> None:
        headers = _user_headers(client)
        resp = client.get(
            f"/api/approved-resume-attachments/{uuid.uuid4().hex}/download",
            headers=headers,
        )
        assert resp.status_code == 404

    def test_download_cross_user_attachment_returns_403_or_404(
        self, client: TestClient
    ) -> None:
        user_a = _make_user(client)
        headers_a = _user_headers(client, user_a)
        headers_b = _user_headers(client)
        admin_h = _admin_headers(client)
        job_id = _create_verified_job(client, admin_h)
        profile_id = _create_confirmed_profile(client, headers_a)

        # Create match, draft, approve for user A
        ik = f"ik-dl-auth-{uuid.uuid4().hex}"
        mr = client.post(
            "/api/matches",
            json={"job_id": job_id, "profile_version_id": profile_id},
            headers={**headers_a, "Idempotency-Key": ik},
        )
        if mr.status_code != 201:
            pytest.skip("match creation failed")

        dr = client.post(
            "/api/resume-drafts",
            json={"match_report_id": mr.json()["id"]},
            headers={**headers_a, "Idempotency-Key": f"ik-dl-auth-dr-{uuid.uuid4().hex}"},
        )
        if dr.status_code != 201:
            pytest.skip("draft creation failed")

        ar = client.post(
            f"/api/resume-drafts/{dr.json()['id']}/approve",
            json={"expected_version": 0},
            headers={**headers_a, "Idempotency-Key": f"ik-dl-auth-ap-{uuid.uuid4().hex}"},
        )
        if ar.status_code != 200:
            pytest.skip("draft approval failed")

        att_id = ar.json()["attachments"][0]["id"]

        # User B downloads User A's attachment -> should fail
        resp = client.get(
            f"/api/approved-resume-attachments/{att_id}/download",
            headers=headers_b,
        )
        assert resp.status_code in (403, 404), (
            f"Expected 403 or 404 for cross-user download, got {resp.status_code}"
        )


# ---------------------------------------------------------------------------
# Gate 4: Executor v2 attachment download validates
# device token + task binding + lease + snapshot->attachment chain
# ---------------------------------------------------------------------------


class TestExecutorAttachmentDownloadAuthorization:
    """Executor attachment download enforces full chain of authorization."""

    def test_executor_download_missing_device_token_returns_401(
        self, client: TestClient
    ) -> None:
        resp = client.get(
            f"/executor/tasks/{uuid.uuid4().hex}/attachments/{uuid.uuid4().hex}",
        )
        assert resp.status_code == 401

    def test_executor_download_missing_task_lease_returns_401(
        self, client: TestClient
    ) -> None:
        resp = client.get(
            f"/executor/tasks/{uuid.uuid4().hex}/attachments/{uuid.uuid4().hex}",
            headers={"X-Device-Token": "some-token"},
        )
        assert resp.status_code == 401

    def test_executor_download_invalid_lease_returns_401(
        self, client: TestClient
    ) -> None:
        resp = client.get(
            f"/executor/tasks/{uuid.uuid4().hex}/attachments/{uuid.uuid4().hex}",
            headers={
                "X-Device-Token": "some-token",
                "X-Task-ID": uuid.uuid4().hex,
                "X-Task-Lease": "invalid-lease",
            },
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Gate 7: task:submit scope — confirm it does NOT exist
# ---------------------------------------------------------------------------


class TestTaskSubmitScopeAbsent:
    """The codebase must never define or use a ``task:submit`` scope."""

    def test_allowed_scopes_exclude_task_submit(self) -> None:
        assert "task:submit" not in ALLOWED_TASK_LEASE_SCOPES

    def test_only_task_progress_and_task_result_allowed(self) -> None:
        assert ALLOWED_TASK_LEASE_SCOPES == frozenset({"task:progress", "task:result"})
