"""Diagnostic: does _find_next_page_element find xiaomi's pager, and does the
click loop actually advance pages? Replicates the pagination loop with prints.
"""
from __future__ import annotations
import sys
from pathlib import Path
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
from backend.app.services.job_discovery.deepagents_runner import (  # noqa: E402
    _NAV_USER_AGENT,
    _capture_page_text,
    _dismiss_consent_dialog,
    _find_next_page_element,
    _is_pagination_wall,
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

    import hashlib
    _t, text0 = _capture_page_text(p)
    prev_hash = hashlib.sha256(text0.encode()).hexdigest() if text0.strip() else ""
    print(f"page1 body_len={len(text0)} wall={_is_pagination_wall(text0)}", flush=True)

    pages_captured = 0
    for i in range(15):
        nxt = _find_next_page_element(p)
        if nxt is None:
            print(f"  iter{i}: _find_next_page_element returned None -> stop", flush=True)
            break
        try:
            cls = nxt.get_attribute("class") or ""
            txt = (nxt.inner_text(timeout=400) or "").strip()
        except Exception:
            cls = txt = "?"
        # active page number before click
        try:
            act_before = (p.locator("[class*=active][class*=pagination], [class*=active][class*=page]").first.inner_text(timeout=500) or "").strip()
        except Exception:
            act_before = "?"
        print(f"  iter{i}: active_before={act_before!r} next class={cls!r}", flush=True)
        try:
            nxt.scroll_into_view_if_needed(timeout=3_000)
            nxt.click(timeout=3_000, no_wait_after=True)
            click_ok = True
            click_err = None
        except Exception as e:
            click_ok = False
            click_err = repr(e)
        print(f"    click ok={click_ok} err={click_err}", flush=True)
        if not click_ok:
            break
        # wait for networkidle, then poll for active page number to change
        try:
            p.wait_for_load_state("networkidle", timeout=8_000)
        except Exception:
            p.wait_for_timeout(2_000)
        # poll up to ~6s for the active page number to change
        act_after = act_before
        for _poll in range(12):
            p.wait_for_timeout(500)
            try:
                act_after = (p.locator("[class*=active][class*=pagination], [class*=active][class*=page]").first.inner_text(timeout=400) or "").strip()
            except Exception:
                act_after = "?"
            if act_after != act_before and act_after != "?":
                break
        _dismiss_consent_dialog(p)
        _pt, ptext = _capture_page_text(p)
        print(f"    active_after={act_after!r} body_len={len(ptext)}", flush=True)
        if not ptext.strip():
            print(f"    iter{i}: empty page -> stop", flush=True)
            break
        if _is_pagination_wall(ptext):
            print(f"    iter{i}: wall detected -> stop", flush=True)
            break
        cur_hash = hashlib.sha256(ptext.encode()).hexdigest()
        if cur_hash == prev_hash:
            print(f"    iter{i}: content unchanged (hash same) -> stop", flush=True)
            break
        prev_hash = cur_hash
        pages_captured += 1
        print(f"    iter{i}: captured page. pages_so_far={pages_captured}", flush=True)

    print(f"TOTAL paginated pages captured: {pages_captured}", flush=True)
    b.close()
