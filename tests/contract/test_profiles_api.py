from __future__ import annotations

from collections.abc import Iterator
import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.api import dependencies
from backend.app.config import Settings
from backend.app.db.base import Base

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("APP_AUTH_SECRET", "test-secret-with-at-least-32-characters")
os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")
os.environ.setdefault(
    "OBJECT_ENCRYPTION_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
)

from backend.app.main import create_app
from backend.app.services.storage import EncryptedObjectStore
from tests.unit.test_encrypted_storage import MemoryBlobStore


ASSET_FIELDS = {
    "id",
    "original_filename",
    "content_type",
    "plaintext_size",
    "encryption_version",
    "status",
    "error_code",
    "created_at",
    "updated_at",
}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
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

    memory_blob_store = MemoryBlobStore()
    encryption_key = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
    object_store = EncryptedObjectStore(memory_blob_store, encryption_key)

    def override_db() -> Iterator[Session]:
        with session_factory() as db:
            yield db

    monkeypatch.setattr(dependencies, "get_settings", lambda: settings)
    app = create_app(settings)
    app.state.object_store = object_store
    app.state.session_factory = session_factory
    app.dependency_overrides[dependencies._get_db] = override_db
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def student_headers(client: TestClient) -> dict[str, str]:
    body = client.post(
        "/api/auth/register",
        json={"account": "student1", "nickname": "Student1", "password": "secret12"},
    ).json()
    return {"Authorization": f"Bearer {body['token']}"}


def test_upload_lists_only_safe_metadata(
    client: TestClient, student_headers: dict[str, str]
) -> None:
    response = client.post(
        "/api/resume-assets",
        headers=student_headers,
        files={"file": ("resume.txt", b"Zhang San\nSkills\nPython", "text/plain")},
    )
    assert response.status_code == 201
    assert set(response.json()) == ASSET_FIELDS
    serialized = response.text.lower()
    assert "object_key" not in serialized
    assert "plaintext_sha256" not in serialized
    assert "zhang san" not in serialized
    listed = client.get("/api/resume-assets", headers=student_headers)
    assert listed.status_code == 200
    assert set(listed.json()["assets"][0]) == ASSET_FIELDS


def test_upload_import_evidence_and_version_lifecycle(
    client: TestClient, student_headers: dict[str, str]
) -> None:
    # Upload
    upload = client.post(
        "/api/resume-assets",
        headers=student_headers,
        files={"file": ("resume.txt", b"Zhang San\nSkills\nPython", "text/plain")},
    )
    assert upload.status_code == 201
    asset_id = upload.json()["id"]

    # Reconcile to ready
    reconcile = client.post(
        f"/api/resume-assets/{asset_id}/reconcile",
        headers=student_headers,
    )
    assert reconcile.status_code == 200
    assert reconcile.json()["status"] == "ready"

    # Create import
    import_resp = client.post(
        "/api/resume-imports",
        headers=student_headers,
        json={"asset_id": asset_id},
    )
    assert import_resp.status_code == 201
    import_id = import_resp.json()["id"]
    assert import_resp.json()["status"] == "awaiting_confirmation"

    # Read profile with evidence
    profile = client.get("/api/profiles", headers=student_headers)
    assert profile.status_code == 200
    assert "version" in profile.json()
    assert "evidence" in profile.json()

    # Apply decisions
    evidence = profile.json()["evidence"]
    assert evidence
    decisions = [
        {"evidence_id": item["id"], "action": "confirm"}
        for item in evidence
    ]
    patch = client.patch(
        "/api/profiles/evidence",
        headers=student_headers,
        json={"expected_version": profile.json()["version"], "decisions": decisions},
    )
    assert patch.status_code == 200
    new_version = patch.json()["version"]

    reviewed_profile = client.get("/api/profiles", headers=student_headers)
    assert len(reviewed_profile.json()["evidence"]) == len(evidence)
    assert {item["status"] for item in reviewed_profile.json()["evidence"]} == {
        "confirmed"
    }

    # Create confirmed version
    version_resp = client.post(
        "/api/profile-versions",
        headers=student_headers,
        json={"expected_version": new_version, "resume_import_id": import_id},
    )
    assert version_resp.status_code == 201
    assert version_resp.json()["version_number"] == 1

    # List versions
    versions = client.get("/api/profile-versions", headers=student_headers)
    assert versions.status_code == 200
    assert len(versions.json()["versions"]) >= 1

    # Download
    download = client.get(
        f"/api/resume-assets/{asset_id}/download", headers=student_headers
    )
    assert download.status_code == 200
    assert download.content == b"Zhang San\nSkills\nPython"

    # Cross-user 404
    other_headers = client.post(
        "/api/auth/register",
        json={"account": "other", "nickname": "Other", "password": "secret12"},
    ).json()
    other_auth = {"Authorization": f"Bearer {other_headers['token']}"}
    assert (
        client.get(
            f"/api/resume-assets/{asset_id}", headers=other_auth
        ).status_code
        == 404
    )


def test_profile_diff_response_tolerates_missing_diff_entry(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    student_headers: dict[str, str],
) -> None:
    upload = client.post(
        "/api/resume-assets",
        headers=student_headers,
        files={"file": ("resume.txt", b"Skills\nPython", "text/plain")},
    )
    client.post(
        "/api/resume-imports",
        headers=student_headers,
        json={"asset_id": upload.json()["id"]},
    )
    from backend.app.api.routes import profiles as profile_routes

    monkeypatch.setattr(profile_routes.profile_repository, "compute_evidence_diff", lambda *_: {})
    response = client.get("/api/profiles", headers=student_headers)

    assert response.status_code == 200
    assert all(item["diff_action"] is None for item in response.json()["evidence"])


def test_download_uses_sanitized_rfc5987_filename(
    client: TestClient, student_headers: dict[str, str]
) -> None:
    upload = client.post(
        "/api/resume-assets",
        headers=student_headers,
        files={"file": ('résumé "final".txt', b"Skills\nPython", "text/plain")},
    )

    response = client.get(
        f"/api/resume-assets/{upload.json()['id']}/download",
        headers=student_headers,
    )

    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment; filename*=UTF-8''")
    disposition.encode("ascii")
    assert '"' not in disposition
    assert "\r" not in disposition and "\n" not in disposition
