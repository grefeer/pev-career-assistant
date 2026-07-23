"""Diagnose why extract_rendered_job_evidence pagination stops early for pdd.

Steps the baseline pagination manually and reports, for each page transition:
  - which "next page" element was found (selector/text)
  - whether the click succeeded
  - whether the content-hash changed within the wait window

This isolates whether the stop is due to (a) next-element detection failure,
(b) a click error, or (c) content-change wait timeout.
"""
from __future__ import annotations

import hashlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from playwright.sync_api import sync_playwright

PDD_URL = "https://careers.pddglobalhr.com/campus/grad?t=AOT9z6aa0x"

from backend.app.services.job_discovery.deepagents_runner import (  # noqa: E402
    _NEXT_PAGE_SELECTORS,
    _NEXT_PAGE_TEXTS,
    _capture_page_text,
    _dismiss_consent_dialog,
    _find_next_page_element,
    _is_pagination_wall,
    _wait_for_list_page_change,
)


def step_pagination(page) -> None:
    title, text = _capture_page_text(page)
    prev_hash = hashlib.sha256(text.encode("utf-8")).hexdigest() if text.strip() else ""
    print(f"[page] title={title[:60]!r} text_len={len(text)} hash={prev_hash[:8]}")
    print(f"[pager selectors] {_NEXT_PAGE_SELECTORS}")
    print(f"[pager texts] {_NEXT_PAGE_TEXTS}")

    for i in range(5):
        next_el = _find_next_page_element(page)
        if next_el is None:
            print(f"[step {i}] _find_next_page_element -> None (stopping)")
            # debug: dump all candidate pager elements
            for tag in ("button", "a", "[role='button']", "li"):
                try:
                    locs = page.locator(tag)
                    n = locs.count()
                    for j in range(min(n, 30)):
                        loc = locs.nth(j)
                        try:
                            if not loc.is_visible(timeout=200):
                                continue
                            txt = (loc.inner_text(timeout=200) or "").strip()[:20]
                            cls = (loc.get_attribute("class") or "")[:40]
                            dis = loc.get_attribute("disabled")
                            print(f"    [{tag}#{j}] text={txt!r} class={cls!r} disabled={dis}")
                        except Exception:
                            continue
                except Exception:
                    continue
            break
        # report what we found
        try:
            tag_name = next_el.evaluate("e => e.tagName")
            txt = (next_el.inner_text(timeout=500) or "").strip()[:30]
        except Exception as exc:
            tag_name, txt = "?", f"<err {exc}>"
        print(f"[step {i}] found next element <{tag_name}> text={txt!r}")
        try:
            next_el.scroll_into_view_if_needed(timeout=3_000)
            next_el.click(timeout=3_000, no_wait_after=True)
            print(f"[step {i}] click OK")
        except Exception as exc:
            print(f"[step {i}] click FAILED: {type(exc).__name__}: {exc}")
            break
        _dismiss_consent_dialog(page)
        nt, ntext, nhash = _wait_for_list_page_change(page, prev_hash, max_wait_ms=12_000)
        changed = bool(ntext.strip())
        print(f"[step {i}] after click: changed={changed} new_len={len(ntext)} hash={nhash[:8]}")
        if not changed:
            print(f"[step {i}] content did NOT change -> would break here")
            break
        if _is_pagination_wall(ntext):
            print(f"[step {i}] pagination wall detected -> would break here")
            break
        prev_hash = nhash
        # count how many job-ish lines on this page
        jobs = [ln for ln in ntext.splitlines() if ln.strip() and len(ln.strip()) < 40]
        print(f"[step {i}] short lines on new page: {len(jobs)}")


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            viewport={"width": 1366, "height": 900},
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"),
        )
        page = ctx.new_page()
        page.goto(PDD_URL, wait_until="domcontentloaded", timeout=60_000)
        _dismiss_consent_dialog(page)
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass
        step_pagination(page)
        browser.close()


if __name__ == "__main__":
    main()
