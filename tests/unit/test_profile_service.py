from __future__ import annotations

import base64

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.app.db.base import Base
from backend.app.db.models import (
    ResumeAsset,
    ResumeAssetStatus,
)
from backend.app.domain.profiles import (
    EvidenceDecisionAction,
)
from backend.app.repositories import profiles as profile_repository
from backend.app.services.profiles import (
    EvidenceDecisionInput,
    ProfileService,
    ResumeAssetService,
    ResumeImportService,
)
from backend.app.services.storage import (
    EncryptedObjectStore,
)
from tests.unit.test_encrypted_storage import MemoryBlobStore


@pytest.fixture
def profile_db() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    session_local = sessionmaker(bind=engine)
    session = session_local()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def object_store() -> EncryptedObjectStore:
    blob_store = MemoryBlobStore()
    key = base64.b64encode(bytes(range(32))).decode("ascii")
    return EncryptedObjectStore(blob_store, key)


@pytest.fixture
def ready_asset_context(
    profile_db: Session, object_store: EncryptedObjectStore
) -> tuple[str, ResumeAsset]:
    service = ResumeAssetService(object_store)
    resume_content = "技能\nPython".encode("utf-8")
    asset = service.create_pending_asset(
        profile_db,
        user_id="user-1",
        filename="resume.txt",
        content_type="text/plain",
        raw=resume_content,
    )
    profile_db.commit()
    service.write_encrypted_object(asset, resume_content)
    reconciled = service.reconcile(profile_db, user_id="user-1", asset_id=asset.id)
    assert reconciled.status is ResumeAssetStatus.READY
    return "user-1", reconciled


def test_upload_persists_pending_before_object_write_and_reconciles_after_commit_error(
    profile_db: Session, object_store: EncryptedObjectStore
) -> None:
    service = ResumeAssetService(object_store)
    asset = service.create_pending_asset(
        profile_db,
        user_id="user-1",
        filename="resume.txt",
        content_type="text/plain",
        raw=b"private resume",
    )
    profile_db.commit()
    service.write_encrypted_object(asset, b"private resume")
    reconciled = service.reconcile(profile_db, user_id="user-1", asset_id=asset.id)
    assert reconciled.status is ResumeAssetStatus.READY
    assert reconciled.error_code is None


def test_reparse_appends_import_and_preserves_confirmed_version(
    profile_db: Session,
    ready_asset_context: tuple[str, ResumeAsset],
    object_store: EncryptedObjectStore,
) -> None:
    owner_id, ready_asset = ready_asset_context
    import_service = ResumeImportService(object_store)
    profile_service = ProfileService()
    first = import_service.start(profile_db, user_id=owner_id, asset_id=ready_asset.id)
    import_service.process(profile_db, user_id=owner_id, import_id=first.id)
    profile = profile_repository.ensure_profile(profile_db, owner_id)
    evidence = profile_repository.list_import_evidence(profile_db, first.id)
    decided = profile_service.apply_decisions(
        profile_db,
        user_id=owner_id,
        expected_version=profile.version,
        decisions=tuple(
            EvidenceDecisionInput(item.id, EvidenceDecisionAction.CONFIRM)
            for item in evidence
        ),
    )
    confirmed = profile_service.create_confirmed_version(
        profile_db,
        user_id=owner_id,
        expected_version=decided.version,
        resume_import_id=first.id,
    )
    second = import_service.start(profile_db, user_id=owner_id, asset_id=ready_asset.id)
    import_service.process(profile_db, user_id=owner_id, import_id=second.id)
    profile_db.flush()
    assert second.id != first.id
    assert confirmed.facts_snapshot == {"skills": ["Python"]}
