from __future__ import annotations

import json
from pathlib import Path

from anti_crawl.profiles import (
    LoginStatus,
    check_login_signal,
    profile_dir_for,
    profile_meta,
    read_login_state,
    record_login,
    store_dir,
)


class _FakeLocator:
    def __init__(self, visible: bool) -> None:
        self._visible = visible

    @property
    def first(self) -> _FakeLocator:
        return self

    def is_visible(self) -> bool:
        return self._visible


class _FakePage:
    """最小页面替身：只实现 check_login_signal 用到的三件事。"""

    def __init__(self, url: str = "https://x.example/", visible_selectors: set[str] | None = None,
                 visible_texts: set[str] | None = None) -> None:
        self.url = url
        self._selectors = visible_selectors or set()
        self._texts = visible_texts or set()

    def locator(self, selector: str) -> _FakeLocator:
        return _FakeLocator(selector in self._selectors)

    def get_by_text(self, text: str, exact: bool = False) -> _FakeLocator:
        return _FakeLocator(text in self._texts)


def test_store_dir_under_anti_crawl() -> None:
    assert store_dir().name == "store"
    assert store_dir().parent.name == "anti_crawl"


def test_profile_dir_isolation() -> None:
    a = profile_dir_for("liepin")
    b = profile_dir_for("58")
    assert a != b
    assert "liepin" in str(a)
    assert a.name == "user_data_dir"


def test_profile_meta_stable_across_calls() -> None:
    m1 = profile_meta("selftest_meta")
    m2 = profile_meta("selftest_meta")
    assert m1 == m2
    assert m1["viewport"] == {"width": 1920, "height": 1080}


def test_check_login_signal_none_when_no_signal() -> None:
    assert check_login_signal(_FakePage(), None) is None


def test_check_login_signal_url_contains() -> None:
    page = _FakePage(url="https://www.liepin.com/mylife/")
    assert check_login_signal(page, {"url_contains": "mylife"}) is True


def test_check_login_signal_selector_and_text() -> None:
    page = _FakePage(visible_selectors={"a[href*='mylife']"}, visible_texts={"我的求职"})
    assert check_login_signal(page, {"selector": "a[href*='mylife']", "text": "我的求职"}) is True
    assert check_login_signal(page, {"selector": "a[href*='nope']"}) is False


def test_login_state_roundtrip(tmp_path: Path, monkeypatch) -> None:
    # 注意：record_login/read_login_state 用的是模块级 STATE_DIR 常量，
    # 必须 patch STATE_DIR（patch STORE_DIR 不生效，会写进真实 store/）
    monkeypatch.setattr("anti_crawl.profiles.STATE_DIR", tmp_path / "state")
    record_login("liepin", LoginStatus.LOGGED_IN.value)
    state = read_login_state("liepin")
    assert state is not None and state["status"] == "logged_in"
    assert read_login_state("missing_site") is None
