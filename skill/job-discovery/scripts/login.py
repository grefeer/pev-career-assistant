#!/usr/bin/env python3
"""login.py — 交互式一次性登录（人扫码/短信/滑块），profile 持久化复用。

用法：
  python scripts/login.py --site liepin [--timeout 600] [--headless]

流程（spec §4.6）：
  1. 打开该站 login_url（有头 stealth 浏览器，使用该站独立 profile）
  2. 提示用户完成登录（扫码/短信/滑块——都是人工动作）
  3. 每 3s 轮询 login_signal，检测到登录态即保存 profile 并退出 0
  4. 超时退出 1，提示重试

stdout 输出单行 JSON：{"status": "logged_in"|"login_timeout"|"error", "site": ...}
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from anti_crawl import engine  # noqa: E402
from anti_crawl.profiles import LoginStatus, check_login_signal, record_login  # noqa: E402
from anti_crawl.site_registry import get_site  # noqa: E402

POLL_INTERVAL_S = 3


def _emit(result: dict) -> None:
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()


def main() -> None:
    parser = argparse.ArgumentParser(description="交互式登录招聘站点（人工扫码/短信/滑块）")
    parser.add_argument("--site", required=True, help="站点档案 key（见 anti_crawl/site_registry.py）")
    parser.add_argument("--timeout", type=int, default=600, help="登录等待秒数（默认 600）")
    parser.add_argument("--headless", action="store_true", help="无头模式（不推荐，滑块需有头）")
    args = parser.parse_args()

    try:
        entry = get_site(args.site)
    except KeyError as exc:
        _emit({"status": "error", "site": args.site, "error": str(exc)})
        sys.exit(1)

    signal = entry.get("login_signal")
    if not signal:
        _emit({
            "status": "error", "site": args.site,
            "error": f"{args.site} 未配置 login_signal，无法自动检测登录态；"
                     "请在 anti_crawl/site_registry.py 补充后重试",
        })
        sys.exit(1)

    print(f"[login] 站点 {args.site}: 即将打开 {entry.get('login_url') or '主页'}。"
          f"请在浏览器窗口内完成登录（扫码/短信/滑块）。", file=sys.stderr, flush=True)

    context = engine.launch_context(args.site, headless=args.headless)
    try:
        page = context.new_page()
        target = entry.get("login_url") or ""
        if target:
            try:
                page.goto(target, wait_until="domcontentloaded", timeout=45000)
            except Exception as exc:
                _emit({"status": "error", "site": args.site, "error": f"打开页面失败: {exc}"})
                sys.exit(1)
        page.wait_for_timeout(3000)

        deadline = time.monotonic() + args.timeout
        logged_in = False
        while time.monotonic() < deadline:
            if check_login_signal(page, signal) is True:
                logged_in = True
                break
            time.sleep(POLL_INTERVAL_S)

        if logged_in:
            record_login(args.site, LoginStatus.LOGGED_IN.value)
            _emit({"status": "logged_in", "site": args.site, "url": page.url})
            sys.exit(0)
        _emit({
            "status": "login_timeout", "site": args.site,
            "error": f"{args.timeout}s 内未检测到登录态（login_signal 未命中）",
        })
        sys.exit(1)
    finally:
        engine.close_context(context)


if __name__ == "__main__":
    main()
