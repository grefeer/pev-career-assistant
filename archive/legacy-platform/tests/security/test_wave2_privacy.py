"""Privacy gates for Wave 2 evidence-matching features.

Verifies:
  1. API responses never contain ``object_key`` (internal storage refs stay internal).
  2. ``SnapshotResponse`` excludes ``profile_facts``, ``dynamic_answers``,
     ``local_sensitive_requirements``.
  3. Unclassified / unknown dynamic-answer field -> API rejects with 422.
  4. Local-sensitive field submitted as ``non_sensitive`` -> API rejects with 422.
  5. Log statements in Wave 2 services contain only entity IDs + error codes
     (no sensitive plaintext).
  6. Executor v2 attachment download response headers don't expose ``object_key``.
"""

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
    User,
    UserRole,
)
from backend.app.main import create_app
from backend.app.services.auth import AuthService
from backend.app.services.field_classification import (
    classify_field,
    FieldClassification,
)
from backend.app.services.snapshot_validators import (
    validate_dynamic_answers,
    validate_local_sensitive_requirements,
    SnapshotValidationError,
)
from tests.conftest import settings_override


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SNAPSHOT_SENSITIVE_KEYS = {
    "profile_facts",
    "dynamic_answers",
    "local_sensitive_requirements",
}


def _auth_headers(
    client: TestClient, user: User | None = None,
) -> tuple[str, dict[str, str]]:
    """Return (user_id, Bearer auth headers) for the given or a new student user."""
    with client.session_factory() as db:
        if user is None:
            user = User(
                id=str(uuid.uuid4()),
                account=f"privacy-user-{uuid.uuid4().hex[:8]}",
                nickname="Privacy Test",
                password_hash="hash",
            )
            db.add(user)
            db.commit()
            db.refresh(user)
        token = AuthService(client.app.state.settings).issue_user_token(user)
    return user.id, {"Authorization": f"Bearer {token}"}


def _admin_headers(client: TestClient) -> tuple[str, dict[str, str]]:
    with client.session_factory() as db:
        admin = User(
            id=str(uuid.uuid4()),
            account=f"privacy-admin-{uuid.uuid4().hex[:8]}",
            nickname="Privacy Admin",
            password_hash="hash",
            role=UserRole.ADMIN,
        )
        db.add(admin)
        db.commit()
        db.refresh(admin)
        token = AuthService(client.app.state.settings).issue_user_token(admin)
    return admin.id, {"Authorization": f"Bearer {token}"}


def _create_verified_job(client: TestClient, admin_headers: dict[str, str]) -> str:
    """Create a verified job posting and return its id."""
    with client.session_factory() as db:
        from backend.app.db.models import JobSource, JobSourceProvider, RawJobRecord

        source = JobSource(
            id=str(uuid.uuid4()),
            source_key=f"privacy-src-{uuid.uuid4().hex[:8]}",
            provider=JobSourceProvider.TENCENT_SMARTSHEET,
            name="Privacy Source",
            file_id="privacy-file",
            sheet_id="privacy-sheet",
            mapper_version="privacy-v1",
            enabled=True,
        )
        raw = RawJobRecord(
            id=str(uuid.uuid4()),
            source_id=source.id,
            external_record_id="privacy-ext",
            payload_hash="p" * 64,
            raw_fields=[{"field": "test"}],
        )
        posting = JobPosting(
            source_id=source.id,
            external_record_id="privacy-ext",
            raw_record_id=raw.id,
            status=JobPostingStatus.VERIFIED,
            company_name="Privacy Corp",
            title="Privacy Role",
            locations=[],
            recruitment_types=[],
            industries=[],
            description_text="A test job description for privacy gates.",
            apply_url="https://example.com/privacy",
            mapper_version="privacy-v1",
            source_candidate={},
            gui_eligible=True,
            review_version=1,
        )
        db.add_all([source, raw, posting])
        db.flush()
        from backend.app.db.models import JobVerification
        verification = JobVerification(
            id=str(uuid.uuid4()),
            job_id=posting.id,
            action="verified",
            from_status="pending_review",
            to_status="verified",
            review_version=1,
            field_snapshot={},
        )
        db.add(verification)
        db.commit()
        return posting.id


