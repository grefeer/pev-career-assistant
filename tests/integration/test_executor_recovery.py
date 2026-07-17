from __future__ import annotations

from collections.abc import Iterator
import socket
import threading
from typing import Any

import pytest
import uvicorn

from executor.browser import BrowserSession
from executor.checkpoints import CheckpointStore, ExecutorCheckpoint
from executor.client import ApiUnauthorized, ApiConflict, UncertainWriteResult
from executor.engine import ExecutorEngine, FaultPoint, InjectedCrash
from executor.mock_site.app import app as mock_app, telemetry
from executor.protocol import (
    ExecutorField,
    ExecutorTaskDetail,
    ExecutorTaskPayload,
    ExecutorTaskState,
    FieldConfidence,
    PROTOCOL_VERSION,
    TaskStatus,
)


# ---------------------------------------------------------------------------
# Shared mock-site server (module-scoped, same design as simulation tests)
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
# Browser session (per-test, headless Chromium)
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
# Helpers
# ---------------------------------------------------------------------------


PAYLOAD_FIELDS: list[ExecutorField] = [
    ExecutorField(
        field_key="full_name",
        label="姓名",
        value="Alice Example",
        confidence=FieldConfidence.CONFIRMED,
        required=True,
        sensitive=False,
    ),
]


def _telemetry() -> dict[str, object]:
    return telemetry.snapshot()


class _RecoveryFakeApiClient:
    """Fake API client for recovery tests with configurable fault injection."""

    def __init__(self, detail: ExecutorTaskDetail) -> None:
        self._detail = detail
        self.state_version: int = 0
        self.progress_attempts: int = 0
        self.progress_calls: list[str] = []
        self.result_calls: list[str] = []
        self.result_error: Exception | None = None
        self.lease_denied: bool = False
        self.detail_denied: bool = False
        self.detail_attempts: int = 0
        self.detail_error_on_attempt: dict[int, Exception] = {}
        self.progress_conflict: bool = False
        self.progress_error_on_attempt: dict[int, Exception] = {}

    def heartbeat(self, version: str) -> None:
        pass

    def issue_lease(self, task_id: str) -> str:
        if self.lease_denied:
            raise ApiUnauthorized("lease denied")
        return f"lease-{task_id}"

    def get_task(self, task_id: str, lease: str) -> ExecutorTaskDetail:
        self.detail_attempts += 1
        if error := self.detail_error_on_attempt.get(self.detail_attempts):
            raise error
        if self.detail_denied:
            raise ApiUnauthorized("detail denied")
        payload = self._detail.payload.model_copy(
            update={"state_version": self.state_version}
        )
        return self._detail.model_copy(
            update={"state_version": self.state_version, "payload": payload}
        )

    def report_progress(
        self,
        *,
        task_id: str,
        lease: str,
        expected_version: int,
        target_status: str,
        **kwargs: object,
    ) -> ExecutorTaskState:
        self.progress_attempts += 1
        if error := self.progress_error_on_attempt.get(self.progress_attempts):
            raise error
        if self.progress_conflict:
            raise ApiConflict("stale_task_version")
        self.state_version += 1
        self.progress_calls.append(target_status)
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
        target_status: str,
        **kwargs: object,
    ) -> ExecutorTaskState:
        if self.result_error is not None:
            raise self.result_error
        self.state_version += 1
        self.result_calls.append(target_status)
        return ExecutorTaskState(
            protocol_version="executor.v1",
            task_id=task_id,
            status=TaskStatus(target_status),
            state_version=self.state_version,
        )


def _make_detail(
    url: str,
    fields: list[ExecutorField],
    *,
    status: TaskStatus = TaskStatus.DISPATCHED,
) -> ExecutorTaskDetail:
    """Build an ExecutorTaskDetail pointing at ``url``."""
    payload = ExecutorTaskPayload(
        task_id="00000000-0000-0000-0000-000000000001",
        state_version=0,
        target_url=url,
        fields=fields,
    )
    return ExecutorTaskDetail(
        protocol_version="executor.v1",
        task_id=payload.task_id,
        target_job_id="job-001",
        snapshot_id=None,
        status=status,
        state_version=0,
        payload=payload,
    )


# ---------------------------------------------------------------------------
# Process restart recovery
# ---------------------------------------------------------------------------


