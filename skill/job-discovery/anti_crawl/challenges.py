"""挑战检测与处理：不破解，只检测 + 人工协作或退避。

检测优先级（spec §4.3）：
  HTTP 特征（429 / 403+限流文案）→ rate_limited
  滑块（先于验证码——滑块文案常含"验证码"字样）→ slider
  验证码 → captcha
  JS 挑战（加速乐/瑞数类，真实浏览器通常自动通过）→ js_challenge
  登录墙（窄短语，避免与页面普通"登录"导航误判）→ login_wall
"""
from __future__ import annotations

import sys
import time
from typing import Any

RATE_LIMIT_TEXT_PHRASES = ("访问过于频繁", "请求频率过高", "操作过快", "稍后再试", "too many requests", "rate limit")

SLIDER_PHRASES = ("拖动滑块", "按住滑块", "向右滑动", "滑动拼图", "拼图验证", "slide to verify", "拖动下方滑块")

CAPTCHA_PHRASES = ("验证码", "captcha", "请输入验证码", "图形验证")

JS_CHALLENGE_PHRASES = (
    "环境异常", "完成验证后即可继续访问", "验证中", "访问异常",
    "checking your browser", "js_challenge", "正在检测浏览器环境",
)

# 刻意窄：普通页面的"登录"导航链接、"岗位职责"里的"验证码"字样都不命中。
LOGIN_WALL_PHRASES = ("请先登录后查看", "登录后查看", "登录后可查看", "请登录后查看", "sign in to view", "login required to view")


class ChallengeDetected(Exception):
    """需要外部动作的挑战（人工登录/风控退避），调用方决定如何上报。"""

    def __init__(self, challenge_type: str, evidence: str) -> None:
        super().__init__(f"{challenge_type}: {evidence}")
        self.challenge_type = challenge_type
        self.evidence = evidence


def detect_challenge(page: Any, response_status: int | None = None, url: str = "") -> str | None:
    if response_status == 429:
        return "rate_limited"
    body = ""
    try:
        body = str(page.evaluate("() => document.body.innerText || ''") or "")
    except Exception:
        pass
    low = body.lower()
    if response_status == 403 and any(p in low for p in RATE_LIMIT_TEXT_PHRASES):
        return "rate_limited"
    for phrase in SLIDER_PHRASES:
        if phrase in low:
            return "slider"
    for phrase in CAPTCHA_PHRASES:
        if phrase in low:
            return "captcha"
    for phrase in JS_CHALLENGE_PHRASES:
        if phrase in low:
            return "js_challenge"
    for phrase in LOGIN_WALL_PHRASES:
        if phrase in low:
            return "login_wall"
    return None


def challenge_resolved(page: Any) -> bool:
    return detect_challenge(page, None, page.url) is None


def handle_challenge(
    page: Any,
    challenge_type: str,
    *,
    timeout_s: int = 300,
    poll_interval_s: int = 5,
) -> bool:
    """处理一种挑战。返回是否已解除。

    - slider/captcha：有头窗口保留，提示人工处理，每 poll_interval_s 轮询一次，
      直到解除或 timeout_s 超时（超时返回 False，绝不自动破解）。
    - js_challenge：真实浏览器通常几秒内自动通过；等最多 5 秒，未过抛异常。
    - login_wall / rate_limited：不是本函数能处理的，直接抛 ChallengeDetected。
    """
    if challenge_type in ("slider", "captcha"):
        # 人工提示走 stderr：stdout 契约是机器 JSON，不得混流（评审 I3）
        print(
            f"\n[anti-crawl] 检测到 {challenge_type}。请在浏览器窗口内人工完成验证"
            f"（合规红线：不自动破解）。最多等待 {timeout_s}s。\n",
            file=sys.stderr, flush=True,
        )
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if challenge_resolved(page):
                print("[anti-crawl] 挑战已解除，继续。", file=sys.stderr, flush=True)
                return True
            time.sleep(poll_interval_s)
        print(f"[anti-crawl] {challenge_type} 在 {timeout_s}s 内未解除。", file=sys.stderr, flush=True)
        return False
    if challenge_type == "js_challenge":
        deadline = time.monotonic() + min(5, timeout_s)  # 默认 5s；显式更短时尊重调用方（评审 M1）
        while time.monotonic() < deadline:
            if challenge_resolved(page):
                return True
            time.sleep(1)
        raise ChallengeDetected("js_challenge", "JS 挑战 5s 内未自动通过")
    raise ChallengeDetected(
        challenge_type,
        f"{challenge_type} 需要外部动作（login.py / 等待退避）",
    )


def blocked_status(challenge_type: str) -> str:
    return f"blocked:{challenge_type}"
