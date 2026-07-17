"""Builds ConfirmedProfileSnapshot, filtering out local-sensitive fields."""
from datetime import datetime
from sqlalchemy.orm import Session
from backend.app.db.models import ConfirmedProfileVersion as CPV
from backend.app.services.field_classification import filter_non_sensitive


class ConfirmedProfileSnapshot:
    def __init__(
        self, *, profile_version_id, profile_id, version_number,
        facts, evidence_refs, confirmed_at,
    ):
        self.profile_version_id = profile_version_id
        self.profile_id = profile_id
        self.version_number = version_number
        self.facts = facts
        self.evidence_refs = evidence_refs
        self.confirmed_at = confirmed_at


def build_confirmed_profile_snapshot(
    db: Session, profile_version_id: str, user_id: str
) -> ConfirmedProfileSnapshot:
    cpv = db.query(CPV).filter(
        CPV.id == profile_version_id,
        CPV.profile.has(user_id=user_id),
    ).first()
    if cpv is None:
        raise ValueError("not_found")

    raw_facts = cpv.facts_snapshot or {}
    non_sensitive_facts = filter_non_sensitive(raw_facts)

    raw_evidence = cpv.evidence_refs or {}
    # evidence_refs shape: {field_path: [evidence_id, ...]}
    # Filter to non-sensitive fields only
    from backend.app.services.field_classification import is_non_sensitive
    filtered_evidence = {
        fp: ids for fp, ids in raw_evidence.items()
        if is_non_sensitive(fp)
    }

    return ConfirmedProfileSnapshot(
        profile_version_id=cpv.id,
        profile_id=cpv.profile_id,
        version_number=cpv.version_number,
        facts=non_sensitive_facts,
        evidence_refs=filtered_evidence,
        confirmed_at=cpv.aggregate_version,  # using aggregate_version as proxy for confirmed_at
    )