def test_process_restart_after_field_write_does_not_duplicate_fill(
    browser: BrowserSession,
    mock_site_url: str,
    tmp_path: Any,
) -> None:
    """Crash after field checkpoint: recovery filters completed fields."""
    url = f"{mock_site_url}/single-page"
    detail = _make_detail(url, PAYLOAD_FIELDS)
    fake = _RecoveryFakeApiClient(detail)
    cp_dir = tmp_path / "checkpoints"

    # First run crashes after fill + checkpoint save
    checkpoints = CheckpointStore(cp_dir)
    engine = ExecutorEngine(
        client=fake,
        browser=browser,
        checkpoints=checkpoints,
        fault_point=FaultPoint.AFTER_FIELD_CHECKPOINT_SAVED,
    )
    with pytest.raises(InjectedCrash):
        engine.run(task_id=detail.task_id)

    # Verify checkpoint captured the completed field
    saved = checkpoints.load(detail.task_id)
    assert saved is not None
    assert "full_name" in saved.completed_field_keys

    # Second run — no fault injection, same checkpoint store
    engine2 = ExecutorEngine(
        client=_RecoveryFakeApiClient(detail),
        browser=browser,
        checkpoints=CheckpointStore(cp_dir),
    )
    outcome = engine2.run(task_id=detail.task_id)

    assert outcome.kind == "ready_for_review"
    assert outcome.reason_code == "single_page_bottom_action"
    # The field was filled exactly once (first run).  Second run's checkpoint
    # filtering prevented re-fill even on a fresh page load.
    assert _telemetry()["field_events"].get("full_name", 0) == 1
    assert _telemetry()["final_clicks"] == 0


def test_crash_between_field_write_and_verified_checkpoint_never_refills(
    browser: BrowserSession,
    mock_site_url: str,
    tmp_path: Any,
) -> None:
    detail = _make_detail(f"{mock_site_url}/single-page", PAYLOAD_FIELDS)
    cp_dir = tmp_path / "checkpoints"
    engine = ExecutorEngine(
        client=_RecoveryFakeApiClient(detail),
        browser=browser,
        checkpoints=CheckpointStore(cp_dir),
        fault_point=FaultPoint.AFTER_FIELD_WRITE_BEFORE_VERIFIED,
    )

    with pytest.raises(InjectedCrash):
        engine.run(task_id=detail.task_id)

    pending = CheckpointStore(cp_dir).load(detail.task_id)
    assert pending is not None
    assert pending.pending_field_key == "full_name"

    outcome = ExecutorEngine(
        client=_RecoveryFakeApiClient(detail),
        browser=browser,
        checkpoints=CheckpointStore(cp_dir),
    ).run(task_id=detail.task_id)

    assert outcome.kind == "ready_for_review"
    assert outcome.reason_code == "field_write_uncertain"
    assert _telemetry()["field_events"].get("full_name", 0) == 1
    assert _telemetry()["final_clicks"] == 0


def test_process_restart_with_pending_effect_does_not_reclick(
    browser: BrowserSession,
    mock_site_url: str,
    tmp_path: Any,
) -> None:
    """Crash after pending-effect checkpoint: recovery enters review
    without retrying the intermediate click."""
    url = f"{mock_site_url}/multi-step/1"
    detail = _make_detail(url, PAYLOAD_FIELDS)
    fake = _RecoveryFakeApiClient(detail)
    cp_dir = tmp_path / "checkpoints"

    checkpoints = CheckpointStore(cp_dir)
    engine = ExecutorEngine(
        client=fake,
        browser=browser,
        checkpoints=checkpoints,
        fault_point=FaultPoint.AFTER_PENDING_EFFECT_CHECKPOINT_SAVED,
    )
    with pytest.raises(InjectedCrash):
        engine.run(task_id=detail.task_id)

    # Checkpoint should have pending_effect_key set
    saved = checkpoints.load(detail.task_id)
    assert saved is not None
    assert saved.pending_effect_key is not None

    # Second run sees pending effect and enters review without clicking
    engine2 = ExecutorEngine(
        client=_RecoveryFakeApiClient(detail),
        browser=browser,
        checkpoints=CheckpointStore(cp_dir),
    )
    outcome = engine2.run(task_id=detail.task_id)

    assert outcome.kind == "ready_for_review"
    assert outcome.reason_code == "intermediate_result_uncertain"
    # The intermediate button was never clicked (first run crashed before
    # click; second run detected pending effect and refused to click)
    assert _telemetry()["intermediate_clicks"] == 0
    assert _telemetry()["final_clicks"] == 0


# ---------------------------------------------------------------------------
# Lease and conflict recovery
# ---------------------------------------------------------------------------


def test_expired_lease_stops_unauthorized_before_browser_actions(
    browser: BrowserSession,
    mock_site_url: str,
    tmp_path: Any,
) -> None:
    """401 on detail/progress returns stopped_unauthorized."""
    url = f"{mock_site_url}/single-page"
    detail = _make_detail(url, PAYLOAD_FIELDS)
    fake = _RecoveryFakeApiClient(detail)
    fake.lease_denied = True
    checkpoints = CheckpointStore(tmp_path / "checkpoints")
    engine = ExecutorEngine(
        client=fake,
        browser=browser,
        checkpoints=checkpoints,
    )

    outcome = engine.run(task_id=detail.task_id)

    assert outcome.kind == "stopped_unauthorized"
    assert outcome.reason_code == "lease_denied"
    # No browser actions happened
    assert _telemetry()["field_events"] == {}
    assert _telemetry()["final_clicks"] == 0


