"""Repository for UserPreference (personal-mode memory).

Data access only - no business logic. A user has at most one preference row
(`uq_user_preferences_user`); `upsert` replaces field values and bumps
`version` so downstream caches keyed on `preferences_version` are invalidated.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.db.models import UserPreference

# Columns callers may set via ``upsert(..., **changes)``. Keeps the surface
# explicit so a typo doesn't silently land in the JSON payload.
_PREFERENCE_COLUMNS = (
    "desired_roles",
    "target_cities",
    "salary_min",
    "salary_max",
    "excluded_companies",
    "excluded_industries",
    "preferred_industries",
    "preferred_recruitment_types",
    "work_mode",
    "is_active_search",
    "notes",
)


def get_for_user(db: Session, user_id: str) -> UserPreference | None:
    """Return the single preference row for ``user_id`` or ``None``."""
    return db.scalars(
        select(UserPreference).where(UserPreference.user_id == user_id)
    ).first()


def get_version(db: Session, user_id: str) -> int:
    """Current preferences version, or 0 when no row exists yet."""
    pref = get_for_user(db, user_id)
    return pref.version if pref else 0


def upsert(db: Session, user_id: str, **changes: Any) -> UserPreference:
    """Insert or update the preference row for ``user_id``.

    Only keys in ``_PREFERENCE_COLUMNS`` are applied; unknown keys are ignored.
    On update every provided field is replaced and ``version`` is bumped by 1
    so cached relevance scores (keyed on ``preferences_version``) are retired.
    """
    valid = {k: v for k, v in changes.items() if k in _PREFERENCE_COLUMNS}

    existing = get_for_user(db, user_id)
    if existing is None:
        pref = UserPreference(user_id=user_id, **valid)
        db.add(pref)
        db.flush()
        return pref

    for column, value in valid.items():
        setattr(existing, column, value)
    existing.version = (existing.version or 0) + 1
    db.flush()
    return existing


def to_summary(pref: UserPreference | None) -> dict[str, Any]:
    """Flat dict view used by the ranker / recommendation service."""
    if pref is None:
        return {
            "desired_roles": [],
            "target_cities": [],
            "salary_min": None,
            "salary_max": None,
            "excluded_companies": [],
            "excluded_industries": [],
            "preferred_industries": [],
            "preferred_recruitment_types": [],
            "work_mode": None,
            "is_active_search": True,
            "notes": None,
            "version": 0,
        }
    return {
        "desired_roles": pref.desired_roles or [],
        "target_cities": pref.target_cities or [],
        "salary_min": pref.salary_min,
        "salary_max": pref.salary_max,
        "excluded_companies": pref.excluded_companies or [],
        "excluded_industries": pref.excluded_industries or [],
        "preferred_industries": pref.preferred_industries or [],
        "preferred_recruitment_types": pref.preferred_recruitment_types or [],
        "work_mode": pref.work_mode.value if pref.work_mode else None,
        "is_active_search": pref.is_active_search,
        "notes": pref.notes,
        "version": pref.version,
    }
