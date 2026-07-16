from __future__ import annotations

from collections.abc import Iterator
import os
import socket
import threading
from typing import Any

import pytest
import uvicorn
from fastapi.testclient import TestClient

from executor.browser import BrowserSession
from executor.mock_site.app import app as mock_app, telemetry
from executor.protocol import ExecutorField, ExecutorTaskPayload, FieldConfidence


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
