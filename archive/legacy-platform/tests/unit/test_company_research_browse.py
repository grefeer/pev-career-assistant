"""Unit tests for the company-research browse page fetcher."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


_BROWSE_PATH = (
    Path(__file__).resolve().parents[2]
    / "skill"
    / "company-research"
    / "scripts"
    / "browse.py"
)


@pytest.fixture(scope="module")
def browse():
    """Load the standalone browse script as an importable module."""
    spec = importlib.util.spec_from_file_location(
        "company_research_browse", _BROWSE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _FakeCtx:
    """Stand-in for ``sync_playwright()``'s context manager."""

    def __init__(self, browser) -> None:
        self._browser = browser

    def __enter__(self):
        return self

    def __exit__(self, *exc_info) -> bool:
        return False

    @property
    def chromium(self):
        return SimpleNamespace(launch=lambda **kwargs: self._browser)


def _fake_playwright(text: str, title: str):
    page = SimpleNamespace(
        goto=lambda *a, **k: None,
        wait_for_timeout=lambda ms: None,
        inner_text=lambda selector="body": text,
        title=lambda: title,
        close=lambda: None,
    )
    browser = SimpleNamespace(new_page=lambda: page, close=lambda: None)
    return lambda: _FakeCtx(browser)


def test_detect_block_finds_verification_wall(browse) -> None:
    assert browse._detect_block("请完成验证后即可继续访问") == "anti_bot"
    assert browse._detect_block("环境异常，请稍后重试") == "anti_bot"


def test_detect_block_returns_none_for_normal_text(browse) -> None:
    assert browse._detect_block("We are hiring engineers.") is None


def test_main_writes_page_and_ok_metadata(browse, tmp_path, monkeypatch) -> None:
    out = tmp_path / "evidence"
    monkeypatch.setattr(
        browse,
        "_fetch_page",
        lambda url, wait: (
            "Acme is hiring engineers across multiple teams worldwide.",
            "Acme Careers",
        ),
    )
    rc = browse.main(["https://careers.acme.example", "--out", str(out), "--wait", "0"])

    assert rc == 0
    assert (out / "pages" / "page_001.txt").read_text(encoding="utf-8") == (
        "Acme is hiring engineers across multiple teams worldwide."
    )
    metadata = json.loads((out / "browse_metadata.json").read_text(encoding="utf-8"))
    assert metadata["status"] == "ok"
    assert metadata["title"] == "Acme Careers"
    assert metadata["pages_collected"] == 1


def test_main_blocked_writes_no_page(browse, tmp_path, monkeypatch) -> None:
    out = tmp_path / "evidence"
    monkeypatch.setattr(
        browse, "_fetch_page", lambda url, wait: ("请完成验证后即可继续访问", "Wall")
    )
    rc = browse.main(["https://careers.acme.example", "--out", str(out)])

    assert rc == 0
    assert not (out / "pages").exists()
    metadata = json.loads((out / "browse_metadata.json").read_text(encoding="utf-8"))
    assert metadata["status"] == "blocked"
    assert metadata["block_reason"] == "anti_bot"


def test_main_empty_writes_no_page(browse, tmp_path, monkeypatch) -> None:
    out = tmp_path / "evidence"
    monkeypatch.setattr(browse, "_fetch_page", lambda url, wait: ("tiny", "T"))
    rc = browse.main(["https://careers.acme.example", "--out", str(out)])

    assert rc == 0
    assert not (out / "pages").exists()
    metadata = json.loads((out / "browse_metadata.json").read_text(encoding="utf-8"))
    assert metadata["status"] == "empty"


def test_main_error_returns_nonzero(browse, tmp_path, monkeypatch) -> None:
    out = tmp_path / "evidence"

    def boom(url, wait):
        raise RuntimeError("navigation timeout")

    monkeypatch.setattr(browse, "_fetch_page", boom)
    rc = browse.main(["https://careers.acme.example", "--out", str(out)])

    assert rc == 1
    metadata = json.loads((out / "browse_metadata.json").read_text(encoding="utf-8"))
    assert metadata["status"] == "error"
    assert "navigation timeout" in metadata["error"]


def test_fetch_page_uses_playwright_and_waits(browse, monkeypatch) -> None:
    monkeypatch.setattr(
        "playwright.sync_api.sync_playwright", _fake_playwright("body text", "Title")
    )
    text, title = browse._fetch_page("https://careers.acme.example", 800)

    assert text == "body text"
    assert title == "Title"


def test_fetch_page_skips_wait_when_zero(browse, monkeypatch) -> None:
    waited = {"ms": None}
    page = SimpleNamespace(
        goto=lambda *a, **k: None,
        wait_for_timeout=lambda ms: waited.update(ms=ms),
        inner_text=lambda selector="body": "body",
        title=lambda: "T",
        close=lambda: None,
    )
    browser = SimpleNamespace(new_page=lambda: page, close=lambda: None)
    monkeypatch.setattr(
        "playwright.sync_api.sync_playwright", lambda: _FakeCtx(browser)
    )
    browse._fetch_page("https://careers.acme.example", 0)

    assert waited["ms"] is None  # wait_for_timeout never called
