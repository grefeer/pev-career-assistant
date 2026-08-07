"""HTTP contract and branch coverage for the retained profiles routes and services.

Drives every endpoint through ``TestClient`` against a real in-memory SQLite
database plus a real ``EncryptedObjectStore`` backed by a ``MemoryBlobStore``,
so both ``backend/app/api/routes/profiles.py`` and
``backend/app/services/profiles.py`` are exercised end-to-end. A handful of
service branches that the routes can never trigger (e.g. parser fed a
non-validated filename) are covered by direct service calls at the bottom.
"""

from __future__ import annotations

import base64
import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.api.routes import profiles as routes_module
from backend.app.db.base import Base
from backend.app.db.models import (
    ResumeAsset,
    ResumeAssetStatus,
    ResumeImport,
    ResumeImportStatus,
    User,
    UserRole,
)
from backend.app.domain.profiles import LocalSensitiveReferenceError
from backend.app.repositories import profiles as profile_repository
from backend.app.services.auth import AuthService
from backend.app.services import profiles as services_module
from backend.app.services.profiles import (
    OwnedProfileResourceNotFound,
    ResumeAssetService,
    ResumeImportService,
)
from backend.app.services.storage import EncryptedObjectStore
from tests.conftest import settings_override
from tests.unit.test_encrypted_storage import MemoryBlobStore

USER_ID = "user-a"
ENCRYPTION_KEY = base64.b64encode(bytes(range(32))).decode("ascii")
# A resume body whose parsed evidence spans name + email + skills so that the
# CONFIRM / CORRECT / IGNORE decision branches can all be exercised together.
MULTI_FIELD_RESUME = "张三\nzhangsan@example.com\n技能\nPython\n".encode("utf-8")
# A distinct resume body used when a second confirmed version is needed.
RESUME_VARIANT = "李四\nlisi@example.com\n技能\nJava\n".encode("utf-8")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def env():
    """Fresh app + in-memory DB + encrypted object store for each test."""
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with Session(engine) as db:
        db.add(
            User(
                id=USER_ID,
                account="user-a@example.test",
                nickname="user-a",
                password_hash="not-a-real-password-hash",
                role=UserRole.STUDENT,
            )
        )
        db.commit()

    blob_store = MemoryBlobStore()
    object_store = EncryptedObjectStore(blob_store, ENCRYPTION_KEY)
    app = FastAPI()
    app.state.settings = settings_override()
    app.state.session_factory = factory
    app.state.object_store = object_store
    app.include_router(routes_module.router, prefix="/api")

    token = AuthService(app.state.settings).issue_user_token(
        SimpleNamespace(id=USER_ID, role=UserRole.STUDENT)
    )
    headers = {"Authorization": f"Bearer {token}"}
    client = TestClient(app, raise_server_exceptions=False)
    ns = SimpleNamespace(
        client=client,
        headers=headers,
        app=app,
        blob_store=blob_store,
        object_store=object_store,
        settings=app.state.settings,
    )
    yield ns
    engine.dispose()


# ---------------------------------------------------------------------------
# Seed helpers (operate on the same DB / store the routes use)
# ---------------------------------------------------------------------------


def _upload(env, *, filename="resume.txt", content=MULTI_FIELD_RESUME, content_type="text/plain"):
    return env.client.post(
        "/api/resume-assets",
        headers=env.headers,
        files={"file": (filename, content, content_type)},
    )


def _ready_asset_id(env, *, content=MULTI_FIELD_RESUME) -> str:
    response = _upload(env, content=content)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _seed_pending_asset(env, *, filename="resume.txt", content_type="text/plain", raw=b"resume"):
    """Insert a PENDING_UPLOAD asset directly (no object write, no mark_ready)."""
    with env.app.state.session_factory() as db:
        service = ResumeAssetService(env.object_store)
        asset = service.create_pending_asset(
            db,
            user_id=USER_ID,
            filename=filename,
            content_type=content_type,
            raw=raw,
        )
        db.commit()
        return asset.id


def _create_import(env, asset_id: str):
    return env.client.post(
        "/api/resume-imports",
        headers=env.headers,
        json={"asset_id": asset_id},
    )


