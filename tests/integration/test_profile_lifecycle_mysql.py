from __future__ import annotations

import threading
import uuid

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.db.base import Base
from backend.app.db.models import (
    ConfirmedProfileVersion,
    Profile,
    ProfileFieldDecision,
    ProfileFieldEvidence,
    ResumeAsset,
    ResumeAssetStatus,
    ResumeImport,
    ResumeImportStatus,
)
from backend.app.domain.profiles import EvidenceDecisionAction
from backend.app.services.profiles import (
    ProfileService,
    StaleProfileVersionError,
)


def test_concurrent_confirmation_prevents_double_commit(
    destructive_mysql_url: str,
) -> None:
    """Two threads confirming the same profile version with row-level locking.

    Seed one user / profile / import / evidence with all decisions made, then
    synchronize two threads so both call ``create_confirmed_version`` with the
    same ``expected_version``. Exactly one must succeed; the other must raise
    ``StaleProfileVersionError``.  This proves that ``get_profile_for_update``
    (``SELECT ... FOR UPDATE``) serialises concurrent confirmation attempts.
    """
    engine = create_engine(destructive_mysql_url)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    db = SessionLocal()

    try:
        user_id = str(uuid.uuid4())
        profile_id = str(uuid.uuid4())
        import_id = str(uuid.uuid4())
        evidence_id = str(uuid.uuid4())

        profile = Profile(
            id=profile_id,
            user_id=user_id,
            version=0,
            local_sensitive_references={},
        )
        db.add(profile)

        asset = ResumeAsset(
            id=str(uuid.uuid4()),
            profile_id=profile_id,
            object_key=f"users/{user_id}/resume-assets/{uuid.uuid4().hex}",
            original_filename="resume.txt",
            content_type="text/plain",
            plaintext_size=6,
            plaintext_sha256="a" * 64,
            encryption_version="v1-aes-256-gcm",
            status=ResumeAssetStatus.READY,
        )
        db.add(asset)

        import_row = ResumeImport(
            id=import_id,
            profile_id=profile_id,
            asset_id=asset.id,
            parser_version="profile-parser-v1",
            status=ResumeImportStatus.AWAITING_CONFIRMATION,
        )
        db.add(import_row)

        evidence = ProfileFieldEvidence(
            id=evidence_id,
            profile_id=profile_id,
            resume_import_id=import_id,
            field_path="skills",
            candidate_value=["Python"],
            evidence_excerpt="Python",
            confidence=90,
            sequence=1,
        )
        db.add(evidence)

        decision = ProfileFieldDecision(
            profile_id=profile_id,
            evidence_id=evidence_id,
            actor_user_id=user_id,
            action=EvidenceDecisionAction.CONFIRM,
        )
        db.add(decision)

        db.commit()
        db.close()

        barrier = threading.Barrier(2, timeout=30)
        results: list[tuple[str, ConfirmedProfileVersion | None]] = []

        def _confirm() -> None:
            session = SessionLocal()
            try:
                barrier.wait()
                result = ProfileService().create_confirmed_version(
                    session,
                    user_id=user_id,
                    expected_version=0,
                    resume_import_id=import_id,
                )
                session.commit()
                results.append(("success", result))
            except StaleProfileVersionError:
                session.rollback()
                results.append(("stale_profile_version", None))
            finally:
                session.close()

        threads = [
            threading.Thread(target=_confirm),
            threading.Thread(target=_confirm),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        outcomes = sorted(r[0] for r in results)
        assert outcomes == ["stale_profile_version", "success"]

        # Exactly one ConfirmedProfileVersion row should exist
        db = SessionLocal()
        try:
            count = db.query(ConfirmedProfileVersion).count()
            assert count == 1
        finally:
            db.close()
    finally:
        engine.dispose()
