"""Deep probe of xiaomi (Mioffice) pagination DOM + click test. No LLM."""
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

    print("=== elements with class*=next (tag, class, text, aria) ===", flush=True)
    locs = p.locator("[class*=next], [class*=Next]")
    n = locs.count()
    print(f"  count={n}", flush=True)
    for i in range(min(n, 8)):
        el = locs.nth(i)
        try:
            tag = el.evaluate("e => e.tagName")
            cls = el.get_attribute("class") or ""
            txt = (el.inner_text(timeout=400) or "").strip().replace("\n", " ")[:30]
            aria = el.get_attribute("aria-label") or el.get_attribute("aria-disabled") or ""
            dis = el.get_attribute("disabled")
        except Exception as e:
            tag = cls = txt = aria = dis = f"err:{e}"
        print(f"  [{i}] tag={tag} class={cls!r} text={txt!r} aria={aria!r} disabled={dis!r}", flush=True)

    print("=== elements with class*=pagination or pager ===", flush=True)
    locs2 = p.locator("[class*=pagination], [class*=Pagination], [class*=pager], [class*=Pager]")
    n2 = locs2.count()
    print(f"  count={n2}", flush=True)
    for i in range(min(n2, 10)):
        el = locs2.nth(i)
        try:
            tag = el.evaluate("e => e.tagName")
            cls = el.get_attribute("class") or ""
            txt = (el.inner_text(timeout=400) or "").strip().replace("\n", " ")[:40]
        except Exception as e:
            tag = cls = txt = f"err:{e}"
        print(f"  [{i}] tag={tag} class={cls!r} text={txt!r}", flush=True)

    print("=== numbered page buttons (1..16) ===", flush=True)
    # find elements whose text is exactly a number 1..20
    for num in (1, 2, 3, 16):
        loc = p.get_by_text(str(num), exact=True).first
        try:
            cnt = loc.count()
            if cnt:
                tag = loc.evaluate("e => e.tagName")
                cls = loc.get_attribute("class") or ""
                aria = loc.get_attribute("aria-current") or ""
                print(f"  num={num} count={cnt} tag={tag} class={cls!r} aria-current={aria!r}", flush=True)
        except Exception as e:
            print(f"  num={num} err={e}", flush=True)

    # which number is "current"/active?
    print("=== active/current page marker ===", flush=True)
    for sel in ("[aria-current='page']", "[class*=active][class*=pagination], [class*=active][class*=page]", "[class*=current][class*=page]"):
        try:
            cnt = p.locator(sel).count()
            if cnt:
                el = p.locator(sel).first
                txt = (el.inner_text(timeout=400) or "").strip()[:20]
                print(f"  sel={sel!r} count={cnt} text={txt!r}", flush=True)
        except Exception:
            pass

    # Try clicking the first [class*=next] that is not disabled, see if page changes
    print("=== click test: try [class*=next]:not(.disabled) variants ===", flush=True)
    before_hash = p.evaluate("() => document.body.innerText.length")
    print(f"  before body len={before_hash}", flush=True)
    for sel in ("[class*='next']:not([class*='disabled'])", "[class*='Next']:not([class*='disabled'])"):
        loc = p.locator(sel).first
        try:
            if loc.count() == 0:
                print(f"  {sel!r}: none", flush=True)
                continue
            loc.scroll_into_view_if_needed(timeout=2_000)
            loc.click(timeout=3_000, no_wait_after=True)
            p.wait_for_timeout(2_000)
            try:
                p.wait_for_load_state("networkidle", timeout=6_000)
            except Exception:
                pass
            after = p.evaluate("() => document.body.innerText.length")
            print(f"  {sel!r}: clicked. before={before_hash} after={after} changed={after != before_hash}", flush=True)
            break
        except Exception as e:
            print(f"  {sel!r}: err={e!r}", flush=True)

    b.close()
