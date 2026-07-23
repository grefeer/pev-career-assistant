"""Thin service over the preferences repository.

Business logic only (validation + high-level intent -> field mapping); no SQL.
The repository performs the actual writes.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from backend.app.db.models import UserPreference
from backend.app.domain.preferences import WorkModePreference
from backend.app.repositories import preferences as preferences_repo


def set_preferences(db: Session, user_id: str, **changes: Any) -> UserPreference:
    """Replace preference fields for ``user_id`` and bump ``version``."""
    if "work_mode" in changes and changes["work_mode"] is not None:
        changes["work_mode"] = WorkModePreference(coerce(changes["work_mode"]))
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
