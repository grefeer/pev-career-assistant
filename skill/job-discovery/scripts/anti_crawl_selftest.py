#!/usr/bin/env python3
"""anti_crawl_selftest.py — 本地自检（无需网络）。

验收载体（规格 §9.1）：对本地页面（有头模式，引擎默认配置）验证：
  1. stealth：navigator.webdriver 为 undefined、UA 无 HeadlessChrome、languages 非空
  2. 指纹稳定：同一 profile 两次 canvas hash 一致
  3. 登录信号匹配（profiles.check_login_signal）
  4. pacing 生效（间隔 >= 下限；日上限抛 PacingViolation）
全部通过退出 0；任一失败退出 1。
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from anti_crawl.engine import STEALTH_INIT_SCRIPT, apply_stealth, launch_context  # noqa: E402
from anti_crawl.pacing import PacingConfig, PacingController, PacingViolation  # noqa: E402
from anti_crawl.profiles import check_login_signal  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def _check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""), flush=True)


def _run() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        context = launch_context("selftest", headless=False, profile_dir=Path(tmp) / "profile")
        try:
            page = context.new_page()
            page.set_content("<html><body><p>selftest</p></body></html>")

            # 1a. navigator.webdriver
            webdriver = page.evaluate("() => navigator.webdriver")
            _check("stealth: navigator.webdriver 隐藏", webdriver is None or webdriver is False)

            # 1b. UA 不含 HeadlessChrome，且形如真实 Chrome
            ua = page.evaluate("() => navigator.userAgent")
            _check("stealth: UA 无 HeadlessChrome", "HeadlessChrome" not in ua, ua)
            import re
            _check("stealth: UA 形如真实 Chrome",
                   bool(re.search(r"Mozilla/5.0.*Chrome/\d+.*Safari/537.36", ua)))

            # 1c. languages 非空
            langs = page.evaluate("() => navigator.languages")
            _check("stealth: navigator.languages 非空", bool(langs))

            # 1d. init script 存在且语法可注入（上面全部已隐式证明；显式留痕）
            _check("stealth: STEALTH_INIT_SCRIPT 非空", bool(STEALTH_INIT_SCRIPT.strip()))

            # 2. 指纹稳定：同一 profile 内两次 canvas hash 一致
            def canvas_hash() -> str:
                return page.evaluate(
                    "() => { const c = document.createElement('canvas');"
                    " c.width = 220; c.height = 30;"
                    " const g = c.getContext('2d');"
                    " g.textBaseline = 'top'; g.font = '14px Arial';"
                    " g.fillText('anti-crawl fingerprint probe', 2, 2);"
                    " return c.toDataURL(); }"
                )
            h1 = canvas_hash()
            page.set_content("<html><body>second</body></html>")
            h2 = canvas_hash()
            _check("指纹稳定: 同 profile 两次 canvas hash 一致", h1 == h2)

            # 3. 登录信号匹配（本地页面注入可见元素）
            page.set_content(
                "<html><body><a href='/mylife'>我的求职</a></body></html>"
            )
            _check("信号: selector 命中",
                   check_login_signal(page, {"selector": "a[href*='mylife']"}) is True)
            _check("信号: text 命中",
                   check_login_signal(page, {"text": "我的求职"}) is True)
            _check("信号: 未命中返回 False",
                   check_login_signal(page, {"selector": "a[href*='nope']"}) is False)
            _check("信号: 无信号返回 None",
                   check_login_signal(page, None) is None)
        finally:
            from anti_crawl.engine import close_context
            close_context(context)

    # 4. pacing（离线纯逻辑）
    with tempfile.TemporaryDirectory() as tmp:
        pacing = PacingController(
            "selftest_pacing",
            PacingConfig(base_interval_s=(0.03, 0.04)),
            Path(tmp),
        )
        start = time.monotonic()
        for _ in range(5):
            pacing.wait_before_request()
            pacing.record_request()
        elapsed = time.monotonic() - start
        _check("pacing: 5 次间隔 >= 0.15s", elapsed >= 0.15, f"{elapsed:.3f}s")

        capped = PacingController(
            "selftest_cap",
            PacingConfig(base_interval_s=(0.01, 0.01), max_pages_per_day=2),
            Path(tmp),
        )
        capped.record_request()
        capped.record_request()
        try:
            capped.wait_before_request()
            cap_ok = False
        except PacingViolation:
            cap_ok = True
        _check("pacing: 日上限触发 PacingViolation", cap_ok)

    failed = [name for name, ok, _ in RESULTS if not ok]
    print("")
    if failed:
        print(f"自检失败: {', '.join(failed)}", flush=True)
        return 1
    print(f"自检全部通过 ({len(RESULTS)} 项)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(_run())