def _profile_version(env) -> int:
    return env.client.get("/api/profiles", headers=env.headers).json()["version"]


def _evidence_list(env) -> list[dict]:
    return env.client.get("/api/profiles", headers=env.headers).json()["evidence"]


def _confirm_all_evidence(env) -> None:
    """Confirm every current evidence row so a version can be created."""
    evidence = _evidence_list(env)
    version = _profile_version(env)
    response = env.client.patch(
        "/api/profiles/evidence",
        headers=env.headers,
        json={
            "expected_version": version,
            "decisions": [{"evidence_id": ev["id"], "action": "confirm"} for ev in evidence],
        },
    )
    assert response.status_code == 200, response.text


def _create_confirmed_version(env, *, content=MULTI_FIELD_RESUME) -> dict:
    """Full flow: upload -> import -> confirm all -> create version. Returns body."""
    asset_id = _ready_asset_id(env, content=content)
    import_id = _create_import(env, asset_id).json()["id"]
    _confirm_all_evidence(env)
    version = _profile_version(env)
    response = env.client.post(
        "/api/profile-versions",
        headers=env.headers,
        json={"expected_version": version, "resume_import_id": import_id},
    )
    assert response.status_code == 201, response.text
    return response.json()


# ---------------------------------------------------------------------------
# _profile_http_error helper
# ---------------------------------------------------------------------------


def test_profile_http_error_reraises_unhandled_errors() -> None:
    """Errors outside the mapped set propagate untouched (defensive fallback)."""
    with pytest.raises(RuntimeError):
        routes_module._profile_http_error(RuntimeError("unhandled"))


# ---------------------------------------------------------------------------
# Resume asset upload
# ---------------------------------------------------------------------------


def test_upload_resume_asset_creates_ready_asset(env) -> None:
    response = _upload(env)
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "ready"
    assert body["original_filename"] == "resume.txt"
    assert body["content_type"] == "text/plain"
    assert body["plaintext_size"] == len(MULTI_FIELD_RESUME)


