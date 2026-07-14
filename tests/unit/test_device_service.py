from __future__ import annotations

import hashlib

import fakeredis
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db.base import Base
from backend.app.db.models import AuditEvent, DevicePlatform, DeviceStatus, User
from backend.app.services.devices import DeviceService, InvalidPairingTicketError


VALID_TEST_PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
test-windows-device-key
-----END PUBLIC KEY-----"""


@pytest.fixture
def db() -> Session:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        yield session


@pytest.fixture
def user(db: Session) -> User:
    value = User(
        account="alice", nickname="Alice", password_hash="hash", is_active=True
    )
    db.add(value)
    db.commit()
    return value


@pytest.fixture
def device_service() -> DeviceService:
    return DeviceService(fakeredis.FakeRedis())


@pytest.fixture
def issued_device(device_service: DeviceService, db: Session, user: User):
    ticket = device_service.create_pairing_ticket(db, user_id=user.id)
    return device_service.redeem_pairing_ticket(
        db,
        code=ticket.code,
        name="Alice Windows",
        public_key_pem=VALID_TEST_PUBLIC_KEY,
    )


def test_pairing_ticket_can_only_be_redeemed_once(
    device_service: DeviceService, db: Session, user: User
) -> None:
    ticket = device_service.create_pairing_ticket(db, user_id=user.id)
    key = f"pairing-ticket:{hashlib.sha256(ticket.code.encode()).hexdigest()}"
    assert 599 <= device_service.redis.ttl(key) <= 600

    issued = device_service.redeem_pairing_ticket(
        db,
        code=ticket.code,
        name="Alice Windows",
        public_key_pem=VALID_TEST_PUBLIC_KEY,
    )

    assert issued.device.platform is DevicePlatform.WINDOWS
    assert len(issued.plaintext_token) >= 43
    assert issued.device.token_hash == hashlib.sha256(
        issued.plaintext_token.encode()
    ).hexdigest()
    assert issued.plaintext_token not in issued.device.token_hash
    with pytest.raises(InvalidPairingTicketError):
        device_service.redeem_pairing_ticket(
            db,
            code=ticket.code,
            name="Replay",
            public_key_pem=VALID_TEST_PUBLIC_KEY,
        )


def test_pairing_ticket_service_interface_does_not_require_db(
    device_service: DeviceService, user: User
) -> None:
    ticket = device_service.create_pairing_ticket(user_id=user.id)

    key = f"pairing-ticket:{hashlib.sha256(ticket.code.encode()).hexdigest()}"
    assert device_service.redis.exists(key) == 1


def test_revoked_device_token_no_longer_authenticates(
    device_service: DeviceService, db: Session, issued_device
) -> None:
    assert (
        device_service.authenticate(db, issued_device.plaintext_token).id
        == issued_device.device.id
    )
    device_service.revoke(
        db,
        user_id=issued_device.device.user_id,
        device_id=issued_device.device.id,
    )
    assert issued_device.device.status is DeviceStatus.REVOKED
    assert device_service.authenticate(db, issued_device.plaintext_token) is None


def test_heartbeat_sets_short_online_ttl(
    device_service: DeviceService, db: Session, issued_device
) -> None:
    device = device_service.heartbeat(
        db, issued_device.plaintext_token, version="0.1.0"
    )
    key = f"device-online:{issued_device.device.id}"

    assert device_service.redis.get(key) == b"1"
    assert 1 <= device_service.redis.ttl(key) <= 90
    assert device.last_seen_at is not None
    assert device.version == "0.1.0"


def test_list_online_state_is_redis_only_and_does_not_change_status(
    device_service: DeviceService, db: Session, issued_device
) -> None:
    device_service.heartbeat(db, issued_device.plaintext_token, version="0.1.0")
    assert device_service.list_for_user(db, issued_device.device.user_id)[0].online

    device_service.redis.flushall()
    listed = device_service.list_for_user(db, issued_device.device.user_id)[0]

    assert listed.online is False
    assert listed.device.status is DeviceStatus.ACTIVE


def test_device_audit_payloads_never_contain_credentials_or_public_key(
    device_service: DeviceService, db: Session, user: User
) -> None:
    ticket = device_service.create_pairing_ticket(db, user_id=user.id)
    issued = device_service.redeem_pairing_ticket(
        db,
        code=ticket.code,
        name="Alice Windows",
        public_key_pem=VALID_TEST_PUBLIC_KEY,
    )
    device_service.revoke(db, user_id=user.id, device_id=issued.device.id)

    events = list(db.scalars(select(AuditEvent).order_by(AuditEvent.id)))
    assert [event.event_type for event in events] == [
        "device.pairing_ticket_created",
        "device.paired",
        "device.revoked",
    ]
    serialized = repr([event.redacted_payload for event in events])
    assert ticket.code not in serialized
    assert issued.plaintext_token not in serialized
    assert VALID_TEST_PUBLIC_KEY not in serialized
    assert all(
        set(event.redacted_payload) <= {"platform", "version", "result"}
        for event in events
    )
