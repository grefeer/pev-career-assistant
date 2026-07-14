from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.db.models import Device, DeviceStatus


def get_by_id(db: Session, device_id: str) -> Device | None:
    return db.get(Device, device_id)


def get_by_token_hash(db: Session, token_hash: str) -> Device | None:
    return db.scalar(select(Device).where(Device.token_hash == token_hash))


def list_for_user(db: Session, user_id: str) -> list[Device]:
    return list(
        db.scalars(
            select(Device)
            .where(Device.user_id == user_id)
            .order_by(Device.paired_at.desc())
        )
    )


def get_active_owned(db: Session, *, user_id: str, device_id: str) -> Device | None:
    return db.scalar(
        select(Device).where(
            Device.id == device_id,
            Device.user_id == user_id,
            Device.status == DeviceStatus.ACTIVE,
        )
    )