def test_stale_task_version_stops_without_progress_retry(
    browser: BrowserSession,
    mock_site_url: str,
    tmp_path: Any,
) -> None:
    """409 on progress returns stopped_conflict, exactly one attempt."""
    url = f"{mock_site_url}/single-page"
    detail = _make_detail(url, PAYLOAD_FIELDS)
    fake = _RecoveryFakeApiClient(detail)
    fake.progress_conflict = True
    checkpoints = CheckpointStore(tmp_path / "checkpoints")
    engine = ExecutorEngine(
        client=fake,
        browser=browser,
        checkpoints=checkpoints,
    )

    outcome = engine.run(task_id=detail.task_id)

    assert outcome.kind == "stopped_conflict"
    assert outcome.reason_code == "stale_task_version"
    # Exactly one progress attempt, none succeeded
    assert fake.progress_attempts == 1
    assert fake.progress_calls == []
    assert _telemetry()["final_clicks"] == 0


# ---------------------------------------------------------------------------
# Checkpoint fingerprint mismatch
# ---------------------------------------------------------------------------


def test_changed_fingerprint_enters_review_without_click(
    browser: BrowserSession,
    mock_site_url: str,
    tmp_path: Any,
) -> None:
    """Pre-existing checkpoint with mismatched fingerprint enters review."""
    url = f"{mock_site_url}/single-page"
    detail = _make_detail(url, PAYLOAD_FIELDS)
    fake = _RecoveryFakeApiClient(detail)
    checkpoints = CheckpointStore(tmp_path / "checkpoints")

    # Seed a checkpoint with a deliberately different fingerprint
    mismatched = ExecutorCheckpoint(
        protocol_version=PROTOCOL_VERSION,
        task_id=detail.task_id,
        task_state_version=1,
        step="fill_page",
        page_index=None,
        page_fingerprint="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        completed_field_keys=[],
        completed_effect_keys=[],
        pending_field_key=None,
        pending_effect_key=None,
        issue_counts={"missing": 0, "low": 0, "readback": 0, "defaulted": 0},
    )
    checkpoints.save(mismatched)

    engine = ExecutorEngine(
        client=fake,
        browser=browser,
        checkpoints=checkpoints,
    )
    outcome = engine.run(task_id=detail.task_id)

    assert outcome.kind == "ready_for_review"
    assert outcome.reason_code == "page_topology_changed"
    assert _telemetry()["final_clicks"] == 0


class _FailingCheckpointStore(CheckpointStore):
    def save(self, checkpoint: ExecutorCheckpoint) -> None:
        raise OSError("checkpoint disk unavailable")


def test_checkpoint_write_failure_prevents_intermediate_click(
    browser: BrowserSession,
    mock_site_url: str,
    tmp_path: Any,
) -> None:
    detail = _make_detail(f"{mock_site_url}/multi-step/1", PAYLOAD_FIELDS)
    engine = ExecutorEngine(
        client=_RecoveryFakeApiClient(detail),
        browser=browser,
        checkpoints=_FailingCheckpointStore(tmp_path / "checkpoints"),
    )

    outcome = engine.run(task_id=detail.task_id)

    assert outcome.kind == "failed_safe"
    assert outcome.reason_code == "checkpoint_unavailable"
    assert _telemetry()["intermediate_clicks"] == 0
    assert _telemetry()["final_clicks"] == 0


def test_uncertain_initial_progress_stops_before_browser_actions(
    browser: BrowserSession,
    mock_site_url: str,
    tmp_path: Any,
) -> None:
    detail = _make_detail(f"{mock_site_url}/single-page", PAYLOAD_FIELDS)
    fake = _RecoveryFakeApiClient(detail)
    fake.progress_error_on_attempt[1] = UncertainWriteResult("uncertain")
    engine = ExecutorEngine(
        client=fake,
        browser=browser,
        checkpoints=CheckpointStore(tmp_path / "checkpoints"),
    )

    outcome = engine.run(task_id=detail.task_id)

    assert outcome.kind == "failed_safe"
    assert outcome.reason_code == "progress_result_uncertain"
    assert _telemetry()["field_events"] == {}
    assert _telemetry()["intermediate_clicks"] == 0