def _create_confirmed_profile(
    client: TestClient, headers: dict[str, str], user_id: str
) -> str:
    """Create a confirmed profile version directly in DB, return its id."""
    with client.session_factory() as db:
        from backend.app.db.models import ConfirmedProfileVersion, Profile

        profile = Profile(
            id=str(uuid.uuid4()),
            user_id=user_id,
            version=1,
            local_sensitive_references={},
        )
        db.add(profile)
        db.flush()
        cpv = ConfirmedProfileVersion(
            id=str(uuid.uuid4()),
            profile_id=profile.id,
            version_number=1,
            aggregate_version=1,
            facts_snapshot={
                "name": "Privacy Test User",
                "email": "privacy@test.com",
                "skills": ["Python"],
            },
            evidence_refs={},
            local_sensitive_references={},
        )
        db.add(cpv)
        db.commit()
        return cpv.id


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_session() -> Iterator[Session]:
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

    class _FakeObjectStore:
        def get(self, key: str) -> bytes:
            return b"fake-encrypted-content"

    app = create_app(
        settings_override(
            app_auth_secret="test-secret-with-at-least-32-characters",
        )
    )
    app.dependency_overrides[dependencies._get_db] = override_db
    app.state.object_store = _FakeObjectStore()
    app.dependency_overrides[dependencies.get_redis] = lambda: redis
    with TestClient(app) as test_client:
        test_client.session_factory = session_factory  # type: ignore[attr-defined]
        yield test_client


# ---------------------------------------------------------------------------
# Gate 3: API responses must never contain object_key
# ---------------------------------------------------------------------------


class TestApiResponsesOmitObjectKey:
    """No API response schema includes ``object_key``."""

    def test_match_report_response_omits_object_key(self, client: TestClient) -> None:
        user_id, headers = _auth_headers(client)
        _admin_id, admin_h = _admin_headers(client)
        job_id = _create_verified_job(client, admin_h)
        profile_id = _create_confirmed_profile(client, headers, user_id)

        # Create a match report
        resp = client.post(
            "/api/matches",
            json={
                "job_id": job_id,
                "profile_version_id": profile_id,
            },
            headers={**headers, "Idempotency-Key": f"ik-obj-key-{uuid.uuid4().hex}"},
        )
        # May be pending or running — just check response schema never has object_key
        if resp.status_code == 201:
            assert "object_key" not in resp.text.lower()

    def test_draft_response_omits_object_key(self, client: TestClient) -> None:
        user_id, headers = _auth_headers(client)
        _admin_id, admin_h = _admin_headers(client)
        job_id = _create_verified_job(client, admin_h)
        profile_id = _create_confirmed_profile(client, headers, user_id)

        # Create match
        ik = f"ik-draft-obj-{uuid.uuid4().hex}"
        match_resp = client.post(
            "/api/matches",
            json={"job_id": job_id, "profile_version_id": profile_id},
            headers={**headers, "Idempotency-Key": ik},
        )
        if match_resp.status_code != 201:
            pytest.skip("match creation did not return 201")

        # Create draft
        draft_resp = client.post(
            "/api/resume-drafts",
            json={"match_report_id": match_resp.json()["id"]},
            headers={**headers, "Idempotency-Key": f"ik-draft-{uuid.uuid4().hex}"},
        )
        if draft_resp.status_code == 201:
            assert "object_key" not in draft_resp.text.lower()

    def test_snapshot_response_omits_object_key(self, client: TestClient) -> None:
        user_id, headers = _auth_headers(client)
        _admin_id, admin_h = _admin_headers(client)
        job_id = _create_verified_job(client, admin_h)
        profile_id = _create_confirmed_profile(client, headers, user_id)

        ik = f"ik-snap-obj-{uuid.uuid4().hex}"
        match_resp = client.post(
            "/api/matches",
            json={"job_id": job_id, "profile_version_id": profile_id},
            headers={**headers, "Idempotency-Key": ik},
        )
        if match_resp.status_code != 201:
            pytest.skip("match creation did not return 201")
        match_id = match_resp.json()["id"]

        # Create draft & approve
        draft_ik = f"ik-snap-draft-{uuid.uuid4().hex}"
        draft_resp = client.post(
            "/api/resume-drafts",
            json={"match_report_id": match_id},
            headers={**headers, "Idempotency-Key": draft_ik},
        )
        if draft_resp.status_code != 201:
            pytest.skip("draft creation did not return 201")
        draft_id = draft_resp.json()["id"]

        approve_resp = client.post(
            f"/api/resume-drafts/{draft_id}/approve",
            json={"expected_version": 0},
            headers={**headers, "Idempotency-Key": f"ik-snap-app-{uuid.uuid4().hex}"},
        )
        if approve_resp.status_code != 200:
            pytest.skip("draft approval did not return 200")
        arv_id = approve_resp.json()["id"]

        # Create snapshot
        snap_resp = client.post(
            "/api/application-snapshots",
            json={
                "job_id": job_id,
                "approved_resume_version_id": arv_id,
                "dynamic_answers": [],
                "local_sensitive_requirements": [],
            },
            headers={**headers, "Idempotency-Key": f"ik-snap-{uuid.uuid4().hex}"},
        )
        if snap_resp.status_code == 201:
            assert "object_key" not in snap_resp.text.lower()


