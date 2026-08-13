from __future__ import annotations

import pytest

from anti_crawl.challenges import (
    ChallengeDetected,
    blocked_status,
    challenge_resolved,
    detect_challenge,
    handle_challenge,
)


class _FakePage:
    def __init__(self, body: str = "", url: str = "https://x.example/jobs") -> None:
        self._body = body
        self.url = url

    def evaluate(self, script: str):
        # detect_challenge 只调 document.body.innerText
        return self._body


def test_plain_page_no_challenge() -> None:
    page = _FakePage("职位列表 工程师 运营")
    assert detect_challenge(page, 200, page.url) is None


def test_http_429_is_rate_limited() -> None:
    page = _FakePage("稍后再试")
    assert detect_challenge(page, 429, page.url) == "rate_limited"


def test_403_with_rate_limit_text() -> None:
    page = _FakePage("访问过于频繁，请稍后再试")
    assert detect_challenge(page, 403, page.url) == "rate_limited"


def test_403_alone_is_js_challenge_when_phrase_present() -> None:
    page = _FakePage("完成验证后即可继续访问")
    assert detect_challenge(page, 403, page.url) == "js_challenge"


def test_slider_beats_captcha_phrase() -> None:
    page = _FakePage("请输入验证码并拖动滑块完成验证")  # 同时含 captcha 与 slider 短语，锁定优先级
    assert detect_challenge(page, 200, page.url) == "slider"


def test_captcha_phrase() -> None:
    page = _FakePage("请输入验证码")
    assert detect_challenge(page, 200, page.url) == "captcha"


def test_login_wall_phrase() -> None:
    page = _FakePage("请先登录后查看职位详情")
    assert detect_challenge(page, 200, page.url) == "login_wall"


def test_challenge_resolved() -> None:
    page = _FakePage("正常内容")
    assert challenge_resolved(page) is True


def test_handle_login_wall_raises() -> None:
    page = _FakePage("请先登录")
    with pytest.raises(ChallengeDetected) as exc:
        handle_challenge(page, "login_wall")
    assert exc.value.challenge_type == "login_wall"


def test_handle_js_challenge_times_out_and_raises() -> None:
    page = _FakePage("完成验证后即可继续访问")
    with pytest.raises(ChallengeDetected) as exc:
        handle_challenge(page, "js_challenge", timeout_s=0)
    assert exc.value.challenge_type == "js_challenge"


def test_blocked_status_mapping() -> None:
    assert blocked_status("slider") == "blocked:slider"
    assert blocked_status("rate_limited") == "blocked:rate_limited"