def test_lease_failure_before_intermediate_click_stops_without_side_effect(
    browser: BrowserSession,
    mock_site_url: str,
    tmp_path: Any,
) -> None:
    detail = _make_detail(f"{mock_site_url}/multi-step/1", PAYLOAD_FIELDS)
    fake = _RecoveryFakeApiClient(detail)
    fake.detail_error_on_attempt[2] = ApiUnauthorized("expired")
    engine = ExecutorEngine(
        client=fake,
        browser=browser,
        checkpoints=CheckpointStore(tmp_path / "checkpoints"),
    )

    outcome = engine.run(task_id=detail.task_id)

    assert outcome.kind == "stopped_unauthorized"
    assert outcome.reason_code == "progress_denied"
    assert _telemetry()["intermediate_clicks"] == 0
    assert _telemetry()["final_clicks"] == 0


def test_stale_checkpoint_version_stops_before_field_or_page_side_effects(
    browser: BrowserSession,
    mock_site_url: str,
    tmp_path: Any,
) -> None:
    detail = _make_detail(f"{mock_site_url}/single-page", PAYLOAD_FIELDS)
    checkpoints = CheckpointStore(tmp_path / "checkpoints")
    checkpoints.save(
        ExecutorCheckpoint(
            protocol_version=PROTOCOL_VERSION,
            task_id=detail.task_id,
            task_state_version=999,
            step="fill_page",
            page_index=None,
            page_fingerprint="sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            completed_field_keys=["full_name"],
            completed_effect_keys=[],
            issue_counts={"missing": 0, "low": 0, "readback": 0, "defaulted": 0},
        )
    )
    engine = ExecutorEngine(
        client=_RecoveryFakeApiClient(detail),
        browser=browser,
        checkpoints=checkpoints,
    )

    outcome = engine.run(task_id=detail.task_id)

    assert outcome.kind == "ready_for_review"
    assert outcome.reason_code == "checkpoint_version_mismatch"
    assert _telemetry()["field_events"] == {}
    assert _telemetry()["intermediate_clicks"] == 0


class _BrowserThatMustNotOpen:
    def open(self, url: str) -> None:
        raise AssertionError(f"non-loopback browser navigation attempted: {url}")


def test_wave_one_payload_rejects_non_loopback_target_before_navigation(
    mock_site_url: str,
    tmp_path: Any,
) -> None:
    detail = _make_detail(f"{mock_site_url}/single-page", PAYLOAD_FIELDS)
    payload = detail.payload.model_copy(
        update={"target_url": "https://careers.example.com/apply"}
    )
    engine = ExecutorEngine(
        client=_RecoveryFakeApiClient(detail),
        browser=_BrowserThatMustNotOpen(),  # type: ignore[arg-type]
        checkpoints=CheckpointStore(tmp_path / "checkpoints"),
    )

    outcome = engine.run(payload=payload)

    assert outcome.kind == "failed_safe"
    assert outcome.reason_code == "target_not_loopback"


def test_review_status_timeout_does_not_claim_review_or_refill_on_resume(
    browser: BrowserSession,
    mock_site_url: str,
    tmp_path: Any,
) -> None:
    detail = _make_detail(f"{mock_site_url}/single-page", PAYLOAD_FIELDS)
    cp_dir = tmp_path / "checkpoints"
    first_client = _RecoveryFakeApiClient(detail)
    first_client.progress_error_on_attempt[2] = UncertainWriteResult("uncertain")

    first = ExecutorEngine(
        client=first_client,
        browser=browser,
        checkpoints=CheckpointStore(cp_dir),
    ).run(task_id=detail.task_id)

    assert first.kind == "failed_safe"
    assert first.reason_code == "progress_result_uncertain"
    assert _telemetry()["field_events"].get("full_name", 0) == 1
    assert _telemetry()["final_clicks"] == 0

    resumed = ExecutorEngine(
        client=_RecoveryFakeApiClient(detail),
        browser=browser,
        checkpoints=CheckpointStore(cp_dir),
    ).run(task_id=detail.task_id)

    assert resumed.kind == "ready_for_review"
    assert _telemetry()["field_events"].get("full_name", 0) == 1
    assert _telemetry()["final_clicks"] == 0


def test_result_write_timeout_does_not_claim_result_observed(
    browser: BrowserSession,
    mock_site_url: str,
    tmp_path: Any,
) -> None:
    detail = _make_detail(
        f"{mock_site_url}/submission-success",
        PAYLOAD_FIELDS,
        status=TaskStatus.OBSERVING_USER_SUBMISSION,
    )
    fake = _RecoveryFakeApiClient(detail)
    fake.result_error = UncertainWriteResult("uncertain")

    outcome = ExecutorEngine(
        client=fake,
        browser=browser,
        checkpoints=CheckpointStore(tmp_path / "checkpoints"),
    ).run(task_id=detail.task_id)

    assert outcome.kind == "failed_safe"
    assert outcome.reason_code == "result_write_uncertain"
    assert fake.result_calls == []
    assert _telemetry()["final_clicks"] == 0
