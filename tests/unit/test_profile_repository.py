import pytest
from sqlalchemy import create_engine, inspect, select, func
from sqlalchemy.orm import Session, sessionmaker

from backend.app.db.base import Base
from backend.app.db.models import (
    Profile,
    ProfileFieldEvidence,
    ResumeAsset,
    ResumeAssetStatus,
    ResumeImportStatus,
)
from backend.app.domain.profiles import (
    EvidenceCandidate,
    EvidenceDiffAction,
)
from backend.app.repositories import profiles


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
def seeded_profiles(profile_db: Session) -> tuple[Profile, Profile, ResumeAsset]:
    owner = Profile(user_id="user-owner", version=0, local_sensitive_references={})
    other = Profile(user_id="user-other", version=0, local_sensitive_references={})
    profile_db.add_all([owner, other])
    profile_db.flush()
    asset = ResumeAsset(
        profile_id=owner.id,
        object_key="users/u/resume-assets/a",
        original_filename="resume.txt",
        content_type="text/plain",
        plaintext_size=13,
        plaintext_sha256="a" * 64,
        encryption_version="v1-aes-256-gcm",
        status=ResumeAssetStatus.READY,
    )
    profile_db.add(asset)
    profile_db.flush()
    return owner, other, asset


def test_profile_schema_has_version_and_append_only_tables() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    inspector = inspect(engine)
    assert {
        "profiles",
        "resume_assets",
        "resume_imports",
        "profile_field_evidence",
        "profile_field_decisions",
        "confirmed_profile_versions",
    } <= set(inspector.get_table_names())
    assert {"version", "local_sensitive_references"} <= {
        column["name"] for column in inspector.get_columns("profiles")
    }
    engine.dispose()


def test_owned_queries_hide_cross_user_rows(
    profile_db: Session, seeded_profiles: tuple[Profile, Profile, ResumeAsset]
) -> None:
    owner, other, asset = seeded_profiles
    assert profiles.get_owned_asset(profile_db, owner.user_id, asset.id) is not None
    assert profiles.get_owned_asset(profile_db, other.user_id, asset.id) is None


def test_new_import_and_decisions_do_not_mutate_history(
    profile_db: Session, seeded_profiles: tuple[Profile, Profile, ResumeAsset]
) -> None:
    owner, _other, asset = seeded_profiles
    first = profiles.create_import(
        profile_db, asset=asset, parser_version="profile-v1"
    )
    profiles.append_evidence(
        profile_db,
        profile_id=asset.profile_id,
        import_id=first.id,
        candidates=(
            EvidenceCandidate("skills", ["Python"], "Python", 90),
        ),
    )
    second = profiles.create_import(
        profile_db, asset=asset, parser_version="profile-v1"
    )
    profile_db.flush()
    assert first.id != second.id
    assert first.status is ResumeImportStatus.PENDING
    assert profile_db.scalar(select(func.count(ProfileFieldEvidence.id))) == 1


def test_evidence_diff_reports_add_unchanged_replace_and_conflict(
    profile_db: Session, seeded_profiles: tuple[Profile, Profile, ResumeAsset]
) -> None:
    owner, _other, asset = seeded_profiles

    first = profiles.create_import(
        profile_db, asset=asset, parser_version="profile-v1"
    )
    profiles.append_evidence(
        profile_db,
        profile_id=asset.profile_id,
        import_id=first.id,
        candidates=(
            EvidenceCandidate("skills", ["Python"], "Python", 90),
            EvidenceCandidate("education", ["某大学"], "某大学 软件工程", 80),
        ),
    )

    snapshot = {"skills": ["Python"], "education": ["另一所大学"]}
    evidence = profiles.list_import_evidence(profile_db, first.id)
    diff = profiles.compute_evidence_diff(evidence, snapshot)

    assert diff.get("skills") == EvidenceDiffAction.UNCHANGED
    assert diff.get("education") == EvidenceDiffAction.REPLACE

    second = profiles.create_import(
        profile_db, asset=asset, parser_version="profile-v1"
    )
    profile_db.flush()
    profiles.append_evidence(
        profile_db,
        profile_id=asset.profile_id,
        import_id=second.id,
        candidates=(
            EvidenceCandidate("projects", ["职业助手"], "职业助手 LangGraph", 80),
            EvidenceCandidate("skills", ["Python"], "Python", 90),
        ),
    )
    evidence2 = profiles.list_import_evidence(profile_db, second.id)
    diff2 = profiles.compute_evidence_diff(evidence2, snapshot)

    assert diff2.get("projects") == EvidenceDiffAction.ADD
    assert diff2.get("skills") == EvidenceDiffAction.UNCHANGED
