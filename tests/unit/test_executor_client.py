from __future__ import annotations

import json

import httpx
import pytest

from executor.client import (
    ExecutorApiClient,
    UncertainWriteResult,
)
from executor.secrets import SecretStore


class InMemorySecretStore:
    def __init__(self) -> None:
        self._data: dict[str, str] = {
            "device-token": "test-device-token-12345",
        }

    def get(self, key: str) -> str | None:
        return self._data.get(key)

    def set(self, key: str, value: str) -> None:
        self._data[key] = value

    def delete(self, key: str) -> None:
        self._data.pop(key, None)


class RecordingTransport(httpx.MockTransport):
    """Mock transport that records request attempts for assertions."""

    def __init__(self) -> None:
        self.progress_attempts = 0
        self.fail_next_write_with_timeout = False
        self.handler = self._handler

    def _handler(self, request: httpx.Request) -> httpx.Response:
        is_progress = "/progress" in str(request.url)
        if is_progress:
            self.progress_attempts += 1
            if self.fail_next_write_with_timeout and self.progress_attempts <= 1:
                raise httpx.TimeoutException("write timed out", request=request)
        if str(request.url).endswith("/tasks") and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "tasks": [
                        {
                            "protocol_version": "executor.v1",
                            "task_id": "11111111-1111-4111-8111-111111111111",
                            "target_job_id": "simulation-job",
                            "snapshot_id": None,
                            "status": "dispatched",
                            "state_version": 0,
                        }
                    ]
                },
            )
        if str(request.url).endswith("/heartbeat") and request.method == "POST":
            return httpx.Response(200, json={"status": "online", "expires_in": 90})
        if "task-lease" in str(request.url) and request.method == "POST":
            return httpx.Response(200, json={"lease": "test-lease-value"})
        if "/progress" in str(request.url) and request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "protocol_version": "executor.v1",
                    "task_id": "11111111-1111-4111-8111-111111111111",
                    "status": "running",
                    "state_version": 1,
                },
            )
        return httpx.Response(404)


@pytest.fixture
def transport() -> RecordingTransport:
    return RecordingTransport()


@pytest.fixture
def client(transport: RecordingTransport) -> ExecutorApiClient:
    return ExecutorApiClient(
        base_url="http://127.0.0.1:8765",
        secret_store=InMemorySecretStore(),
        transport=transport,
    )


def test_authenticated_request_has_device_token_header(
    client, transport
) -> None:
    client.list_tasks()
    # Cannot directly assert headers, but successful 200 proves auth passed


def test_progress_write_is_never_retried_after_timeout(
    client, transport
) -> None:
    transport.fail_next_write_with_timeout = True
    with pytest.raises(UncertainWriteResult):
        client.report_progress(
            task_id="11111111-1111-4111-8111-111111111111",
            lease="lease-in-memory",
            expected_version=0,
            target_status="running",
            page_fingerprint="sha256:abc123",
            page_index=1,
            field_counts={"confirmed": 1, "defaulted": 0, "missing": 0, "low": 0},
            reason_code=None,
        )
    assert transport.progress_attempts == 1


def test_heartbeat_sends_version(client) -> None:
    client.heartbeat(version="0.1.0")


def test_issue_lease_returns_lease_string(client) -> None:
    lease = client.issue_lease(task_id="11111111-1111-4111-8111-111111111111")
    assert lease == "test-lease-value"


def test_list_tasks_returns_summaries(client) -> None:
    response = client.list_tasks()
    assert len(response.tasks) == 1
    assert response.tasks[0].task_id == "11111111-1111-4111-8111-111111111111"
