from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import uuid
import jwt
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
DEVICE_CREDENTIAL_TTL_DAYS = 90
TASK_LEASE_TTL_SECONDS = 300


class InvalidPairingTicketError(ValueError):
    pass


class DeviceNotFoundError(LookupError):
    pass


class PairingPersistenceUncertainError(RuntimeError):
    pass


class InvalidTaskLeaseError(ValueError):
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
    def __init__(self, redis_client: Any, *, lease_secret: str | None = None) -> None:
        self.redis = redis_client
        self.lease_secret = lease_secret

    def create_pairing_ticket(self, db: Session, *, user_id: str) -> PairingTicket:
        code = secrets.token_urlsafe(24)
        created_at = datetime.now(timezone.utc)
        value = json.dumps(
            {"user_id": user_id, "created_at": created_at.isoformat()},
            separators=(",", ":"),
        )
        key = f"pairing-ticket:{token_digest(code)}"
        self.redis.setex(key, PAIRING_TTL_SECONDS, value)
        try:
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
        except Exception:
            try:
                db.rollback()
            finally:
                self.redis.delete(key)
            raise
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
        key = f"pairing-ticket:{token_digest(code)}"
        raw = self.redis.getdel(key)
        if raw is None:
            raise InvalidPairingTicketError("invalid, expired, or used pairing ticket")
        try:
            ticket = json.loads(raw)
            user_id = ticket["user_id"]
            created_at = datetime.fromisoformat(ticket["created_at"])
            if not isinstance(user_id, str) or not user_id:
                raise ValueError
            if created_at.tzinfo is None:
                raise ValueError
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            raise InvalidPairingTicketError("invalid pairing ticket") from None

        plaintext_token = secrets.token_urlsafe(32)
        digest = token_digest(plaintext_token)
        device_id = str(uuid.uuid4())
        device = Device(
            id=device_id,
            user_id=user_id,
            name=name,
            platform=DevicePlatform.WINDOWS,
            status=DeviceStatus.ACTIVE,
            token_hash=digest,
            public_key_pem=public_key_pem,
            expires_at=utc_now() + timedelta(days=DEVICE_CREDENTIAL_TTL_DAYS),
        )
        try:
            db.add(device)
            db.flush()
            _audit(
                db,
                event_type="device.paired",
                entity_id=device.id,
                actor_user_id=user_id,
                payload={
                    "platform": DevicePlatform.WINDOWS.value,
                    "result": "paired",
                },
            )
            db.commit()
            return IssuedDevice(device=device, plaintext_token=plaintext_token)
        except Exception as persistence_error:
            try:
                db.rollback()
                by_id = devices.get_by_id(db, device_id)
                by_token = devices.get_by_token_hash(db, digest)
            except Exception as verification_error:
                raise PairingPersistenceUncertainError(
                    "unable to determine whether device pairing was committed"
                ) from verification_error

            if (
                by_id is not None
                and by_token is not None
                and by_id.id == by_token.id == device_id
                and hmac.compare_digest(by_id.token_hash, digest)
            ):
                return IssuedDevice(device=by_id, plaintext_token=plaintext_token)
            if by_id is not None or by_token is not None:
                raise PairingPersistenceUncertainError(
                    "device pairing persistence state is inconsistent"
                ) from persistence_error

            remaining_seconds = int(
                (
                    created_at
                    + timedelta(seconds=PAIRING_TTL_SECONDS)
                    - datetime.now(timezone.utc)
                ).total_seconds()
            )
            if remaining_seconds > 0:
                self.redis.set(
                    key,
                    raw,
                    ex=remaining_seconds,
                    nx=True,
                )
            raise

    def authenticate(self, db: Session, plaintext_token: str) -> Device | None:
        digest = token_digest(plaintext_token)
        device = devices.get_by_token_hash(db, digest)
        if device is None or device.status is not DeviceStatus.ACTIVE:
            return None
        expires_at = device.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= utc_now():
            return None
        if not hmac.compare_digest(device.token_hash, digest):
            return None
        return device

    def rotate_credential(
        self, db: Session, *, user_id: str, device_id: str
    ) -> IssuedDevice:
        device = devices.get_active_owned(db, user_id=user_id, device_id=device_id)
        if device is None:
            raise DeviceNotFoundError(device_id)
        plaintext = secrets.token_urlsafe(32)
        device.token_hash = token_digest(plaintext)
        device.credential_rotated_at = utc_now()
        device.expires_at = utc_now() + timedelta(days=DEVICE_CREDENTIAL_TTL_DAYS)
        self.redis.delete(f"device-online:{device.id}")
        _audit(
            db,
            event_type="device.credential_rotated",
            entity_id=device.id,
            actor_user_id=user_id,
            payload={"result": "rotated"},
        )
        db.commit()
        return IssuedDevice(device=device, plaintext_token=plaintext)

    def issue_task_lease(
        self, db: Session, *, device: Device, task_id: str, scopes: set[str]
    ) -> str:
        if not self.lease_secret:
            raise InvalidTaskLeaseError("task lease signing is not configured")
        from backend.app.db.models import ApplicationTask

        task = db.get(ApplicationTask, task_id)
        if (
            task is None
            or task.device_id != device.id
            or task.user_id != device.user_id
        ):
            raise InvalidTaskLeaseError("task is not assigned to this device")
        now = utc_now()
        claims = {
            "sub": device.id,
            "user_id": device.user_id,
            "task_id": task.id,
            "scope": sorted(scopes),
            "iat": now,
            "exp": now + timedelta(seconds=TASK_LEASE_TTL_SECONDS),
        }
        return jwt.encode(claims, self.lease_secret, algorithm="HS256")

    def verify_task_lease(
        self,
        db: Session,
        lease: str,
        *,
        device: Device,
        task_id: str,
        required_scope: str,
    ) -> dict[str, Any]:
        if not self.lease_secret:
            raise InvalidTaskLeaseError("task lease signing is not configured")
        try:
            claims = jwt.decode(
                lease,
                self.lease_secret,
                algorithms=["HS256"],
                options={"require": ["sub", "user_id", "task_id", "scope", "exp"]},
            )
        except jwt.PyJWTError as exc:
            raise InvalidTaskLeaseError("invalid or expired task lease") from exc
        from backend.app.db.models import ApplicationTask

        task = db.get(ApplicationTask, task_id)
        if (
            claims.get("sub") != device.id
            or claims.get("user_id") != device.user_id
            or claims.get("task_id") != task_id
            or required_scope not in claims.get("scope", [])
            or task is None
            or task.device_id != device.id
            or task.user_id != device.user_id
        ):
            raise InvalidTaskLeaseError("task lease scope or binding is invalid")
        return claims

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
    "InvalidTaskLeaseError",
    "IssuedDevice",
    "ListedDevice",
    "PairingTicket",
    "PairingPersistenceUncertainError",
    "token_digest",
]