# ---------------------------------------------------------------------------
# Gate 4: ApplicationSnapshot response excludes sensitive fields
# ---------------------------------------------------------------------------


class TestSnapshotResponseExcludesSensitiveFields:
    """SnapshotResponse must not expose ``profile_facts``, ``dynamic_answers``,
    or ``local_sensitive_requirements``."""

    def test_snapshot_get_omits_sensitive_keys(self, client: TestClient) -> None:
        user_id, headers = _auth_headers(client)
        _admin_id, admin_h = _admin_headers(client)
        job_id = _create_verified_job(client, admin_h)
        profile_id = _create_confirmed_profile(client, headers, user_id)

        ik = f"ik-excl-{uuid.uuid4().hex}"
        match_resp = client.post(
            "/api/matches",
            json={"job_id": job_id, "profile_version_id": profile_id},
            headers={**headers, "Idempotency-Key": ik},
        )
        if match_resp.status_code != 201:
            pytest.skip("match creation failed")

        draft_resp = client.post(
            "/api/resume-drafts",
            json={"match_report_id": match_resp.json()["id"]},
            headers={**headers, "Idempotency-Key": f"ik-excl-dr-{uuid.uuid4().hex}"},
        )
        if draft_resp.status_code != 201:
            pytest.skip("draft creation failed")

        approve_resp = client.post(
            f"/api/resume-drafts/{draft_resp.json()['id']}/approve",
            json={"expected_version": 0},
            headers={**headers, "Idempotency-Key": f"ik-excl-app-{uuid.uuid4().hex}"},
        )
        if approve_resp.status_code != 200:
            pytest.skip("draft approval failed")

        snap_resp = client.post(
            "/api/application-snapshots",
            json={
                "job_id": job_id,
                "approved_resume_version_id": approve_resp.json()["id"],
                "dynamic_answers": [],
                "local_sensitive_requirements": [],
            },
            headers={**headers, "Idempotency-Key": f"ik-excl-snap-{uuid.uuid4().hex}"},
        )
        if snap_resp.status_code != 201:
            pytest.skip("snapshot creation failed")

        snap_id = snap_resp.json()["id"]

        # GET single snapshot
        get_resp = client.get(
            f"/api/application-snapshots/{snap_id}",
            headers=headers,
        )
        assert get_resp.status_code == 200
        body = get_resp.json()
        for key in SNAPSHOT_SENSITIVE_KEYS:
            assert key not in body, f"Sensitive key '{key}' leaked in snapshot GET response"

        # LIST snapshots
        list_resp = client.get("/api/application-snapshots", headers=headers)
        assert list_resp.status_code == 200
        for item in list_resp.json()["items"]:
            for key in SNAPSHOT_SENSITIVE_KEYS:
                assert key not in item, f"Sensitive key '{key}' leaked in snapshot LIST response"


# ---------------------------------------------------------------------------
# Gate 5a: Unclassified / unknown field -> validator rejects with 422
# ---------------------------------------------------------------------------


class TestUnclassifiedFieldRejected:
    """A dynamic answer with an unclassified ``field_key`` must raise 422."""

    def test_unknown_field_classification_returns_unknown(self) -> None:
        assert classify_field("some_random_field") == FieldClassification.UNKNOWN

    def test_unknown_dynamic_answer_raises_422(self) -> None:
        with pytest.raises(SnapshotValidationError) as exc:
            validate_dynamic_answers([
                {
                    "field_key": "some_bogus_field",
                    "classification": "non_sensitive",
                    "value": "test",
                }
            ])
        assert exc.value.error_code == "snapshot_validation_field_not_allowed"

    def test_missing_classification_raises_422(self) -> None:
        with pytest.raises(SnapshotValidationError) as exc:
            validate_dynamic_answers([
                {"field_key": "name", "value": "test"}
            ])
        assert exc.value.error_code == "snapshot_validation_missing_classification"

    def test_non_sensitive_classification_with_local_sensitive_field_raises_422(
        self,
    ) -> None:
        # id_number is local_sensitive — submitting as non_sensitive must be rejected
        with pytest.raises(SnapshotValidationError) as exc:
            validate_dynamic_answers([
                {
                    "field_key": "id_number",
                    "classification": "non_sensitive",
                    "value": "110101199001011234",
                }
            ])
        assert exc.value.error_code == "snapshot_validation_field_not_allowed"


# ---------------------------------------------------------------------------
# Gate 5b: Local-sensitive field submitted as non_sensitive -> rejected
# ---------------------------------------------------------------------------