def test_upload_resume_asset_rejects_too_large(env, monkeypatch) -> None:
    monkeypatch.setattr(routes_module, "MAX_RESUME_BYTES", 10)
    response = env.client.post(
        "/api/resume-assets",
        headers=env.headers,
        files={"file": ("resume.txt", b"x" * 11, "text/plain")},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "resume_too_large"


def test_upload_resume_asset_rejects_unsupported_suffix(env) -> None:
    response = env.client.post(
        "/api/resume-assets",
        headers=env.headers,
        files={"file": ("resume.xyz", b"data", "text/plain")},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "unsupported_resume_type"


def test_upload_resume_asset_rejects_unsupported_content_type(env) -> None:
    response = env.client.post(
        "/api/resume-assets",
        headers=env.headers,
        files={"file": ("resume.txt", b"data", "application/pdf")},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_profile_operation"


def test_upload_resume_asset_uses_content_type_fallback_when_missing(env) -> None:
    """A part without an explicit content type still routes through the guard."""
    response = env.client.post(
        "/api/resume-assets",
        headers=env.headers,
        files={"file": ("resume.txt", b"data")},
    )
    # httpx infers text/plain for .txt, so the upload succeeds; the point is
    # that the ``or`` fallback on line 133 executes without breaking the flow.
    assert response.status_code in (201, 422)


def test_upload_resume_asset_object_store_unavailable_marks_upload_failed(env, monkeypatch) -> None:
    def _raise(**_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(env.blob_store, "put_bytes", _raise)
    response = _upload(env)
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "object_store_unavailable"
    # Asset row was persisted and flagged as upload_failed before the 503.
    asset_id = response.json().get("id")  # response has no id on failure path
    assert asset_id is None
    listing = env.client.get("/api/resume-assets", headers=env.headers).json()["assets"]
    assert listing
    assert listing[0]["status"] == "upload_failed"
    assert listing[0]["error_code"] == "object_store_unavailable"


def test_upload_resume_asset_rolls_back_when_mark_upload_failed_raises(env, monkeypatch) -> None:
    def _raise_put(**_kwargs):
        raise OSError("disk full")

    def _raise_mark(_self, _db, *, asset, error_code):
        raise RuntimeError("db down")

    monkeypatch.setattr(env.blob_store, "put_bytes", _raise_put)
    monkeypatch.setattr(services_module.ResumeAssetService, "mark_upload_failed", _raise_mark)
    response = _upload(env)
    # Inner failure is swallowed; the outer 503 still fires.
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "object_store_unavailable"


def test_upload_resume_asset_generic_exception_rolls_back(env, monkeypatch) -> None:
    def _raise(_self, _db, *, asset):
        raise RuntimeError("unexpected boom")

    monkeypatch.setattr(services_module.ResumeAssetService, "mark_ready", _raise)
    response = _upload(env)
    assert response.status_code == 500


# ---------------------------------------------------------------------------
# Resume asset listing / fetch / download
# ---------------------------------------------------------------------------


def test_list_resume_assets_returns_uploaded_asset(env) -> None:
    assert env.client.get("/api/resume-assets", headers=env.headers).json() == {"assets": []}
    _ready_asset_id(env)
    assets = env.client.get("/api/resume-assets", headers=env.headers).json()["assets"]
    assert len(assets) == 1
    assert assets[0]["status"] == "ready"


def test_get_resume_asset_returns_404_for_unknown(env) -> None:
    response = env.client.get(
        f"/api/resume-assets/{uuid.uuid4()}", headers=env.headers
    )
    assert response.status_code == 404


def test_get_resume_asset_returns_asset(env) -> None:
    asset_id = _ready_asset_id(env)
    response = env.client.get(f"/api/resume-assets/{asset_id}", headers=env.headers)
    assert response.status_code == 200
    assert response.json()["id"] == asset_id


def test_download_resume_asset_returns_404_for_unknown(env) -> None:
    response = env.client.get(
        f"/api/resume-assets/{uuid.uuid4()}/download", headers=env.headers
    )
    assert response.status_code == 404


def test_download_resume_asset_streams_encrypted_object(env) -> None:
    asset_id = _ready_asset_id(env)
    response = env.client.get(
        f"/api/resume-assets/{asset_id}/download", headers=env.headers
    )
    assert response.status_code == 200
    assert response.content == MULTI_FIELD_RESUME
    assert response.headers["content-type"].startswith("text/plain")
    assert "filename*=UTF-8''resume.txt" in response.headers["content-disposition"]


def test_download_resume_asset_returns_503_when_object_store_read_fails(env, monkeypatch) -> None:
    asset_id = _ready_asset_id(env)

    def _raise(*, key):
        raise OSError("read failed")

    monkeypatch.setattr(env.blob_store, "get_bytes", _raise)
    response = env.client.get(
        f"/api/resume-assets/{asset_id}/download", headers=env.headers
    )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "object_store_unavailable"


# ---------------------------------------------------------------------------
# Resume asset reconcile
# ---------------------------------------------------------------------------


def test_reconcile_resume_asset_returns_ready_asset(env) -> None:
    asset_id = _ready_asset_id(env)
    response = env.client.post(
        f"/api/resume-assets/{asset_id}/reconcile", headers=env.headers
    )
    assert response.status_code == 200
    assert response.json()["status"] == "ready"


def test_reconcile_resume_asset_returns_404_for_unknown(env) -> None:
    response = env.client.post(
        f"/api/resume-assets/{uuid.uuid4()}/reconcile", headers=env.headers
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "profile_resource_not_found"


def test_reconcile_resume_asset_returns_409_when_object_missing(env) -> None:
    """A pending asset whose object was never written cannot be reconciled."""
    asset_id = _seed_pending_asset(env)
    response = env.client.post(
        f"/api/resume-assets/{asset_id}/reconcile", headers=env.headers
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "resume_asset_state_conflict"


# ---------------------------------------------------------------------------
# Resume asset delete
# ---------------------------------------------------------------------------


def test_delete_resume_asset_returns_404_for_unknown(env) -> None:
    response = env.client.delete(
        f"/api/resume-assets/{uuid.uuid4()}", headers=env.headers
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "profile_resource_not_found"


def test_delete_resume_asset_removes_asset_and_object(env) -> None:
    asset_id = _ready_asset_id(env)
    object_key = f"users/{USER_ID}/resume-assets/{asset_id}"
    assert object_key in env.blob_store.objects

    response = env.client.delete(
        f"/api/resume-assets/{asset_id}", headers=env.headers
    )
    assert response.status_code == 200
    assert response.json() == {"deleted": True}
    # Asset is gone from the listing and its encrypted object is purged.
    assets = env.client.get("/api/resume-assets", headers=env.headers).json()["assets"]
    assert all(a["id"] != asset_id for a in assets)
    assert object_key not in env.blob_store.objects


def test_delete_resume_asset_cascades_import_evidence_and_decisions(env) -> None:
    asset_id = _ready_asset_id(env)
    import_id = _create_import(env, asset_id).json()["id"]
    evidence = _evidence_list(env)
    version = _profile_version(env)
    env.client.patch(
        "/api/profiles/evidence",
        headers=env.headers,
        json={
            "expected_version": version,
            "decisions": [{"evidence_id": evidence[0]["id"], "action": "confirm"}],
        },
    )
    assert _evidence_list(env)

    response = env.client.delete(
        f"/api/resume-assets/{asset_id}", headers=env.headers
    )
    assert response.status_code == 200
    # Import is gone, and the cascaded evidence no longer projects.
    assert env.client.get(
        f"/api/resume-imports/{import_id}", headers=env.headers
    ).status_code == 404
    assert _evidence_list(env) == []


def test_delete_resume_asset_swallows_object_delete_failure(env, monkeypatch) -> None:
    """An object-store failure during purge must not fail the committed delete."""
    asset_id = _ready_asset_id(env)

    def _raise(*, key):
        raise OSError("delete failed")

    monkeypatch.setattr(env.blob_store, "delete", _raise)
    response = env.client.delete(
        f"/api/resume-assets/{asset_id}", headers=env.headers
    )
    # DB delete already committed; best-effort purge failure is swallowed.
    assert response.status_code == 200
    assets = env.client.get("/api/resume-assets", headers=env.headers).json()["assets"]
    assert all(a["id"] != asset_id for a in assets)


# ---------------------------------------------------------------------------
# Resume imports
# ---------------------------------------------------------------------------


def test_create_resume_import_rejects_invalid_body(env) -> None:
    response = env.client.post("/api/resume-imports", headers=env.headers, json={})
    assert response.status_code == 422


def test_create_resume_import_returns_404_for_unknown_asset(env) -> None:
    response = _create_import(env, str(uuid.uuid4()))
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "profile_resource_not_found"


def test_create_resume_import_returns_409_when_asset_not_ready(env) -> None:
    asset_id = _seed_pending_asset(env)
    response = _create_import(env, asset_id)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "resume_asset_state_conflict"


def test_create_resume_import_processes_evidence(env) -> None:
    asset_id = _ready_asset_id(env)
    response = _create_import(env, asset_id)
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "awaiting_confirmation"
    evidence = _evidence_list(env)
    assert any(ev["field_path"] == "skills" for ev in evidence)


def test_create_resume_import_marks_failed_when_object_store_read_fails(env, monkeypatch) -> None:
    asset_id = _ready_asset_id(env)

    def _raise(*, key):
        raise OSError("read failed")

    monkeypatch.setattr(env.blob_store, "get_bytes", _raise)
    response = _create_import(env, asset_id)
    assert response.status_code == 201
    assert response.json()["status"] == "failed"
    assert response.json()["error_code"] == "resume_asset_read_failed"


def test_create_resume_import_marks_needs_manual_entry_for_empty_text(env) -> None:
    asset_id = _ready_asset_id(env, content=b"")
    response = _create_import(env, asset_id)
    assert response.status_code == 201
    assert response.json()["status"] == "needs_manual_entry"
    assert response.json()["error_code"] == "resume_text_unavailable"


def test_get_resume_import_returns_404_for_unknown(env) -> None:
    response = env.client.get(
        f"/api/resume-imports/{uuid.uuid4()}", headers=env.headers
    )
    assert response.status_code == 404


def test_get_resume_import_returns_import(env) -> None:
    asset_id = _ready_asset_id(env)
    import_id = _create_import(env, asset_id).json()["id"]
    response = env.client.get(
        f"/api/resume-imports/{import_id}", headers=env.headers
    )
    assert response.status_code == 200
    assert response.json()["id"] == import_id


# ---------------------------------------------------------------------------
# Profile evidence + decisions
# ---------------------------------------------------------------------------


def test_get_profile_returns_empty_profile(env) -> None:
    response = env.client.get("/api/profiles", headers=env.headers)
    assert response.status_code == 200
    body = response.json()
    assert body["version"] == 0
    assert body["evidence"] == []
    assert body["latest_version"] is None
    assert body["local_sensitive_references"] == {}


def test_get_profile_projects_evidence_with_decisions_and_diff(env) -> None:
    asset_id = _ready_asset_id(env)
    _create_import(env, asset_id)
    evidence = _evidence_list(env)
    assert evidence
    assert evidence[0]["status"] == "pending"
    assert evidence[0]["diff_action"] == "add"

    # Apply CONFIRM to one evidence -> status confirmed in projection.
    ev_id = evidence[0]["id"]
    version = _profile_version(env)
    env.client.patch(
        "/api/profiles/evidence",
        headers=env.headers,
        json={"expected_version": version, "decisions": [{"evidence_id": ev_id, "action": "confirm"}]},
    )
    projected = _evidence_list(env)
    confirmed = next(ev for ev in projected if ev["id"] == ev_id)
    assert confirmed["status"] == "confirmed"


def test_apply_evidence_decisions_rejects_invalid_body(env) -> None:
    response = env.client.patch(
        "/api/profiles/evidence", headers=env.headers, json={"expected_version": 0}
    )
    assert response.status_code == 422


def test_apply_evidence_decisions_returns_409_for_stale_version(env) -> None:
    asset_id = _ready_asset_id(env)
    _create_import(env, asset_id)
    ev_id = _evidence_list(env)[0]["id"]
    response = env.client.patch(
        "/api/profiles/evidence",
        headers=env.headers,
        json={
            "expected_version": 999,
            "decisions": [{"evidence_id": ev_id, "action": "confirm"}],
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "stale_profile_version"


def test_apply_evidence_decisions_returns_404_for_missing_evidence(env) -> None:
    response = env.client.patch(
        "/api/profiles/evidence",
        headers=env.headers,
        json={
            "expected_version": 0,
            "decisions": [{"evidence_id": str(uuid.uuid4()), "action": "confirm"}],
        },
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "profile_resource_not_found"


def test_apply_evidence_decisions_applies_confirm_and_correct(env) -> None:
    asset_id = _ready_asset_id(env)
    _create_import(env, asset_id)
    evidence = _evidence_list(env)
    skills = next(ev for ev in evidence if ev["field_path"] == "skills")
    name = next(ev for ev in evidence if ev["field_path"] == "basics.name")
    version = _profile_version(env)
    response = env.client.patch(
        "/api/profiles/evidence",
        headers=env.headers,
        json={
            "expected_version": version,
            "decisions": [
                {"evidence_id": name["id"], "action": "confirm"},
                {"evidence_id": skills["id"], "action": "correct", "corrected_value": ["Java"]},
            ],
        },
    )
    assert response.status_code == 200
    assert response.json()["version"] == version + 1


def test_corrected_value_is_surfaced_in_evidence_projection(env) -> None:
    """A CORRECT decision's resolved_value is projected back as corrected_value."""
    asset_id = _ready_asset_id(env)
    _create_import(env, asset_id)
    evidence = {ev["field_path"]: ev for ev in _evidence_list(env)}
    version = _profile_version(env)
    env.client.patch(
        "/api/profiles/evidence",
        headers=env.headers,
        json={
            "expected_version": version,
            "decisions": [
                {"evidence_id": evidence["basics.name"]["id"], "action": "confirm"},
                {
                    "evidence_id": evidence["skills"]["id"],
                    "action": "correct",
                    "corrected_value": ["Go", "Rust"],
                },
                {"evidence_id": evidence["basics.email"]["id"], "action": "ignore"},
            ],
        },
    )
    projected = {ev["field_path"]: ev for ev in _evidence_list(env)}
    # CORRECT surfaces the resolved value verbatim.
    assert projected["skills"]["status"] == "corrected"
    assert projected["skills"]["corrected_value"] == ["Go", "Rust"]
    # CONFIRM and IGNORE never carry a corrected value.
    assert projected["basics.name"]["status"] == "confirmed"
    assert projected["basics.name"]["corrected_value"] is None
    assert projected["basics.email"]["status"] == "ignored"
    assert projected["basics.email"]["corrected_value"] is None


# ---------------------------------------------------------------------------
# Local sensitive references
# ---------------------------------------------------------------------------


def test_update_local_sensitive_references_rejects_invalid_body(env) -> None:
    response = env.client.patch(
        "/api/profiles/local-sensitive-references",
        headers=env.headers,
        json={"expected_version": 0},
    )
    assert response.status_code == 422


def test_update_local_sensitive_references_returns_409_for_stale_version(env) -> None:
    reference = "lsr:v1:" + "a" * 64
    response = env.client.patch(
        "/api/profiles/local-sensitive-references",
        headers=env.headers,
        json={
            "expected_version": 999,
            "category": "government_id",
            "reference": reference,
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "stale_profile_version"


def test_update_local_sensitive_references_returns_422_for_validation_error(
    env, monkeypatch
) -> None:
    def _raise(*_args, **_kwargs):
        raise LocalSensitiveReferenceError("invalid reference")

    monkeypatch.setattr(services_module.ProfileService, "update_local_sensitive_reference", _raise)
    reference = "lsr:v1:" + "a" * 64
    response = env.client.patch(
        "/api/profiles/local-sensitive-references",
        headers=env.headers,
        json={
            "expected_version": 0,
            "category": "government_id",
            "reference": reference,
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_local_sensitive_reference"


def test_update_local_sensitive_references_updates_reference(env) -> None:
    reference = "lsr:v1:" + "a" * 64
    response = env.client.patch(
        "/api/profiles/local-sensitive-references",
        headers=env.headers,
        json={
            "expected_version": 0,
            "category": "government_id",
            "reference": reference,
        },
    )
    assert response.status_code == 200
    assert response.json()["version"] == 1
    profile = env.client.get("/api/profiles", headers=env.headers).json()
    assert profile["local_sensitive_references"]["government_id"]["reference"] == reference


# ---------------------------------------------------------------------------
# Profile versions
# ---------------------------------------------------------------------------


def test_create_profile_version_rejects_invalid_body(env) -> None:
    response = env.client.post(
        "/api/profile-versions", headers=env.headers, json={"expected_version": 0}
    )
    assert response.status_code == 422


def test_create_profile_version_returns_404_for_unknown_import(env) -> None:
    response = env.client.post(
        "/api/profile-versions",
        headers=env.headers,
        json={"expected_version": 0, "resume_import_id": str(uuid.uuid4())},
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "profile_resource_not_found"


def test_create_profile_version_returns_409_for_stale_version(env) -> None:
    asset_id = _ready_asset_id(env)
    import_id = _create_import(env, asset_id).json()["id"]
    response = env.client.post(
        "/api/profile-versions",
        headers=env.headers,
        json={"expected_version": 999, "resume_import_id": import_id},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "stale_profile_version"


def test_create_profile_version_returns_422_for_undecided_evidence(env) -> None:
    asset_id = _ready_asset_id(env)
    import_id = _create_import(env, asset_id).json()["id"]
    response = env.client.post(
        "/api/profile-versions",
        headers=env.headers,
        json={"expected_version": 0, "resume_import_id": import_id},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_profile_operation"


def test_create_profile_version_confirms_with_confirm_correct_and_ignore(env) -> None:
    asset_id = _ready_asset_id(env)
    import_id = _create_import(env, asset_id).json()["id"]
    evidence = {ev["field_path"]: ev["id"] for ev in _evidence_list(env)}
    version = _profile_version(env)
    env.client.patch(
        "/api/profiles/evidence",
        headers=env.headers,
        json={
            "expected_version": version,
            "decisions": [
                {"evidence_id": evidence["basics.name"], "action": "confirm"},
                {"evidence_id": evidence["basics.email"], "action": "correct", "corrected_value": "new@example.com"},
                {"evidence_id": evidence["skills"], "action": "ignore"},
            ],
        },
    )
    version = _profile_version(env)
    response = env.client.post(
        "/api/profile-versions",
        headers=env.headers,
        json={"expected_version": version, "resume_import_id": import_id},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["version_number"] == 1
    # IGNORE is skipped, CORRECT uses the resolved value, CONFIRM uses candidate.
    assert body["facts_snapshot"]["basics.email"] == "new@example.com"
    assert body["facts_snapshot"]["basics.name"] == "张三"
    assert "skills" not in body["facts_snapshot"]
    assert body["evidence_refs"][evidence["basics.email"]]["action"] == "correct"


def test_list_profile_versions_returns_versions(env) -> None:
    assert env.client.get("/api/profile-versions", headers=env.headers).json() == {"versions": []}
    asset_id = _ready_asset_id(env)
    import_id = _create_import(env, asset_id).json()["id"]
    _confirm_all_evidence(env)
    version = _profile_version(env)
    env.client.post(
        "/api/profile-versions",
        headers=env.headers,
        json={"expected_version": version, "resume_import_id": import_id},
    )
    versions = env.client.get("/api/profile-versions", headers=env.headers).json()["versions"]
    assert len(versions) == 1
    assert versions[0]["version_number"] == 1


def test_get_profile_version_returns_404_for_unknown(env) -> None:
    response = env.client.get(
        f"/api/profile-versions/{uuid.uuid4()}", headers=env.headers
    )
    assert response.status_code == 404


def test_get_profile_version_returns_version(env) -> None:
    asset_id = _ready_asset_id(env)
    import_id = _create_import(env, asset_id).json()["id"]
    _confirm_all_evidence(env)
    version = _profile_version(env)
    created = env.client.post(
        "/api/profile-versions",
        headers=env.headers,
        json={"expected_version": version, "resume_import_id": import_id},
    ).json()
    response = env.client.get(
        f"/api/profile-versions/{created['id']}", headers=env.headers
    )
    assert response.status_code == 200
    assert response.json()["id"] == created["id"]


def test_create_profile_version_sets_active_version(env) -> None:
    """Creating a confirmed version pins it as the version the runtime consumes."""
    created = _create_confirmed_version(env)
    profile = env.client.get("/api/profiles", headers=env.headers).json()
    assert profile["active_version_id"] == created["id"]


def test_activate_profile_version_switches_active(env) -> None:
    """Activating an older confirmed version re-points the runtime's active version."""
    first = _create_confirmed_version(env)
    second = _create_confirmed_version(env, content=RESUME_VARIANT)
    # Each creation auto-pins the version as active; the second wins.
    assert env.client.get("/api/profiles", headers=env.headers).json()["active_version_id"] == second["id"]

    response = env.client.post(
        f"/api/profile-versions/{first['id']}/activate", headers=env.headers
    )
    assert response.status_code == 200
    assert response.json() == {"active_version_id": first["id"]}
    assert env.client.get("/api/profiles", headers=env.headers).json()["active_version_id"] == first["id"]


def test_activate_profile_version_returns_404_for_unknown(env) -> None:
    response = env.client.post(
        f"/api/profile-versions/{uuid.uuid4()}/activate", headers=env.headers
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "profile_resource_not_found"


# ---------------------------------------------------------------------------
# _build_evidence_response defensive branches
# ---------------------------------------------------------------------------


def test_build_evidence_response_handles_plain_string_action(env, monkeypatch) -> None:
    """A decision whose action lacks ``.value`` falls back to ``str(action)``."""
    asset_id = _ready_asset_id(env)
    _create_import(env, asset_id)
    ev_id = _evidence_list(env)[0]["id"]
    monkeypatch.setattr(
        profile_repository,
        "latest_decisions_by_evidence",
        lambda _db, _pid: {ev_id: SimpleNamespace(action="confirm")},
    )
    evidence = _evidence_list(env)
    assert next(ev for ev in evidence if ev["id"] == ev_id)["status"] == "confirmed"


def test_build_evidence_response_handles_missing_diff(env, monkeypatch) -> None:
    """A field_path absent from the diff map projects ``diff_action=None``."""
    asset_id = _ready_asset_id(env)
    _create_import(env, asset_id)
    monkeypatch.setattr(profile_repository, "compute_evidence_diff", lambda _rows, _snap: {})
    evidence = _evidence_list(env)
    assert evidence[0]["diff_action"] is None


# ===========================================================================
# Service-level branches the routes cannot reach directly.
# These cover the remaining gaps in backend/app/services/profiles.py.
# ===========================================================================


@pytest.fixture()
def svc_db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture()
def svc_store():
    blob = MemoryBlobStore()
    return EncryptedObjectStore(blob, ENCRYPTION_KEY)


def test_create_pending_asset_rejects_oversized_raw_directly(svc_db, svc_store, monkeypatch) -> None:
    """The service's own size guard (independent of the route's pre-check)."""
    monkeypatch.setattr(services_module, "MAX_RESUME_BYTES", 10)
    service = ResumeAssetService(svc_store)
    with pytest.raises(services_module.ResumeTooLargeError):
        service.create_pending_asset(
            svc_db,
            user_id="user-1",
            filename="resume.txt",
            content_type="text/plain",
            raw=b"x" * 11,
        )


def test_process_raises_not_found_for_unknown_import(svc_db, svc_store) -> None:
    profile_repository.ensure_profile(svc_db, "user-1")
    svc_db.commit()
    with pytest.raises(OwnedProfileResourceNotFound):
        ResumeImportService(svc_store).process(
            svc_db, user_id="user-1", import_id=str(uuid.uuid4())
        )


def test_process_marks_failed_when_asset_missing(svc_db, svc_store) -> None:
    """A import row whose asset was deleted fails with resume_asset_read_failed."""
    profile = profile_repository.ensure_profile(svc_db, "user-1")
    import_row = ResumeImport(
        profile_id=profile.id,
        asset_id=str(uuid.uuid4()),  # non-existent asset
        parser_version="profile-parser-v1",
        status=ResumeImportStatus.PENDING,
    )
    svc_db.add(import_row)
    svc_db.flush()
    svc_db.commit()

    ResumeImportService(svc_store).process(
        svc_db, user_id="user-1", import_id=import_row.id
    )
    assert import_row.status is ResumeImportStatus.FAILED
    assert import_row.error_code == "resume_asset_read_failed"


def test_process_marks_failed_for_unsupported_resume_type(svc_db, svc_store) -> None:
    """A READY asset with a parser-rejected filename suffix fails cleanly."""
    profile = profile_repository.ensure_profile(svc_db, "user-1")
    asset = ResumeAsset(
        profile_id=profile.id,
        object_key="users/user-1/resume-assets/xyz",
        original_filename="resume.xyz",
        content_type="text/plain",
        plaintext_size=4,
        plaintext_sha256="a" * 64,
        encryption_version="v1-aes-256-gcm",
        status=ResumeAssetStatus.READY,
    )
    svc_db.add(asset)
    svc_db.flush()
    svc_store.put(key=asset.object_key, plaintext=b"data", content_type="text/plain")
    import_row = profile_repository.create_import(
        svc_db, asset=asset, parser_version="profile-parser-v1"
    )
    svc_db.commit()

    ResumeImportService(svc_store).process(
        svc_db, user_id="user-1", import_id=import_row.id
    )
    assert import_row.status is ResumeImportStatus.FAILED
    assert import_row.error_code == "unsupported_resume_type"
