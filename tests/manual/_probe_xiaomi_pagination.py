"""Probe xiaomi (Mioffice) page for pagination / load-more controls. No LLM."""
from __future__ import annotations
import sys
from pathlib import Path
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
from backend.app.services.job_discovery.deepagents_runner import (  # noqa: E402
    _NAV_USER_AGENT,
    _dismiss_consent_dialog,
)
from playwright.sync_api import sync_playwright  # noqa: E402

URL = "https://xiaomi.jobs.f.mioffice.cn/s/kJVnd58xtWY"
with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True)
    p = b.new_page(user_agent=_NAV_USER_AGENT)
    p.goto(URL, wait_until="domcontentloaded", timeout=30_000)
    try:
        p.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:
        p.wait_for_timeout(3_000)
    _dismiss_consent_dialog(p)
    p.wait_for_timeout(2_000)
    txt = p.locator("body").inner_text(timeout=10_000) or ""
    lines = [l.strip() for l in txt.splitlines() if l.strip()]
    print("TOTAL non-empty lines:", len(lines), flush=True)
    print("=== LAST 25 lines ===", flush=True)
    for l in lines[-25:]:
        print("  ", repr(l[:80]), flush=True)
    print("=== CSS selector probe ===", flush=True)
    selectors = [
        ".ant-pagination-next", ".el-pagination .btn-next", "[class*=pagination]",
        "[class*=page-next]", "[class*=Pager]", "[class*=pager]",
        "li[role=button]", "button[type=button]", ".next-btn", "[class*=next]",
    ]
    for sel in selectors:
        try:
            n = p.locator(sel).count()
            if n:
                print(f"  {sel}: count={n}", flush=True)
        except Exception:
            pass
    print("=== text probe ===", flush=True)
    texts = ["下一页", "下页", "next", "»", "›", "加载更多", "更多", "查看更多", "尾页", "末页"]
    for t in texts:
        try:
            n = p.get_by_text(t, exact=False).count()
            if n:
                print(f"  text {t!r}: count={n}", flush=True)
        except Exception:
            pass
    b.close()
