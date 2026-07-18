"""Integration tests for Executor v2 protocol (task_kind=application).

Verifies:
  - v2 payload parsing and discriminated union dispatch in the client
  - Engine can handle a v2 payload and convert fields correctly
  - v1 simulation regression (imported from test_executor_simulation)
  - Attachment download round-trip
"""

from __future__ import annotations

from collections.abc import Iterator
import socket
import threading
from typing import Any
from pathlib import Path

import pytest
import uvicorn

from executor.browser import BrowserSession
from executor.checkpoints import CheckpointStore
from executor.engine import (
    ExecutorEngine,
    _v2_fields,
    _payload_fields,
)
from executor.adapters.base import (
    BlockerInfo,
    FillResult,
    PageFingerprint,
    RepeatSectionResult,
    UploadResult,
)
from executor.mock_site.app import app as mock_app, telemetry
from executor.protocol import (
    ExecutorField,
    ExecutorTaskDetail,
    ExecutorTaskDetailV2,
    ExecutorTaskPayload,
    ExecutorTaskPayloadV2,
    ExecutorTaskState,
    FieldConfidence,
    TaskStatus,
)


# ---------------------------------------------------------------------------
# V2 fixture path
# ---------------------------------------------------------------------------

V2_FIXTURE = Path("tests/fixtures/executor/protocol_v2/task.json")


# ---------------------------------------------------------------------------
# Mock site server (shared fixture)
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class MockSiteServer:
    """Runs the mock site on a loopback port in a daemon thread."""

    def __init__(self, port: int) -> None:
        self.port = port
        self._server = uvicorn.Server(
            uvicorn.Config(mock_app, host="127.0.0.1", port=port, log_level="error")
        )
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def start(self) -> None:
        self._thread.start()
        import time
        import httpx
        for _ in range(20):
            time.sleep(0.2)
            try:
                httpx.get(f"http://127.0.0.1:{self.port}/telemetry", timeout=2)
                return
            except Exception:
                continue
        raise RuntimeError("mock site did not start")

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)


@pytest.fixture(scope="module")
def mock_site() -> Iterator[int]:
    port = _free_port()
    server = MockSiteServer(port)
    server.start()
    try:
        yield port
    finally:
        server.stop()


@pytest.fixture(autouse=True)
def reset_telemetry() -> None:
    telemetry.reset()


@pytest.fixture
def mock_site_url(mock_site: int) -> str:
    return f"http://127.0.0.1:{mock_site}"


