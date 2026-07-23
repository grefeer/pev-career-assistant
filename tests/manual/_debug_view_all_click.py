"""Focused debug: does _click_view_all_positions actually navigate deeproute
from #/home (5 featured jobs) to #/jobs/ (21 results)?

Prints the URL before/after the function call and the result, then re-checks
the page for "21 结果" so we know whether the click took effect inside the
function.
"""
from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from playwright.sync_api import sync_playwright  # noqa: E402

from backend.app.services.job_discovery.deepagents_runner import (  # noqa: E402
    _NAV_USER_AGENT,
    _VIEW_ALL_POSITIONS_TEXTS,
    _click_view_all_positions,
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

    print(f"BEFORE click_view_all: url={page.url}", flush=True)
    # quick probe: which view-all texts are present and visible?
    for pat in _VIEW_ALL_POSITIONS_TEXTS:
        loc = page.get_by_text(pat, exact=True).first
        try:
            cnt = loc.count()
            vis = loc.is_visible(timeout=400) if cnt else False
        except Exception as e:
            cnt, vis = f"err:{e}", False
        if cnt:
            print(f"  probe pat={pat!r} count={cnt} visible={vis}", flush=True)

    result = _click_view_all_positions(page)
    print(f"_click_view_all_positions returned: {result}", flush=True)
    print(f"AFTER click_view_all: url={page.url}", flush=True)

    try:
        body = page.locator("body").inner_text(timeout=3_000) or ""
    except Exception:
        body = ""
    has_21 = "21 结果" in body or "21结果" in body
    print(f"body contains '21 结果': {has_21}", flush=True)
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    print("--- first 30 non-empty lines after click ---", flush=True)
    for ln in lines[:30]:
        print(f"  {ln[:70]!r}", flush=True)

    browser.close()
