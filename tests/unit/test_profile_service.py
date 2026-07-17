from __future__ import annotations

import base64
from datetime import timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.app.db.base import Base
from backend.app.db.models import (
    ResumeAsset,
    ResumeAssetStatus,
)
from backend.app.domain.profiles import (
    EvidenceCandidate,
    EvidenceDecisionAction,
)
from backend.app.repositories import profiles as profile_repository
from backend.app.services.profiles import (
    EvidenceDecisionInput,
    OwnedProfileResourceNotFound,
    ProfileService,
    ProfileValidationError,
    ResumeAssetStateConflict,
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


def test_reconcile_rejects_object_with_mismatched_content_type(
    profile_db: Session,
) -> None:
    class MetadataOnlyStore:
        def inspect(self, *, key: str) -> SimpleNamespace:
            return SimpleNamespace(
                encryption="v1-aes-256-gcm", content_type="application/pdf"
            )

    service = ResumeAssetService(MetadataOnlyStore())  # type: ignore[arg-type]
    asset = service.create_pending_asset(
        profile_db,
        user_id="user-1",
        filename="resume.txt",
        content_type="text/plain",
        raw=b"resume",
    )

    with pytest.raises(ResumeAssetStateConflict):
        service.reconcile(profile_db, user_id="user-1", asset_id=asset.id)

    assert asset.status is ResumeAssetStatus.PENDING_UPLOAD


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


def test_import_processing_records_utc_lifecycle_timestamps(
    profile_db: Session,
    ready_asset_context: tuple[str, ResumeAsset],
    object_store: EncryptedObjectStore,
) -> None:
    owner_id, ready_asset = ready_asset_context
    service = ResumeImportService(object_store)

    import_row = service.start(profile_db, user_id=owner_id, asset_id=ready_asset.id)
    assert import_row.started_at is None
    assert import_row.finished_at is None

    service.process(profile_db, user_id=owner_id, import_id=import_row.id)

    assert import_row.started_at is not None
    assert import_row.finished_at is not None
    assert import_row.started_at.tzinfo is timezone.utc
    assert import_row.finished_at.tzinfo is timezone.utc
    assert import_row.started_at <= import_row.finished_at


def test_apply_decisions_hides_another_users_evidence(
    profile_db: Session,
) -> None:
    first_profile = profile_repository.ensure_profile(profile_db, "user-1")
    second_profile = profile_repository.ensure_profile(profile_db, "user-2")
    asset = ResumeAsset(
        profile_id=second_profile.id,
        object_key="users/user-2/resume-assets/foreign",
        original_filename="resume.txt",
        content_type="text/plain",
        plaintext_size=6,
        plaintext_sha256="a" * 64,
        encryption_version="v1-aes-256-gcm",
        status=ResumeAssetStatus.READY,
    )
    profile_db.add(asset)
    profile_db.flush()
    import_row = profile_repository.create_import(
        profile_db, asset=asset, parser_version="profile-v1"
    )
    evidence = profile_repository.append_evidence(
        profile_db,
        profile_id=second_profile.id,
        import_id=import_row.id,
        candidates=(EvidenceCandidate("skills", ["Python"], "Python", 90),),
    )[0]

    with pytest.raises(OwnedProfileResourceNotFound):
        ProfileService().apply_decisions(
            profile_db,
            user_id="user-1",
            expected_version=first_profile.version,
            decisions=(
                EvidenceDecisionInput(evidence.id, EvidenceDecisionAction.CONFIRM),
            ),
        )


@pytest.mark.parametrize(
    "decision",
    [
        EvidenceDecisionInput("evidence-id", EvidenceDecisionAction.CORRECT),
        EvidenceDecisionInput(
            "evidence-id", EvidenceDecisionAction.CONFIRM, corrected_value="unexpected"
        ),
    ],
)
def test_apply_decisions_enforces_corrected_value_contract(
    profile_db: Session, decision: EvidenceDecisionInput
) -> None:
    profile = profile_repository.ensure_profile(profile_db, "user-1")
    asset = ResumeAsset(
        profile_id=profile.id,
        object_key="users/user-1/resume-assets/own",
        original_filename="resume.txt",
        content_type="text/plain",
        plaintext_size=6,
        plaintext_sha256="a" * 64,
        encryption_version="v1-aes-256-gcm",
        status=ResumeAssetStatus.READY,
    )
    profile_db.add(asset)
    profile_db.flush()
    import_row = profile_repository.create_import(
        profile_db, asset=asset, parser_version="profile-v1"
    )
    evidence = profile_repository.append_evidence(
        profile_db,
        profile_id=profile.id,
        import_id=import_row.id,
        candidates=(EvidenceCandidate("skills", ["Python"], "Python", 90),),
    )[0]
    decision = EvidenceDecisionInput(
        evidence.id, decision.action, corrected_value=decision.corrected_value
    )
    with pytest.raises(ProfileValidationError):
        ProfileService().apply_decisions(
            profile_db,
            user_id="user-1",
            expected_version=0,
            decisions=(decision,),
        )
