from __future__ import annotations

from collections.abc import Iterator
import re
import socket
import threading
from typing import Any

import pytest
import uvicorn

from executor.browser import BrowserSession
from executor.checkpoints import CheckpointStore
from executor.engine import ExecutorEngine
from executor.mock_site.app import app as mock_app, telemetry
from executor.protocol import (
    ExecutorField,
    ExecutorTaskDetail,
    ExecutorTaskPayload,
    ExecutorTaskState,
    FieldConfidence,
    TaskStatus,
)


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
        # Wait for server to be ready
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


@pytest.fixture
def payload_fields() -> list[ExecutorField]:
    return [
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


def _telemetry() -> dict[str, object]:
    return telemetry.snapshot()


def test_single_page_fills_confirmed_fields_but_never_clicks_bottom_action(
    browser: BrowserSession, mock_site_url: str, payload_fields: list[ExecutorField]
) -> None:
    browser.open(f"{mock_site_url}/single-page")
    observation = browser.observe()
    report = browser.fill_confirmed(payload_fields)
    decision = browser.action_decision(observation)
    assert report.confirmed_keys == ["full_name"]
    assert decision.allowed is False
    assert decision.reason_code == "single_page_bottom_action"
    assert _telemetry()["final_clicks"] == 0


def test_ambiguous_and_final_buttons_are_never_clicked(
    browser: BrowserSession, mock_site_url: str
) -> None:
    for path in ("/ambiguous", "/multi-step/2"):
        browser.open(f"{mock_site_url}{path}")
        observation = browser.observe()
        assert browser.action_decision(observation).allowed is False
    current = _telemetry()
    assert current["ambiguous_clicks"] == 0
    assert current["final_clicks"] == 0


def test_multiple_candidate_actions_are_classified_ambiguous(
    browser: BrowserSession, mock_site_url: str
) -> None:
    browser.open(f"{mock_site_url}/single-page")
    browser.page.set_content(
        """
        <main data-topology="multi" data-step-index="1"
              data-step-count="2" data-step-nav="true">
          <button data-action-kind="next">保存并下一步</button>
          <button data-action-kind="final">提交申请</button>
        </main>
        """
    )

    decision = browser.action_decision(browser.observe())

    assert decision.allowed is False
    assert decision.reason_code == "ambiguous_action_forbidden"


# ---------------------------------------------------------------------------
# Engine-level integration tests
# ---------------------------------------------------------------------------


class _FakeApiClient:
    """In-memory fake of ExecutorApiClient for engine tests.

    Tracks all calls and returns controlled responses based on the
    ``ExecutorTaskDetail`` passed at construction time.
    """

    def __init__(self, detail: ExecutorTaskDetail) -> None:
        self._detail = detail
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

    def get_task(self, task_id: str, lease: str) -> ExecutorTaskDetail:
        self.get_task_calls.append((task_id, lease))
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


def _make_detail(
    url: str,
    fields: list[ExecutorField],
    *,
    status: TaskStatus = TaskStatus.DISPATCHED,
) -> ExecutorTaskDetail:
    """Build an ExecutorTaskDetail pointing at *url*."""
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


def test_single_page_ends_in_review_without_final_click(
    browser: BrowserSession,
    mock_site_url: str,
    payload_fields: list[ExecutorField],
    tmp_path: Any,
) -> None:
    """Single page: fill confirmed fields, stop before final click."""
    url = f"{mock_site_url}/single-page"
    detail = _make_detail(url, payload_fields)
    fake = _FakeApiClient(detail)
    checkpoints = CheckpointStore(tmp_path / "checkpoints")
    engine = ExecutorEngine(client=fake, browser=browser, checkpoints=checkpoints)

    outcome = engine.run(task_id=detail.task_id)

    assert outcome.kind == "ready_for_review"
    assert outcome.reason_code == "single_page_bottom_action"
    assert fake.heartbeat_calls == ["0.1.0"]
    assert len(fake.lease_calls) == 1
    assert len(fake.get_task_calls) == 1
    # Expect at least one progress report (failed safety)
    assert any(
        c["target_status"] == "ready_for_review" for c in fake.progress_calls
    )
    assert _telemetry()["final_clicks"] == 0


def test_login_gate_waits_for_explicit_user_resume(
    browser: BrowserSession,
    mock_site_url: str,
    payload_fields: list[ExecutorField],
    tmp_path: Any,
) -> None:
    """Human gate page should yield waiting_for_human without filling."""
    url = f"{mock_site_url}/human-gate"
    detail = _make_detail(url, payload_fields)
    fake = _FakeApiClient(detail)
    checkpoints = CheckpointStore(tmp_path / "checkpoints")
    engine = ExecutorEngine(client=fake, browser=browser, checkpoints=checkpoints)

    outcome = engine.run(task_id=detail.task_id)

    assert outcome.kind == "waiting_for_human"
    assert outcome.reason_code == "login_required"
    # No progress call for ready_for_review -- engine stops at human gate
    assert all(
        c["target_status"] != "ready_for_review" for c in fake.progress_calls
    )


def test_multi_step_intermediate_safe_click(
    browser: BrowserSession,
    mock_site_url: str,
    payload_fields: list[ExecutorField],
    tmp_path: Any,
) -> None:
    """Multi-step: fill, click safe next, navigate, end in review."""
    url = f"{mock_site_url}/multi-step/1"
    detail = _make_detail(url, payload_fields)
    fake = _FakeApiClient(detail)
    checkpoints = CheckpointStore(tmp_path / "checkpoints")
    engine = ExecutorEngine(client=fake, browser=browser, checkpoints=checkpoints)

    outcome = engine.run(task_id=detail.task_id)

    assert outcome.kind == "ready_for_review"
    assert outcome.reason_code == "navigated"
    # Should have reported progress at least for running and ready_for_review
    statuses = [c["target_status"] for c in fake.progress_calls]
    assert "running" in statuses
    assert "ready_for_review" in statuses
    assert all(
        re.fullmatch(r"sha256:[0-9a-f]{6,64}", str(call["page_fingerprint"]))
        for call in fake.progress_calls
    )
    # The intermediate button was clicked -- mock site records it
    assert _telemetry()["intermediate_clicks"] == 1
    # The final page button must NOT have been clicked
    assert _telemetry()["final_clicks"] == 0


def test_readback_mismatch_triggers_review(
    browser: BrowserSession,
    mock_site_url: str,
    payload_fields: list[ExecutorField],
    tmp_path: Any,
) -> None:
    """Readback-mismatch page: field value gets reset after fill -> review."""
    url = f"{mock_site_url}/readback-mismatch"
    detail = _make_detail(url, payload_fields)
    fake = _FakeApiClient(detail)
    checkpoints = CheckpointStore(tmp_path / "checkpoints")
    engine = ExecutorEngine(client=fake, browser=browser, checkpoints=checkpoints)

    outcome = engine.run(task_id=detail.task_id)

    assert outcome.kind == "ready_for_review"
    assert outcome.reason_code == "readback_mismatch"
    # Should have reported ready_for_review with readback_mismatch reason
    mismatch_reports = [
        c for c in fake.progress_calls
        if c["target_status"] == "ready_for_review"
        and c["reason_code"] == "readback_mismatch"
    ]
    assert len(mismatch_reports) >= 1
    assert _telemetry()["final_clicks"] == 0


@pytest.mark.parametrize(
    "path, expected_target",
    [
        ("/submission-success", "submitted_success"),
        ("/submission-failed", "submitted_failed"),
        ("/submission-unknown", "result_unknown"),
    ],
)
def test_observation_result_observed(
    browser: BrowserSession,
    mock_site_url: str,
    payload_fields: list[ExecutorField],
    tmp_path: Any,
    path: str,
    expected_target: str,
) -> None:
    """Post-HUMAN observation: check result page parsing."""
    url = f"{mock_site_url}{path}"
    detail = _make_detail(
        url, payload_fields, status=TaskStatus.OBSERVING_USER_SUBMISSION
    )
    fake = _FakeApiClient(detail)
    checkpoints = CheckpointStore(tmp_path / "checkpoints")
    engine = ExecutorEngine(client=fake, browser=browser, checkpoints=checkpoints)

    outcome = engine.run(task_id=detail.task_id)

    assert outcome.kind == "result_observed"
    assert outcome.reason_code == expected_target
    # Should have called report_result
    assert any(
        c["target_status"] == expected_target for c in fake.result_calls
    )
