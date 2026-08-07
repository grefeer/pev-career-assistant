from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Sequence

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from backend.app.db.models import (
    ConfirmedProfileVersion,
    Profile,
    ProfileFieldDecision,
    ProfileFieldEvidence,
    ResumeAsset,
    ResumeImport,
    ResumeImportStatus,
)
from backend.app.domain.profiles import (
    EvidenceCandidate,
    EvidenceDecisionAction,
    EvidenceDiffAction,
)


def _ensure_owned_profile(db: Session, user_id: str) -> Profile:
    profile = db.scalar(
        select(Profile).where(Profile.user_id == user_id)
    )
    if profile is None:
        profile = Profile(user_id=user_id, version=0, local_sensitive_references={})
        db.add(profile)
        db.flush()
    return profile


def ensure_profile(db: Session, user_id: str) -> Profile:
    return _ensure_owned_profile(db, user_id)


def get_profile_for_update(db: Session, user_id: str) -> Profile:
    profile = db.scalar(
        select(Profile)
        .where(Profile.user_id == user_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if profile is None:
        profile = Profile(user_id=user_id, version=0, local_sensitive_references={})
        db.add(profile)
        db.flush()
    return profile


def get_profile_by_user(db: Session, user_id: str) -> Profile | None:
    """Read-only profile lookup; returns None when the profile does not exist."""
    return db.scalar(select(Profile).where(Profile.user_id == user_id))


def set_active_version(
    db: Session, profile: Profile, version_id: str | None
) -> None:
    """Point the profile at the confirmed version the runtime should consume."""
    profile.active_version_id = version_id
    db.flush()


def get_owned_asset(db: Session, user_id: str, asset_id: str) -> ResumeAsset | None:
    return db.scalar(
        select(ResumeAsset)
        .join(Profile, ResumeAsset.profile_id == Profile.id)
        .where(Profile.user_id == user_id, ResumeAsset.id == asset_id)
    )


def list_owned_assets(db: Session, user_id: str) -> Sequence[ResumeAsset]:
    return db.scalars(
        select(ResumeAsset)
        .join(Profile, ResumeAsset.profile_id == Profile.id)
        .where(Profile.user_id == user_id)
        .order_by(ResumeAsset.created_at.desc())
    ).all()


def list_imports_for_asset(db: Session, asset_id: str) -> Sequence[ResumeImport]:
    return db.scalars(
        select(ResumeImport).where(ResumeImport.asset_id == asset_id)
    ).all()


def delete_asset(db: Session, asset: ResumeAsset) -> None:
    """Delete an asset with its imports, evidence, and decisions.

    Dependents are removed explicitly so deletion is correct whether or not the
    backend enforces ON DELETE CASCADE (production MySQL does; the in-memory
    SQLite used in tests does not). ``ResumeImport.asset_id`` is ON DELETE
    RESTRICT, so imports must be removed before the asset row.
    """
    imports = list_imports_for_asset(db, asset.id)
    if imports:
        import_ids = [imp.id for imp in imports]
        evidence_rows = db.scalars(
            select(ProfileFieldEvidence).where(
                ProfileFieldEvidence.resume_import_id.in_(import_ids)
            )
        ).all()
        if evidence_rows:
            db.execute(
                delete(ProfileFieldDecision).where(
                    ProfileFieldDecision.evidence_id.in_(
                        [ev.id for ev in evidence_rows]
                    )
                )
            )
        db.execute(
            delete(ProfileFieldEvidence).where(
                ProfileFieldEvidence.resume_import_id.in_(import_ids)
            )
        )
        db.execute(
            delete(ResumeImport).where(ResumeImport.asset_id == asset.id)
        )
    db.execute(delete(ResumeAsset).where(ResumeAsset.id == asset.id))
    db.flush()


def get_owned_import(db: Session, user_id: str, import_id: str) -> ResumeImport | None:
    return db.scalar(
        select(ResumeImport)
        .join(Profile, ResumeImport.profile_id == Profile.id)
        .where(Profile.user_id == user_id, ResumeImport.id == import_id)
    )


def get_owned_version(
    db: Session, user_id: str, version_id: str
) -> ConfirmedProfileVersion | None:
    return db.scalar(
        select(ConfirmedProfileVersion)
        .join(Profile, ConfirmedProfileVersion.profile_id == Profile.id)
        .where(Profile.user_id == user_id, ConfirmedProfileVersion.id == version_id)
    )


def create_import(
    db: Session, *, asset: ResumeAsset, parser_version: str
) -> ResumeImport:
    import_row = ResumeImport(
        profile_id=asset.profile_id,
        asset_id=asset.id,
        parser_version=parser_version,
        status=ResumeImportStatus.PENDING,
    )
    db.add(import_row)
    db.flush()
    return import_row


def update_import_status(
    db: Session,
    import_row: ResumeImport,
    *,
    status: ResumeImportStatus,
    error_code: str | None = None,
) -> None:
    import_row.status = status
    import_row.error_code = error_code
    now = datetime.now(timezone.utc)
    if status is ResumeImportStatus.PARSING and import_row.started_at is None:
        import_row.started_at = now
    if status in {
        ResumeImportStatus.AWAITING_CONFIRMATION,
        ResumeImportStatus.NEEDS_MANUAL_ENTRY,
        ResumeImportStatus.FAILED,
    }:
        import_row.finished_at = now
    db.flush()


def append_evidence(
    db: Session,
    *,
    profile_id: str,
    import_id: str,
    candidates: tuple[EvidenceCandidate, ...],
) -> list[ProfileFieldEvidence]:
    rows: list[ProfileFieldEvidence] = []
    for seq, candidate in enumerate(candidates, start=1):
        row = ProfileFieldEvidence(
            profile_id=profile_id,
            resume_import_id=import_id,
            field_path=candidate.field_path,
            candidate_value=candidate.candidate_value,
            evidence_excerpt=candidate.evidence_excerpt,
            confidence=candidate.confidence,
            sequence=seq,
        )
        db.add(row)
        rows.append(row)
    db.flush()
    return rows


def list_import_evidence(
    db: Session, import_id: str
) -> Sequence[ProfileFieldEvidence]:
    return db.scalars(
        select(ProfileFieldEvidence)
        .where(ProfileFieldEvidence.resume_import_id == import_id)
        .order_by(ProfileFieldEvidence.sequence)
    ).all()


def latest_decisions_by_evidence(
    db: Session, profile_id: str
) -> dict[str, ProfileFieldDecision]:
    subq = (
        select(
            ProfileFieldDecision.evidence_id,
            func.max(ProfileFieldDecision.created_at).label("max_created"),
        )
        .where(ProfileFieldDecision.profile_id == profile_id)
        .group_by(ProfileFieldDecision.evidence_id)
        .subquery()
    )
    rows = db.scalars(
        select(ProfileFieldDecision)
        .join(
            subq,
            (ProfileFieldDecision.evidence_id == subq.c.evidence_id)
            & (ProfileFieldDecision.created_at == subq.c.max_created),
        )
    ).all()
    return {row.evidence_id: row for row in rows}


def append_decision(
    db: Session,
    *,
    profile_id: str,
    evidence_id: str,
    actor_user_id: str,
    action: EvidenceDecisionAction,
    resolved_value: Any = None,
) -> ProfileFieldDecision:
    decision = ProfileFieldDecision(
        profile_id=profile_id,
        evidence_id=evidence_id,
        actor_user_id=actor_user_id,
        action=action,
        resolved_value=resolved_value,
    )
    db.add(decision)
    db.flush()
    return decision


def update_profile_version(db: Session, profile: Profile) -> None:
    profile.version += 1
    db.flush()


def next_confirmed_version_number(db: Session, profile_id: str) -> int:
    result = db.scalar(
        select(func.max(ConfirmedProfileVersion.version_number)).where(
            ConfirmedProfileVersion.profile_id == profile_id
        )
    )
    return (result or 0) + 1


def create_confirmed_version(
    db: Session,
    *,
    profile_id: str,
    version_number: int,
    aggregate_version: int,
    facts_snapshot: dict[str, Any],
    evidence_refs: dict[str, Any],
    local_sensitive_references: dict[str, Any],
) -> ConfirmedProfileVersion:
    version = ConfirmedProfileVersion(
        profile_id=profile_id,
        version_number=version_number,
        aggregate_version=aggregate_version,
        facts_snapshot=facts_snapshot,
        evidence_refs=evidence_refs,
        local_sensitive_references=local_sensitive_references,
    )
    db.add(version)
    db.flush()
    return version


def list_versions(
    db: Session, user_id: str
) -> Sequence[ConfirmedProfileVersion]:
    return db.scalars(
        select(ConfirmedProfileVersion)
        .join(Profile, ConfirmedProfileVersion.profile_id == Profile.id)
        .where(Profile.user_id == user_id)
        .order_by(ConfirmedProfileVersion.created_at.desc())
    ).all()


def get_profile_evidence_with_decisions(
    db: Session, profile_id: str
) -> Sequence[ProfileFieldEvidence]:
    """Return each evidence row once; callers resolve latest decisions separately."""
    return db.scalars(
        select(ProfileFieldEvidence)
        .where(ProfileFieldEvidence.profile_id == profile_id)
        .order_by(ProfileFieldEvidence.created_at.desc())
    ).all()


def get_profile_evidence_by_id(
    db: Session, profile_id: str, evidence_id: str
) -> ProfileFieldEvidence | None:
    return db.scalar(
        select(ProfileFieldEvidence).where(
            ProfileFieldEvidence.profile_id == profile_id,
            ProfileFieldEvidence.id == evidence_id,
        )
    )


def compute_evidence_diff(
    evidence_rows: Sequence[ProfileFieldEvidence],
    latest_snapshot: dict[str, Any] | None,
) -> dict[str, EvidenceDiffAction]:
    """Compute diff actions for evidence against a confirmed snapshot.

    Returns a mapping of field_path to diff action.
    """
    diff: dict[str, EvidenceDiffAction] = {}
    latest = latest_snapshot or {}

    # Track conflicts: same import, same path, different values
    path_values: dict[str, set[int]] = {}
    for ev in evidence_rows:
        val_hash = hash(str(ev.candidate_value))
        if ev.field_path not in path_values:
            path_values[ev.field_path] = set()
        path_values[ev.field_path].add(val_hash)

    for ev in evidence_rows:
        if ev.field_path in diff:
            continue  # already determined
        if ev.field_path not in latest:
            if len(path_values.get(ev.field_path, set())) > 1:
                diff[ev.field_path] = EvidenceDiffAction.CONFLICT
            else:
                diff[ev.field_path] = EvidenceDiffAction.ADD
        else:
            if len(path_values.get(ev.field_path, set())) > 1:
                diff[ev.field_path] = EvidenceDiffAction.CONFLICT
            elif ev.candidate_value == latest.get(ev.field_path):
                diff[ev.field_path] = EvidenceDiffAction.UNCHANGED
            else:
                diff[ev.field_path] = EvidenceDiffAction.REPLACE
    return diff