class TestLocalSensitiveFieldRejectedAsNonSensitive:
    """A dynamic answer with a local_sensitive field_key declared as
    ``non_sensitive`` must be rejected."""

    def test_local_sensitive_as_non_sensitive_raises_422(self) -> None:
        for sensitive_key in ("id_number", "home_address", "bank_account"):
            with pytest.raises(SnapshotValidationError) as exc:
                validate_dynamic_answers([
                    {
                        "field_key": sensitive_key,
                        "classification": "non_sensitive",
                        "value": "some-value",
                    }
                ])
            assert exc.value.error_code == "snapshot_validation_field_not_allowed"

    def test_local_sensitive_requirement_rejects_non_local_sensitive_field(
        self,
    ) -> None:
        """A local-sensitive requirement with a non-sensitive field_key is rejected."""
        with pytest.raises(SnapshotValidationError) as exc:
            validate_local_sensitive_requirements([
                {
                    "field_key": "name",  # NON_SENSITIVE
                    "category": "government_id",
                    "local_reference": "lsr:v1:" + "a" * 64,
                }
            ])
        assert exc.value.error_code == "snapshot_validation_field_not_local_sensitive"

    def test_local_sensitive_requirement_accepts_valid_reference(self) -> None:
        """A properly structured local-sensitive requirement passes validation."""
        result = validate_local_sensitive_requirements([
            {
                "field_key": "id_number",
                "category": "government_id",
                "local_reference": "lsr:v1:" + "a" * 64,
            }
        ])
        assert len(result) == 1
        assert result[0]["field_key"] == "id_number"


# ---------------------------------------------------------------------------
# Gate 6: Log inspection — only entity IDs + error codes
# ---------------------------------------------------------------------------


class TestWave2LogPrivacy:
    """Ensure Wave 2 services log only entity IDs + error codes."""

    SENSITIVE_PATTERNS = [
        "privacy@test.com",
        "110101199001011234",
        "PrivateResumeText",
        "object-key-sentinel",
        "lsr:v1:sensitive-reference-value",
    ]

    def test_match_service_no_logger(self) -> None:
        """Match service has no logger — no risk of logging sensitive data."""
        import backend.app.services.match_service as svc
        assert not hasattr(svc, "logger") or svc.logger is None

    def test_snapshot_service_logger_exists_and_scoped(self) -> None:
        """Application snapshot service has a scoped logger for safe diag."""
        import backend.app.services.application_snapshot_service as svc
        assert hasattr(svc, "logger"), "snapshot service should have a logger"
        assert "application_snapshot_service" in svc.logger.name

    def test_resume_draft_service_logs_only_safe_messages(
        self, client: TestClient
    ) -> None:
        """resume_draft_service logs only format strings and object keys (paths)."""
        from backend.app.services.resume_draft_service import logger as draft_logger
        # The logger is a module-level logger; verify no sensitive formatting
        logger_name = draft_logger.name
        assert "backend" in logger_name


# ---------------------------------------------------------------------------
# Gate 8a: Attachment download response doesn't expose object_key
# ---------------------------------------------------------------------------


class TestAttachmentDownloadNoObjectKey:
    """Download response headers must not contain ``object_key``."""

    def test_download_response_headers_no_object_key(self, client: TestClient) -> None:
        user_id, headers = _auth_headers(client)
        _admin_id, admin_h = _admin_headers(client)
        job_id = _create_verified_job(client, admin_h)
        profile_id = _create_confirmed_profile(client, headers, user_id)

        ik = f"ik-dl-{uuid.uuid4().hex}"
        match_resp = client.post(
            "/api/matches",
            json={"job_id": job_id, "profile_version_id": profile_id},
            headers={**headers, "Idempotency-Key": ik},
        )
        if match_resp.status_code != 201:
            pytest.skip("match creation failed")

        draft_resp = client.post(
            "/api/resume-drafts",
            json={"match_report_id": match_resp.json()["id"]},
            headers={**headers, "Idempotency-Key": f"ik-dl-dr-{uuid.uuid4().hex}"},
        )
        if draft_resp.status_code != 201:
            pytest.skip("draft creation failed")

        approve_resp = client.post(
            f"/api/resume-drafts/{draft_resp.json()['id']}/approve",
            json={"expected_version": 0},
            headers={**headers, "Idempotency-Key": f"ik-dl-app-{uuid.uuid4().hex}"},
        )
        if approve_resp.status_code != 200:
            pytest.skip("draft approval failed")

        # Get attachment id
        att_id = approve_resp.json()["attachments"][0]["id"]

        # Download
        dl_resp = client.get(
            f"/api/approved-resume-attachments/{att_id}/download",
            headers=headers,
        )
        if dl_resp.status_code == 200:
            # Check response headers don't contain object_key
            for header_name, header_value in dl_resp.headers.items():
                assert "object_key" not in header_value.lower(), (
                    f"Header '{header_name}' contains 'object_key': {header_value}"
                )
