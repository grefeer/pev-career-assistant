from datetime import timedelta

import fakeredis
import pytest
from botocore.exceptions import ClientError
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.config import Settings
from backend.app.db.base import Base, utc_now
from backend.app.db.models import (
    ApplicationTask,
    Device,
    DevicePlatform,
    DeviceStatus,
    User,
)
from backend.app.services.applications import (
    UnsafeAuditPayloadError,
    _validate_redacted_value,
)
from backend.app.services.devices import DeviceService, InvalidTaskLeaseError
from backend.app.services.storage import S3BlobStore
from backend.app.services.rate_limit import (
    RateLimitExceededError,
    RateLimitUnavailableError,
    RedisFixedWindowRateLimiter,
)


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session


@pytest.mark.parametrize("key", ["invalid", "QUFBQQ=="])
def test_settings_rejects_invalid_encryption_key(key: str) -> None:
    with pytest.raises(ValidationError, match="OBJECT_ENCRYPTION_KEY"):
        Settings(
            app_env="test",
            app_auth_secret="x" * 32,
            database_url="sqlite+pysqlite:///:memory:",
            redis_url="redis://localhost/0",
            object_encryption_key=key,
        )


def test_production_rejects_default_object_credentials() -> None:
    with pytest.raises(ValidationError, match="OBJECT_STORE"):
        Settings(
            app_env="production",
            app_auth_secret="x" * 32,
            database_url="mysql+pymysql://root@mysql/db",
            redis_url="redis://redis/0",
            checkpoint_backend="redis",
            object_encryption_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"access_token": "x"},
        {"nested": [{"device-token": "x"}]},
        {"authorizationHeader": "x"},
        {"metadata": {"sessionCookie": "x"}},
        {"api_credential": "x"},
    ],
)
def test_audit_rejects_compound_sensitive_keys(payload) -> None:
    with pytest.raises(UnsafeAuditPayloadError):
        _validate_redacted_value(payload)


def _error(code: str, status: int) -> ClientError:
    return ClientError(
        {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}},
        "HeadBucket",
    )


def test_s3_region_and_create_race_are_handled() -> None:
    class Client:
        heads = 0
        create_args = None

        def head_bucket(self, **kwargs):
            self.heads += 1
            if self.heads == 1:
                raise _error("404", 404)

        def create_bucket(self, **kwargs):
            self.create_args = kwargs
            raise _error("BucketAlreadyOwnedByYou", 409)

    client = Client()
    S3BlobStore(client, "bucket", region="ap-southeast-1").ensure_bucket()
    assert client.create_args["CreateBucketConfiguration"] == {
        "LocationConstraint": "ap-southeast-1"
    }
    assert client.heads == 2


def test_s3_permission_error_is_not_swallowed() -> None:
    class Client:
        def head_bucket(self, **kwargs):
            raise _error("AccessDenied", 403)

    with pytest.raises(ClientError):
        S3BlobStore(Client(), "bucket").ensure_bucket()


def test_device_expiry_rotation_and_scoped_task_lease(db) -> None:
    user = User(account="a", nickname="A", password_hash="h")
    db.add(user)
    db.flush()
    device = Device(
        user_id=user.id,
        name="d",
        platform=DevicePlatform.WINDOWS,
        status=DeviceStatus.ACTIVE,
        token_hash="old",
        public_key_pem="p",
        expires_at=utc_now() - timedelta(seconds=1),
    )
    db.add(device)
    db.commit()
    service = DeviceService(fakeredis.FakeRedis(), lease_secret="x" * 32)
    assert service.authenticate(db, "old") is None
    device.expires_at = utc_now() + timedelta(days=1)
    db.commit()
    rotated = service.rotate_credential(db, user_id=user.id, device_id=device.id)
    assert service.authenticate(db, "old") is None
    authenticated = service.authenticate(db, rotated.plaintext_token)
    task = ApplicationTask(user_id=user.id, target_job_id="j", device_id=device.id)
    db.add(task)
    db.commit()
    lease = service.issue_task_lease(
        db, device=authenticated, task_id=task.id, scopes={"task:event"}
    )
    assert (
        service.verify_task_lease(
            db,
            lease,
            device=authenticated,
            task_id=task.id,
            required_scope="task:event",
        )["sub"]
        == device.id
    )
    with pytest.raises(InvalidTaskLeaseError):
        service.verify_task_lease(
            db,
            lease,
            device=authenticated,
            task_id="other",
            required_scope="task:event",
        )
    with pytest.raises(InvalidTaskLeaseError):
        service.verify_task_lease(
            db,
            lease,
            device=authenticated,
            task_id=task.id,
            required_scope="task:submit",
        )


def test_auth_rate_limit_is_separate_per_action_and_fails_closed() -> None:
    redis_client = fakeredis.FakeRedis()
    limiter = RedisFixedWindowRateLimiter(redis_client, limit=1)
    limiter.check(action="login", identity="127.0.0.1")
    limiter.check(action="register", identity="127.0.0.1")
    with pytest.raises(RateLimitExceededError):
        limiter.check(action="login", identity="127.0.0.1")

    class BrokenRedis:
        def pipeline(self, **kwargs):
            raise RuntimeError("down")

    with pytest.raises(RateLimitUnavailableError):
        RedisFixedWindowRateLimiter(BrokenRedis()).check(action="login", identity="x")
