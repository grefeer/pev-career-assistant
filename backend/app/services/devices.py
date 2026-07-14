from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from backend.app.db.base import utc_now
from backend.app.db.models import (
    AuditEvent,
    Device,
    DevicePlatform,
    DeviceStatus,
)
from backend.app.repositories import devices


PAIRING_TTL_SECONDS = 600
ONLINE_TTL_SECONDS = 90


class InvalidPairingTicketError(ValueError):
    pass


class DeviceNotFoundError(LookupError):
    pass


@dataclass(frozen=True)
class PairingTicket:
    code: str
    expires_at: datetime


@dataclass(frozen=True)
class IssuedDevice:
    device: Device
    plaintext_token: str


@dataclass(frozen=True)
class ListedDevice:
    device: Device
    online: bool


def token_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _audit(
    db: Session,
    *,
    event_type: str,
    entity_id: str,
    actor_user_id: str | None = None,
    actor_device_id: str | None = None,
    payload: dict[str, str],
) -> None:
    db.add(
        AuditEvent(
            actor_user_id=actor_user_id,
            actor_device_id=actor_device_id,
            event_type=event_type,
            entity_type="device",
            entity_id=entity_id,
            correlation_id=uuid.uuid4().hex,
            redacted_payload=payload,
        )
    )


class DeviceService:
    def __init__(self, redis_client: Any) -> None:
        self.redis = redis_client

    def create_pairing_ticket(
        self, db: Session | None = None, *, user_id: str
    ) -> PairingTicket:
        code = secrets.token_urlsafe(24)
        created_at = datetime.now(timezone.utc)
        value = json.dumps(
            {"user_id": user_id, "created_at": created_at.isoformat()},
            separators=(",", ":"),
        )
        self.redis.setex(
            f"pairing-ticket:{token_digest(code)}", PAIRING_TTL_SECONDS, value
        )
        if db is not None:
            _audit(
                db,
                event_type="device.pairing_ticket_created",
                entity_id=user_id,
                actor_user_id=user_id,
                payload={
                    "platform": DevicePlatform.WINDOWS.value,
                    "result": "created",
                },
            )
            db.commit()
        return PairingTicket(
            code=code, expires_at=created_at + timedelta(seconds=PAIRING_TTL_SECONDS)
        )

    def redeem_pairing_ticket(
        self,
        db: Session,
        *,
        code: str,
        name: str,
        public_key_pem: str,
    ) -> IssuedDevice:
        raw = self.redis.getdel(f"pairing-ticket:{token_digest(code)}")
        if raw is None:
            raise InvalidPairingTicketError("invalid, expired, or used pairing ticket")
        try:
            ticket = json.loads(raw)
            user_id = ticket["user_id"]
            if not isinstance(user_id, str) or not user_id:
                raise ValueError
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            raise InvalidPairingTicketError("invalid pairing ticket") from None

        plaintext_token = secrets.token_urlsafe(32)
        device = Device(
            user_id=user_id,
            name=name,
            platform=DevicePlatform.WINDOWS,
            status=DeviceStatus.ACTIVE,
            token_hash=token_digest(plaintext_token),
            public_key_pem=public_key_pem,
        )
        db.add(device)
        db.flush()
        _audit(
            db,
            event_type="device.paired",
            entity_id=device.id,
            actor_user_id=user_id,
            payload={"platform": DevicePlatform.WINDOWS.value, "result": "paired"},
        )
        db.commit()
        return IssuedDevice(device=device, plaintext_token=plaintext_token)

    def authenticate(self, db: Session, plaintext_token: str) -> Device | None:
        digest = token_digest(plaintext_token)
        device = devices.get_by_token_hash(db, digest)
        if device is None or device.status is not DeviceStatus.ACTIVE:
            return None
        if not hmac.compare_digest(device.token_hash, digest):
            return None
        return device

    def heartbeat(
        self, db: Session, plaintext_token: str, *, version: str
    ) -> Device | None:
        device = self.authenticate(db, plaintext_token)
        if device is None:
            return None
        device.last_seen_at = utc_now()
        device.version = version
        self.redis.setex(f"device-online:{device.id}", ONLINE_TTL_SECONDS, "1")
        db.commit()
        return device

    def list_for_user(self, db: Session, user_id: str) -> list[ListedDevice]:
        return [
            ListedDevice(
                device=device,
                online=self.redis.exists(f"device-online:{device.id}") == 1,
            )
            for device in devices.list_for_user(db, user_id)
        ]

    def revoke(self, db: Session, *, user_id: str, device_id: str) -> Device:
        device = devices.get_active_owned(db, user_id=user_id, device_id=device_id)
        if device is None:
            raise DeviceNotFoundError(device_id)
        device.status = DeviceStatus.REVOKED
        device.revoked_at = utc_now()
        self.redis.delete(f"device-online:{device.id}")
        _audit(
            db,
            event_type="device.revoked",
            entity_id=device.id,
            actor_user_id=user_id,
            payload={
                "platform": device.platform.value,
                "version": device.version or "",
                "result": "revoked",
            },
        )
        db.commit()
        return device


__all__ = [
    "DeviceNotFoundError",
    "DeviceService",
    "InvalidPairingTicketError",
    "IssuedDevice",
    "ListedDevice",
    "PairingTicket",
    "token_digest",
]
