#!/usr/bin/env python3
"""check_login.py — 全站登录态/反爬健康检查。

用法：
  python scripts/check_login.py [--site liepin] [--headless]

遍历已建档站点（或指定站），用各自 profile 打开轻量页面，输出表格：
  site | login_status | anti_crawl_status | last_checked
供 LLM 编排前决策：未登录 → 先跑 login.py；被风控 → 换策略或等待。

stdout 表格 + 末尾单行 JSON 汇总；结果快照写 store/state/health.json。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from anti_crawl import engine  # noqa: E402
from anti_crawl.profiles import STORE_DIR, read_login_state  # noqa: E402
from anti_crawl.site_registry import get_site, list_sites  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="登录态/反爬健康检查")
    parser.add_argument("--site", default=None, help="只检查指定站（默认全部）")
    parser.add_argument("--headless", action="store_true", help="无头探测（默认有头）")
    args = parser.parse_args()

    keys = [args.site] if args.site else list_sites()
    now = datetime.now().isoformat(timespec="seconds")
    rows: list[dict] = []

    print(f"{'site':<10} | {'login_status':<14} | {'anti_crawl_status':<16} | last_checked", flush=True)
    for key in keys:
        entry = get_site(key)
        probe = engine.probe_site(key, entry, headless=args.headless)
        login = probe["login_status"]
        challenge = probe["challenge"] or "ok"
        row = {
            "site": key,
            "login_status": login,
            "anti_crawl_status": challenge,
            "last_checked": now,
            "checked_url": probe["url"],
        }
        rows.append(row)
        previous = read_login_state(key)
        stamp = previous.get("last_login_at", "") if previous else ""
        print(f"{key:<10} | {login:<14} | {challenge:<16} | {stamp}", flush=True)

    snapshot = {"checked_at": now, "sites": rows}
    state_path = STORE_DIR / "state" / "health.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")

    sys.stdout.buffer.write(json.dumps({"status": "ok", "checked_at": now, "sites": rows},
                                       ensure_ascii=False).encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
