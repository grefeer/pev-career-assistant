from __future__ import annotations

from collections.abc import Iterator
import os

import fakeredis
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.api import dependencies
from backend.app.config import Settings
from backend.app.db.base import Base

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("APP_AUTH_SECRET", "test-secret-with-at-least-32-characters")
os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")
os.environ.setdefault(
    "OBJECT_ENCRYPTION_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
)

from backend.app.main import create_app


PUBLIC_KEY = "-----BEGIN PUBLIC KEY-----\nwindows-test\n-----END PUBLIC KEY-----"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        app_env="test",
        app_auth_secret="test-secret-with-at-least-32-characters",
        object_encryption_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        database_url="sqlite+pysqlite:///:memory:",
        redis_url="redis://localhost:6379/15",
        checkpoint_backend="sqlite",
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    redis = fakeredis.FakeRedis()

    def override_db() -> Iterator[Session]:
        with session_factory() as db:
            yield db

    app = create_app(settings)
    app.dependency_overrides[dependencies._get_db] = override_db
    app.dependency_overrides[dependencies.get_redis] = lambda: redis
    with TestClient(app) as value:
        yield value


def register(client: TestClient, account: str) -> str:
    response = client.post(
        "/api/auth/register",
        json={"account": account, "nickname": account, "password": "secret12"},
    )
    assert response.status_code == 200
    return response.json()["token"]


def pair(client: TestClient, user_token: str) -> dict[str, object]:
    ticket = client.post(
        "/api/devices/pairing-tickets",
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert ticket.status_code == 200
    assert set(ticket.json()) == {"code", "expires_at"}
    response = client.post(
        "/api/devices/pair",
        json={
            "code": ticket.json()["code"],
            "name": "Alice Windows",
            "public_key_pem": PUBLIC_KEY,
        },
    )
    assert response.status_code == 200
    return response.json()


def test_pairing_is_one_time_and_device_token_only_appears_in_pair_response(
    client: TestClient,
) -> None:
    user_token = register(client, "alice")
    ticket = client.post(
        "/api/devices/pairing-tickets",
        headers={"Authorization": f"Bearer {user_token}"},
    ).json()
    payload = {
        "code": ticket["code"],
        "name": "Alice Windows",
        "public_key_pem": PUBLIC_KEY,
    }

    paired = client.post("/api/devices/pair", json=payload)
    replay = client.post("/api/devices/pair", json=payload)
    listed = client.get(
        "/api/devices", headers={"Authorization": f"Bearer {user_token}"}
    )

    assert paired.status_code == 200
    assert set(paired.json()) == {"device", "device_token"}
    assert replay.status_code == 400
    assert listed.status_code == 200
    assert set(listed.json()) == {"devices"}
    assert "device_token" not in repr(listed.json())
    assert "token_hash" not in repr(listed.json())
    assert "public_key_pem" not in repr(listed.json())


def test_device_auth_heartbeat_and_revoke_are_immediate(client: TestClient) -> None:
    user_token = register(client, "alice")
    issued = pair(client, user_token)
    token = issued["device_token"]
    device_id = issued["device"]["id"]
    device_headers = {"X-Device-Token": token}

    assert client.get("/api/devices/me", headers=device_headers).status_code == 200
    heartbeat = client.post(
        "/api/devices/heartbeat",
        headers=device_headers,
        json={"version": "0.1.0"},
    )
    assert heartbeat.status_code == 200
    assert heartbeat.json() == {"status": "online", "expires_in": 90}
    assert client.delete(
        f"/api/devices/{device_id}",
        headers={"Authorization": f"Bearer {user_token}"},
    ).status_code == 204
    assert client.get("/api/devices/me", headers=device_headers).status_code == 401
    assert (
        client.post(
            "/api/devices/heartbeat",
            headers=device_headers,
            json={"version": "0.1.1"},
        ).status_code
        == 401
    )


def test_device_owner_isolation_returns_404(client: TestClient) -> None:
    alice_token = register(client, "alice")
    bob_token = register(client, "bobby")
    issued = pair(client, alice_token)

    response = client.delete(
        f"/api/devices/{issued['device']['id']}",
        headers={"Authorization": f"Bearer {bob_token}"},
    )

    assert response.status_code == 404


@pytest.mark.parametrize("header", [{}, {"X-Device-Token": "invalid"}])
def test_invalid_device_token_returns_401(
    client: TestClient, header: dict[str, str]
) -> None:
    assert client.get("/api/devices/me", headers=header).status_code == 401


def test_openapi_only_exposes_device_token_on_pair_response(
    client: TestClient,
) -> None:
    schema = client.get("/openapi.json").json()
    schemas = schema["components"]["schemas"]

    assert set(schemas["PairingTicketResponse"]["properties"]) == {
        "code",
        "expires_at",
    }
    assert set(schemas["DeviceSummary"]["properties"]) == {
        "id",
        "name",
        "platform",
        "status",
        "version",
        "paired_at",
        "last_seen_at",
        "online",
    }
    assert set(schemas["PairDeviceResponse"]["properties"]) == {
        "device",
        "device_token",
    }
    assert set(schemas["DeviceListResponse"]["properties"]) == {"devices"}
    assert set(schemas["HeartbeatResponse"]["properties"]) == {
        "status",
        "expires_in",
    }
    assert "device_token" not in repr(schemas["DeviceSummary"])
    assert "device_token" not in repr(schemas["DeviceListResponse"])
    assert "token_hash" not in repr(schemas)
    assert "public_key_pem" not in repr(schemas["DeviceSummary"])

    paths = schema["paths"]
    assert paths["/api/devices/pair"]["post"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["$ref"].endswith("/PairDeviceResponse")
    assert paths["/api/devices"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["$ref"].endswith("/DeviceListResponse")
    assert paths["/api/devices/me"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["$ref"].endswith("/DeviceSummary")
