"""Thin service over the preferences repository.

Business logic only (validation + high-level intent -> field mapping); no SQL.
The repository performs the actual writes.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from backend.app.db.models import UserPreference
from backend.app.domain.preferences import WorkModePreference
from backend.app.domain.personalized_discovery import normalize_role_terms
from backend.app.repositories import preferences as preferences_repo

# Role-list fields normalized through Task 1 (trim/dedupe/non-blank).
_ROLE_LIST_FIELDS = ("desired_roles", "role_synonyms", "excluded_roles")


def set_preferences(db: Session, user_id: str, **changes: Any) -> UserPreference:
    """Replace preference fields for ``user_id`` and bump ``version``.

    Role-list fields (``desired_roles``, ``role_synonyms``, ``excluded_roles``)
    are normalized through ``normalize_role_terms`` (trim, case-insensitive
    dedupe, non-blank). ``personalized_discovery_min_score`` is bounded to
    ``[0, 100]``.
    """
    if "work_mode" in changes and changes["work_mode"] is not None:
        changes["work_mode"] = WorkModePreference(coerce(changes["work_mode"]))
    for field in _ROLE_LIST_FIELDS:
        if field in changes:
            changes[field] = normalize_role_terms(changes[field])
    if "personalized_discovery_min_score" in changes:
        score = changes["personalized_discovery_min_score"]
        if score is not None:
            try:
                score = float(score)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "personalized_discovery_min_score must be a number 0..100"
                ) from exc
            if score < 0 or score > 100:
                raise ValueError(
                    "personalized_discovery_min_score must be within 0..100"
                )
            changes["personalized_discovery_min_score"] = score
    return preferences_repo.upsert(db, user_id, **changes)


def get_preferences_summary(db: Session, user_id: str) -> dict[str, Any]:
    """Flat, JSON-friendly view of the user's preferences (never ``None``)."""
    return preferences_repo.to_summary(preferences_repo.get_for_user(db, user_id))


def get_preferences_version(db: Session, user_id: str) -> int:
    return preferences_repo.get_version(db, user_id)


def coerce(value: Any) -> str:
    """Normalize enum-ish input to a plain string for ``WorkModePreference``."""
    if hasattr(value, "value"):
        return value.value
    return str(value)