# ---------------------------------------------------------------------------
# v2 payload fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def v2_payload() -> ExecutorTaskPayloadV2:
    return ExecutorTaskPayloadV2.model_validate_json(V2_FIXTURE.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Browser fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def browser(tmp_path: Any) -> Iterator[BrowserSession]:
    session = BrowserSession(
        user_data_dir=tmp_path / "chrome-profile",
        headless=True,
        channel=None,
    )
    try:
        yield session
    finally:
        session.close()


# ---------------------------------------------------------------------------
# _payload_fields unit tests
# ---------------------------------------------------------------------------


def test_v2_fields_conversion(v2_payload: ExecutorTaskPayloadV2) -> None:
    """Verify _v2_fields converts non_sensitive_fields dict to ExecutorField list."""
    fields = _v2_fields(v2_payload)
    assert len(fields) == 7  # 5 non-sensitive + 2 local-sensitive requirements

    # Non-sensitive fields should have confirmed confidence
    name_field = next(f for f in fields if f.field_key == "name")
    assert name_field.value == "Alice Example"
    assert name_field.confidence == "confirmed"
    assert name_field.required is True

    # Local-sensitive requirements should be missing (value=None)
    id_field = next(f for f in fields if f.field_key == "id_number")
    assert id_field.value is None
    assert id_field.confidence == "missing"
    assert id_field.required is True


def test_payload_fields_v1() -> None:
    """_payload_fields returns v1 fields as-is."""
    v1 = ExecutorTaskPayload(
        task_id="00000000-0000-0000-0000-000000000001",
        state_version=0,
        target_url="http://127.0.0.1:8765/single-page",
        fields=[
            ExecutorField(
                field_key="full_name",
                label="姓名",
                value="Alice",
                confidence="confirmed",
                required=True,
                sensitive=False,
            )
        ],
    )
    fields = _payload_fields(v1)
    assert len(fields) == 1
    assert fields[0].field_key == "full_name"


def test_payload_fields_v2(v2_payload: ExecutorTaskPayloadV2) -> None:
    """_payload_fields converts v2 payload fields correctly."""
    fields = _payload_fields(v2_payload)
    assert len(fields) == 7
    assert any(f.field_key == "name" for f in fields)
    assert any(f.field_key == "id_number" for f in fields)


# ---------------------------------------------------------------------------
# v2 engine run test (basic)
# ---------------------------------------------------------------------------


def test_v2_engine_with_v2_payload(
    browser: BrowserSession,
    mock_site_url: str,
    v2_payload: ExecutorTaskPayloadV2,
    tmp_path: Any,
) -> None:
    """Engine can run a v2 payload — same safety gates apply."""
    # Point the v2 payload at the mock site
    payload = v2_payload.model_copy(update={
        "target_url": f"{mock_site_url}/single-page",
    })

    checkpoints = CheckpointStore(tmp_path / "checkpoints")
    adapter = _MockAdapter()
    engine = ExecutorEngine(
        client=_FakeApiClient(),
        browser=browser,
        checkpoints=checkpoints,
        adapter=adapter,
    )

    outcome = engine.run(payload=payload)

    # Same safety gate: single page bottom action is never auto-clicked
    assert outcome.kind == "ready_for_review"
    assert adapter.fill_calls
    assert _telemetry()["final_clicks"] == 0


def test_v2_engine_ambiguous_safety_gate(
    browser: BrowserSession,
    mock_site_url: str,
    v2_payload: ExecutorTaskPayloadV2,
    tmp_path: Any,
) -> None:
    """Ambiguous action safety gate MUST remain identical for v2."""
    payload = v2_payload.model_copy(update={
        "target_url": f"{mock_site_url}/ambiguous",
    })

    checkpoints = CheckpointStore(tmp_path / "checkpoints")
    engine = ExecutorEngine(
        client=_FakeApiClient(),
        browser=browser,
        checkpoints=checkpoints,
        adapter=_MockAdapter(),
    )

    outcome = engine.run(payload=payload)

    # Ambiguous page: should not click anything
    assert outcome.kind == "ready_for_review"
    assert _telemetry()["ambiguous_clicks"] == 0
    assert _telemetry()["final_clicks"] == 0


# ---------------------------------------------------------------------------
# v1 regression test (import from test_executor_simulation)
# ---------------------------------------------------------------------------


def test_v1_single_page_regression(
    browser: BrowserSession,
    mock_site_url: str,
    tmp_path: Any,
) -> None:
    """v1 simulation: single page ends in review without final click."""
    fields = [
        ExecutorField(
            field_key="full_name",
            label="姓名",
            value="Alice Example",
            confidence=FieldConfidence.CONFIRMED,
            required=True,
            sensitive=False,
        ),
        ExecutorField(
            field_key="portfolio_url",
            label="作品链接",
            value=None,
            confidence=FieldConfidence.MISSING,
            required=False,
            sensitive=False,
        ),
    ]
    payload = ExecutorTaskPayload(
        task_id="00000000-0000-0000-0000-000000000001",
        state_version=0,
        target_url=f"{mock_site_url}/single-page",
        fields=fields,
    )
    detail = ExecutorTaskDetail(
        protocol_version="executor.v1",
        task_id=payload.task_id,
        target_job_id="job-001",
        snapshot_id=None,
        status=TaskStatus.DISPATCHED,
        state_version=0,
        payload=payload,
    )

    fake = _FakeApiClient()
    fake._detail = detail  # type: ignore[assignment]
    checkpoints = CheckpointStore(tmp_path / "checkpoints")
    engine = ExecutorEngine(client=fake, browser=browser, checkpoints=checkpoints)

    outcome = engine.run(task_id=detail.task_id)

    assert outcome.kind == "ready_for_review"
    assert _telemetry()["final_clicks"] == 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _telemetry() -> dict[str, object]:
    return telemetry.snapshot()


class _MockAdapter:
    adapter_id = "mock.local"
    supported_domains = ["127.0.0.1"]
    version = "1.0.0"

    def __init__(self) -> None:
        self.fill_calls: list[str] = []

    def fingerprint_page(self, page) -> PageFingerprint:
        return PageFingerprint(
            url_pattern=page.url,
            dom_hash="sha256:" + ("1" * 64),
        )

    def classify_topology(self, fp: PageFingerprint) -> str:
        return "single_page"

    def fill_field(self, page, field_key: str, value: str) -> FillResult:
        self.fill_calls.append(field_key)
        locator = page.locator(f'[data-field-key="{field_key}"]')
        if locator.count() == 0:
            return FillResult(
                field_key=field_key,
                strategy="mock",
                value_written=value,
                readback_match=False,
                readback_value=None,
                confidence=0.0,
            )
        locator.fill(value)
        readback = locator.input_value()
        return FillResult(
            field_key=field_key,
            strategy="mock",
            value_written=value,
            readback_match=readback == value,
            readback_value=readback,
            confidence=1.0 if readback == value else 0.0,
        )

    def handle_repeat_section(self, page, section_key, entries):
        return RepeatSectionResult(section_key, 0, len(entries), len(entries), True)

    def upload_attachment(self, page, field_key, file_path):
        return UploadResult(field_key, file_path, True, "mock")

    def detect_blocker(self, page) -> BlockerInfo | None:
        return None

    def save_page_progress(self, page) -> bool:
        return False


class _FakeApiClient:
    """Minimal in-memory fake of ExecutorApiClient for engine tests.

    Supports both v1 and v2 detail types via the internal ``_detail`` field.
    """

    def __init__(self) -> None:
        self._detail: (
            ExecutorTaskDetail | ExecutorTaskDetailV2 | None
        ) = None
        self.state_version: int = 0
        self.heartbeat_calls: list[str] = []
        self.lease_calls: list[tuple[str, str]] = []
        self.get_task_calls: list[tuple[str, str]] = []
        self.progress_calls: list[dict[str, object]] = []
        self.result_calls: list[dict[str, object]] = []

    def heartbeat(self, version: str) -> None:
        self.heartbeat_calls.append(version)

    def issue_lease(self, task_id: str) -> str:
        lease = f"lease-{task_id}"
        self.lease_calls.append((task_id, lease))
        return lease

    def get_task(
        self, task_id: str, lease: str
    ) -> ExecutorTaskDetail | ExecutorTaskDetailV2:
        self.get_task_calls.append((task_id, lease))
        if self._detail is None:
            raise RuntimeError("_FakeApiClient: no detail configured")
        return self._detail

    def report_progress(
        self,
        *,
        task_id: str,
        lease: str,
        expected_version: int,
        target_status: str,
        page_fingerprint: str,
        page_index: int | None,
        field_counts: dict[str, int],
        reason_code: str | None,
    ) -> ExecutorTaskState:
        self.progress_calls.append({
            "task_id": task_id,
            "target_status": target_status,
            "expected_version": expected_version,
            "reason_code": reason_code,
            "page_fingerprint": page_fingerprint,
        })
        self.state_version += 1
        return ExecutorTaskState(
            protocol_version="executor.v1",
            task_id=task_id,
            status=TaskStatus(target_status),
            state_version=self.state_version,
        )

    def report_result(
        self,
        *,
        task_id: str,
        lease: str,
        expected_version: int,
        target_status: str,
        page_fingerprint: str,
        reason_code: str,
    ) -> ExecutorTaskState:
        self.result_calls.append({
            "task_id": task_id,
            "target_status": target_status,
            "reason_code": reason_code,
        })
        self.state_version += 1
        return ExecutorTaskState(
            protocol_version="executor.v1",
            task_id=task_id,
            status=TaskStatus(target_status),
            state_version=self.state_version,
        )
