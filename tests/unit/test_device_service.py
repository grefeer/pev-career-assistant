from __future__ import annotations

import hashlib

import fakeredis
import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db.base import Base
from backend.app.db.models import (
    AuditEvent,
    Device,
    DevicePlatform,
    DeviceStatus,
    User,
)
from backend.app.services.devices import (
    DeviceService,
    InvalidPairingTicketError,
    InvalidTaskLeaseError,
)


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
    assert (
        issued.device.token_hash
        == hashlib.sha256(issued.plaintext_token.encode()).hexdigest()
    )
    assert issued.plaintext_token not in issued.device.token_hash
    with pytest.raises(InvalidPairingTicketError):
        device_service.redeem_pairing_ticket(
            db,
            code=ticket.code,
            name="Replay",
            public_key_pem=VALID_TEST_PUBLIC_KEY,
        )


def test_pairing_ticket_audit_failure_removes_redis_ticket(
    device_service: DeviceService, db: Session, user: User
) -> None:
    def fail_ticket_audit(*_args) -> None:
        raise RuntimeError("audit insert failed")

    event.listen(AuditEvent, "before_insert", fail_ticket_audit)
    try:
        with pytest.raises(RuntimeError, match="audit insert failed"):
            device_service.create_pairing_ticket(db, user_id=user.id)
    finally:
        event.remove(AuditEvent, "before_insert", fail_ticket_audit)

    assert list(device_service.redis.scan_iter("pairing-ticket:*")) == []
    assert db.scalar(select(func.count()).select_from(AuditEvent)) == 0
    assert db.is_active


def test_redeem_audit_flush_failure_rolls_back_and_restores_ticket(
    device_service: DeviceService, db: Session, user: User
) -> None:
    ticket = device_service.create_pairing_ticket(db, user_id=user.id)
    key = f"pairing-ticket:{hashlib.sha256(ticket.code.encode()).hexdigest()}"
    original_ttl = device_service.redis.ttl(key)

    def fail_paired_audit(_mapper, _connection, target: AuditEvent) -> None:
        if target.event_type == "device.paired":
            raise RuntimeError("paired audit failed")

    event.listen(AuditEvent, "before_insert", fail_paired_audit)
    try:
        with pytest.raises(RuntimeError, match="paired audit failed"):
            device_service.redeem_pairing_ticket(
                db,
                code=ticket.code,
                name="Alice Windows",
                public_key_pem=VALID_TEST_PUBLIC_KEY,
            )
    finally:
        event.remove(AuditEvent, "before_insert", fail_paired_audit)

    restored_ttl = device_service.redis.ttl(key)
    assert 1 <= restored_ttl <= original_ttl
    assert db.scalar(select(func.count()).select_from(Device)) == 0
    assert db.scalar(select(func.count()).select_from(AuditEvent)) == 1
    assert db.is_active
    assert (
        device_service.redeem_pairing_ticket(
            db,
            code=ticket.code,
            name="Alice Windows retry",
            public_key_pem=VALID_TEST_PUBLIC_KEY,
        ).device.name
        == "Alice Windows retry"
    )


def test_redeem_explicit_commit_failure_restores_ticket_without_extending_ttl(
    device_service: DeviceService,
    db: Session,
    user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticket = device_service.create_pairing_ticket(db, user_id=user.id)
    key = f"pairing-ticket:{hashlib.sha256(ticket.code.encode()).hexdigest()}"
    original_ttl = device_service.redis.ttl(key)
    original_commit = db.commit
    attempts = 0

    def fail_once() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("commit failed before commit")
        original_commit()

    monkeypatch.setattr(db, "commit", fail_once)
    with pytest.raises(RuntimeError, match="commit failed before commit"):
        device_service.redeem_pairing_ticket(
            db,
            code=ticket.code,
            name="Alice Windows",
            public_key_pem=VALID_TEST_PUBLIC_KEY,
        )

    assert 1 <= device_service.redis.ttl(key) <= original_ttl
    assert db.scalar(select(func.count()).select_from(Device)) == 0
    assert (
        device_service.redeem_pairing_ticket(
            db,
            code=ticket.code,
            name="Retry",
            public_key_pem=VALID_TEST_PUBLIC_KEY,
        ).device.name
        == "Retry"
    )


def test_redeem_commit_ack_failure_returns_committed_device_without_restoring_ticket(
    device_service: DeviceService,
    db: Session,
    user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticket = device_service.create_pairing_ticket(db, user_id=user.id)
    key = f"pairing-ticket:{hashlib.sha256(ticket.code.encode()).hexdigest()}"
    original_commit = db.commit
    attempts = 0

    def commit_then_lose_ack() -> None:
        nonlocal attempts
        attempts += 1
        original_commit()
        if attempts == 1:
            raise RuntimeError("commit acknowledgement lost")

    monkeypatch.setattr(db, "commit", commit_then_lose_ack)
    issued = device_service.redeem_pairing_ticket(
        db,
        code=ticket.code,
        name="Committed Windows",
        public_key_pem=VALID_TEST_PUBLIC_KEY,
    )

    assert issued.device.name == "Committed Windows"
    assert device_service.redis.exists(key) == 0
    assert db.scalar(select(func.count()).select_from(Device)) == 1
    assert device_service.authenticate(db, issued.plaintext_token) is not None


def test_redeem_compensation_uses_nx_and_does_not_overwrite_new_value(
    device_service: DeviceService, db: Session, user: User
) -> None:
    ticket = device_service.create_pairing_ticket(db, user_id=user.id)
    key = f"pairing-ticket:{hashlib.sha256(ticket.code.encode()).hexdigest()}"

    def race_then_fail(_mapper, _connection, target: AuditEvent) -> None:
        if target.event_type == "device.paired":
            device_service.redis.set(key, b"new-owner-ticket", ex=60)
            raise RuntimeError("paired audit failed")

    event.listen(AuditEvent, "before_insert", race_then_fail)
    try:
        with pytest.raises(RuntimeError, match="paired audit failed"):
            device_service.redeem_pairing_ticket(
                db,
                code=ticket.code,
                name="Alice Windows",
                public_key_pem=VALID_TEST_PUBLIC_KEY,
            )
    finally:
        event.remove(AuditEvent, "before_insert", race_then_fail)

    assert device_service.redis.get(key) == b"new-owner-ticket"


def test_malformed_pairing_ticket_is_consumed_without_restoration(
    device_service: DeviceService, db: Session
) -> None:
    code = "malformed-code"
    key = f"pairing-ticket:{hashlib.sha256(code.encode()).hexdigest()}"
    device_service.redis.set(key, b"not-json", ex=600)

    with pytest.raises(InvalidPairingTicketError):
        device_service.redeem_pairing_ticket(
            db,
            code=code,
            name="Alice Windows",
            public_key_pem=VALID_TEST_PUBLIC_KEY,
        )

    assert device_service.redis.exists(key) == 0


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


def test_device_service_refuses_to_issue_unknown_or_submit_scope(
    device_service, db, issued_device
) -> None:
    from backend.app.db.models import ApplicationTask

    task = ApplicationTask(
        user_id=issued_device.device.user_id,
        target_job_id="simulation-job",
        device_id=issued_device.device.id,
    )
    db.add(task)
    db.commit()
    service = DeviceService(device_service.redis, lease_secret="x" * 32)

    with pytest.raises(InvalidTaskLeaseError, match="scope"):
        service.issue_task_lease(
            db,
            device=issued_device.device,
            task_id=task.id,
            scopes={"task:progress", "task:submit"},
        )
