"""Step-by-step replication of _click_view_all_positions internals WITH prints,
to find exactly where the click throws (or where the function diverges from
the working manual click).
"""
from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from playwright.sync_api import sync_playwright  # noqa: E402

from backend.app.services.job_discovery.deepagents_runner import (  # noqa: E402
    _CONSENT_BLOCK_KEYWORDS,
    _NAV_USER_AGENT,
    _VIEW_ALL_POSITIONS_TEXTS,
    _dismiss_consent_dialog,
)

URL = "https://app.mokahr.com/campus-recruitment/deeproute/145894#/home"

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(user_agent=_NAV_USER_AGENT)
    page.goto(URL, wait_until="domcontentloaded", timeout=30_000)
    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:
        page.wait_for_timeout(3_000)
    _dismiss_consent_dialog(page)
    print(f"BEFORE: url={page.url}", flush=True)

    pat = "查看更多职位"
    loc = page.get_by_text(pat, exact=True).first
    cnt = loc.count()
    print(f"  count={cnt}", flush=True)
    vis = loc.is_visible(timeout=800)
    print(f"  is_visible={vis}", flush=True)
    txt = (loc.inner_text(timeout=800) or "").strip()
    print(f"  inner_text={txt!r}", flush=True)
    if any(kw in txt for kw in _CONSENT_BLOCK_KEYWORDS):
        print("  WALL keyword match -> would skip", flush=True)
        browser.close()
        raise SystemExit

    for attempt in range(3):
        print(f"  --- attempt {attempt} ---", flush=True)
        try:
            loc.scroll_into_view_if_needed(timeout=2_000)
            print("    scroll OK", flush=True)
        except Exception as e:
            print(f"    scroll threw: {e!r}", flush=True)
        try:
            loc.click(timeout=3_000, no_wait_after=True)
            print("    click(no_wait_after) OK", flush=True)
            clicked = True
        except Exception as e:
            print(f"    click threw: {e!r}", flush=True)
            clicked = False
            try:
                loc.click(timeout=3_000, force=True, no_wait_after=True)
                print("    force click OK", flush=True)
                clicked = True
            except Exception as e2:
                print(f"    force click threw: {e2!r}", flush=True)
                page.wait_for_timeout(1_000)
                continue
        if clicked:
            page.wait_for_timeout(1_500)
            print(f"    AFTER click url={page.url}", flush=True)
            try:
                page.wait_for_load_state("networkidle", timeout=6_000)
                print("    networkidle OK", flush=True)
            except Exception as e:
                print(f"    networkidle threw: {e!r}", flush=True)
            break

    print(f"AFTER loop: url={page.url}", flush=True)
    try:
        body = page.locator("body").inner_text(timeout=3_000) or ""
    except Exception:
        body = ""
    print(f"  body has '21 结果': {'21 结果' in body}", flush=True)
    browser.close()
