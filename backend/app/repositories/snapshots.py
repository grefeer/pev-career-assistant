from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.db.models import ApplicationSnapshot


def create(db: Session, **kwargs: Any) -> ApplicationSnapshot:
    """Create a new ApplicationSnapshot row."""
    snapshot = ApplicationSnapshot(**kwargs)
    db.add(snapshot)
    db.flush()
    return snapshot


def get_by_id(
    db: Session, snapshot_id: str, user_id: str
) -> ApplicationSnapshot | None:
    """Fetch a snapshot by id, scoped to the given user."""
    return db.scalar(
        select(ApplicationSnapshot).where(
            ApplicationSnapshot.id == snapshot_id,
            ApplicationSnapshot.user_id == user_id,
        )
    )


def list_by_user(
    db: Session, user_id: str
) -> list[ApplicationSnapshot]:
    """List all snapshots for a user, newest first."""
    return list(
        db.scalars(
            select(ApplicationSnapshot)
            .where(ApplicationSnapshot.user_id == user_id)
            .order_by(ApplicationSnapshot.created_at.desc())
        ).all()
    )
