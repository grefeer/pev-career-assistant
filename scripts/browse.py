#!/usr/bin/env python3
"""browse.py — Render a career site URL to plain text via Playwright.

Usage:
  browse.py <url> [--mode list|detail|interact] [--out <dir>] [--max-pages N]
              [--max-cards N] [--wait MS] [--ignore-cache]

Modes:
  list     — Listing/search page. Scrolls, paginates, collects visible text.
  detail   — Single job detail page. Opens URL, waits, returns body text.
  interact — Click-through mode: finds job cards on a list page, clicks each to
             reveal hidden detail panels/drawers, collects expanded text.

Output (stdout): JSON object with status, content_hash, text_path, screenshot_path.
Exit code 0 on success, 1 on error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# URL-level cache
# ---------------------------------------------------------------------------

def _load_cache(out_dir: Path) -> dict[str, str]:
    """Load the URL->content_hash cache file. Returns empty dict if missing."""
    cache_path = out_dir / "cache.json"
    if cache_path.exists():
        try:
            return json.loads(cache_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_cache(out_dir: Path, url: str, content_hash: str) -> None:
    """Update the cache mapping and persist."""
    cache = _load_cache(out_dir)
    cache[url] = content_hash
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "cache.json").write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def _check_cache(out_dir: Path, url: str) -> dict[str, Any] | None:
    """Return cached result if URL is cached AND content file still exists."""
    cache = _load_cache(out_dir)
    content_hash = cache.get(url)
    if not content_hash:
        return None
    text_path = out_dir / f"{content_hash}.txt"
    screenshot_path = out_dir / f"{content_hash}.png"
    if text_path.exists():
        text = text_path.read_text(encoding="utf-8")
        return {
            "status": "ok",
            "url": url,
            "content_hash": content_hash,
            "text_path": str(text_path),
            "screenshot_path": str(screenshot_path),
            "text_length": len(text),
            "cached": True,
        }
    return None


# ---------------------------------------------------------------------------
# Consent / GDPR dialog dismissal
# ---------------------------------------------------------------------------

_CONSENT_BUTTON_TEXTS = [
    "Accept", "Accept All", "Accept all", "Allow", "Agree",
    "同意", "接受", "允许", "我知道了", "确定", "好的", "知道了",
    "I agree", "OK", "Ok", "Got it", "Continue",
]

# Security hard gate: never click consent if page body contains any of these.
# These indicate login walls, captcha, anti-bot, or WeChat verification —
# clicking through would be circumventing a security measure.
_CONSENT_BLOCK_KEYWORDS = [
    "验证码", "captcha", "滑块", "登录", "扫码", "robot",
    "环境异常", "完成验证后即可继续访问",
    "请在微信客户端打开", "请长按识别二维码",
]


def _dismiss_consent(page: Any) -> bool:
    try:
        body_text = page.evaluate("() => document.body.innerText || ''")
        body_lower = body_text.lower()
        for kw in _CONSENT_BLOCK_KEYWORDS:
            if kw.lower() in body_lower:
                return False  # Security hard gate — do NOT interact
    except Exception:
        pass

    for text in _CONSENT_BUTTON_TEXTS:
        try:
            btn = page.get_by_text(text, exact=True).first
            if btn.is_visible():
                btn_text = btn.inner_text().lower()
                # Double-check: the button itself must not be a block keyword
                for kw in _CONSENT_BLOCK_KEYWORDS:
                    if kw.lower() in btn_text:
                        return False
                btn.click(timeout=3000)
                page.wait_for_timeout(1000)
                return True
        except Exception:
            continue
    return False


# ---------------------------------------------------------------------------
# Pagination detection
# ---------------------------------------------------------------------------

_NEXT_PAGE_TEXTS = ["Next", "Next Page", ">", "»", "下一页", "下一頁", "下页"]


def _find_next_page_button(page: Any) -> Any | None:
    for text in _NEXT_PAGE_TEXTS:
        try:
            btn = page.get_by_text(text, exact=True).first
            if btn.is_visible():
                return btn
        except Exception:
            continue
    for sel in [".next", ".pagination-next", "[aria-label='Next']", "[aria-label='next']",
                "a[rel='next']", "button.next-page", ".ant-pagination-next",
                ".el-pagination button:last-child", ".t-pagination__btn-next"]:
        try:
            el = page.locator(sel).first
            if el.is_visible():
                return el
        except Exception:
            continue
    return None


def _scroll_to_load(page: Any, wait_ms: int = 2000, rounds: int = 3) -> None:
    for _ in range(rounds):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(wait_ms)
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Text extraction & evidence saving
# ---------------------------------------------------------------------------

def _extract_body_text(page: Any) -> str:
    return page.evaluate("() => document.body.innerText || ''")


def _save_evidence(text: str, out_dir: Path) -> tuple[str, Path, Path]:
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    short_hash = f"sha256_{content_hash[:16]}"
    text_path = out_dir / f"{short_hash}.txt"
    screenshot_path = out_dir / f"{short_hash}.png"
    out_dir.mkdir(parents=True, exist_ok=True)
    if not text_path.exists():
        text_path.write_text(text, encoding="utf-8")
    return short_hash, text_path, screenshot_path


def _save_screenshot(page: Any, path: Path) -> None:
    try:
        page.screenshot(path=str(path), full_page=True)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Browse modes
# ---------------------------------------------------------------------------

def browse_list_mode(page: Any, url: str, out_dir: Path, max_pages: int, wait_ms: int) -> dict[str, Any]:
    all_texts: list[str] = []
    page_num = 1

    _scroll_to_load(page, wait_ms)
    for _ in range(3):
        _dismiss_consent(page)
    all_texts.append(_extract_body_text(page))

    while page_num < max_pages:
        next_btn = _find_next_page_button(page)
        if next_btn is None:
            break
        try:
            old_url = page.url
            next_btn.click(timeout=5000)
            page.wait_for_timeout(wait_ms)
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            if page.url == old_url:
                new_text = _extract_body_text(page)
                if new_text == all_texts[-1]:
                    break
                all_texts.append(new_text)
            else:
                _scroll_to_load(page, wait_ms)
                all_texts.append(_extract_body_text(page))
            page_num += 1
        except Exception:
            break

    full_text = "\n\n--- PAGE BREAK ---\n\n".join(all_texts)
    short_hash, text_path, screenshot_path = _save_evidence(full_text, out_dir)
    _save_screenshot(page, screenshot_path)

    return {
        "status": "ok",
        "url": url,
        "title": page.title(),
        "content_hash": short_hash,
        "text_path": str(text_path),
        "screenshot_path": str(screenshot_path),
        "text_length": len(full_text),
        "pagination": {"pages_collected": page_num, "max_allowed": max_pages},
    }


def browse_detail_mode(page: Any, url: str, out_dir: Path, wait_ms: int) -> dict[str, Any]:
    _scroll_to_load(page, wait_ms)
    for _ in range(3):
        _dismiss_consent(page)
    page.wait_for_timeout(wait_ms)

    text = _extract_body_text(page)
    short_hash, text_path, screenshot_path = _save_evidence(text, out_dir)
    _save_screenshot(page, screenshot_path)

    return {
        "status": "ok",
        "url": url,
        "title": page.title(),
        "content_hash": short_hash,
        "text_path": str(text_path),
        "screenshot_path": str(screenshot_path),
        "text_length": len(text),
    }


# ---------------------------------------------------------------------------
# Interactive mode — click cards to reveal hidden JDs
# ---------------------------------------------------------------------------

# Common selectors for job card elements that trigger detail views
_CARD_CLICK_SELECTORS = [
    # Moka-style: clickable job titles / cards
    "a.job-title", "a.position-name", ".job-card a", ".job-item a",
    "[class*='job-card'] a", "[class*='JobCard'] a", "[class*='position'] a",
    ".card a[href]", ".list-item a[href]",
    # Generic fallback: clickable elements inside visible card areas
    ".job-card", ".job-item", ".position-item", "[class*='job-card']", "[class*='JobCard']",
    "li a[href*='job']", "li a[href*='position']",
    # zhiye.com pattern
    "a[href*='jobDetail']", "a[href*='position_detail']",
    # Text-based fallback for Moka — buttons with "投递" or "查看"
    "button:has-text('查看')", "button:has-text('详情')", "a:has-text('查看')",
    # Moka category headers that expand to reveal cards
    "a:has-text('个职位')", "div:has-text('个职位')", "span:has-text('个职位')",
    "[class*='category'] a", "[class*='Category'] a", "[class*='tab'] a",
    ".recruit-list a", "[class*='recruit'] a",
]

# Category/section elements that need clicking to reveal job cards
_CATEGORY_EXPAND_SELECTORS = [
    "a:has-text('个职位')", "div:has-text('个职位')", "span:has-text('个职位')",
    "[class*='category']", "[class*='Category']", "[class*='tab']",
    "[class*='recruit-type']", "[class*='type-item']", "[class*='job-category']",
    "li:has-text('个职位')", ".position-category",
]

# Selectors for detail panel content after clicking
_DETAIL_CONTENT_SELECTORS = [
    ".job-detail", ".position-detail", ".job-desc", ".detail-content",
    "[class*='detail']", "[class*='Detail']", ".drawer-content",
    ".modal-body", ".popup-content", "[role='dialog']",
    ".job-info", ".position-info", ".jd-content",
]

# Buttons to close detail panels
_CLOSE_BUTTON_SELECTORS = [
    ".close", ".drawer-close", ".modal-close", "[aria-label='Close']",
    "[aria-label='close']", "button:has-text('×')", ".ant-drawer-close",
    ".el-drawer__close", ".moka-drawer-close",
]


def _expand_categories(page: Any, wait_ms: int) -> int:
    """Click category/section headers to reveal hidden job cards. Returns count clicked."""
    clicked = 0
    for sel in _CATEGORY_EXPAND_SELECTORS:
        try:
            elements = page.locator(sel).all()
            for el in elements:
                try:
                    if not el.is_visible():
                        continue
                    text = el.inner_text()
                    if not text.strip():
                        continue
                    # Click to expand this category
                    el.click(timeout=3000)
                    page.wait_for_timeout(wait_ms)
                    clicked += 1
                except Exception:
                    continue
        except Exception:
            continue
    if clicked > 0:
        page.wait_for_timeout(wait_ms)
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
    return clicked


def _find_clickable_cards_js(page: Any, max_cards: int) -> list[dict[str, Any]]:
    """Use JavaScript to find clickable job-card elements. Returns list of {tag, text, selector}.

    Only returns elements whose text looks like a job title or card — filters out
    navigation, pagination, and filter controls.
    """
    js_code = """
    () => {
        const results = [];
        const clickables = document.querySelectorAll('a, button, [role="button"]');
        const seen = new Set();

        // Patterns that suggest a job card or job title (Chinese + English)
        const jobPatterns = [
            /届/, /校招/, /社招/, /实习/, /全职/, /提前批/, /内推/,
            /工程师/, /经理/, /专员/, /算法/, /开发/, /产品/, /设计/, /运营/,
            /Engineer/i, /Manager/i, /Developer/i, /Intern/i, /Scientist/i,
            /发布于/, /岗位/, /职位/,
        ];
        // Skip patterns for nav/filter/ui chrome
        const skipPatterns = [
            /^\\s*$/, /^(首页|末页|登录|注册|搜索|筛选|清除|确定|取消|知道了|提交|保存)$/,
            /^(上一页|下一页|首页|末页|Home|Login|Search|Filter|Clear|Apply|Submit)$/,
            /^\\d+$/, /^(1|2|3|4|5)$/, /行\\/页/, /前往/,
            /^\\+\\d+$/,  // "+0" etc
        ];

        for (const el of clickables) {
            const text = (el.innerText || el.textContent || '').trim();
            if (!text || text.length < 3 || text.length > 200) continue;
            if (seen.has(text)) continue;

            // Must match at least one job pattern AND not match any skip pattern
            const isJob = jobPatterns.some(p => p.test(text));
            const shouldSkip = skipPatterns.some(p => p.test(text));
            if (!isJob || shouldSkip) continue;

            let selector = '';
            if (el.id) selector = '#' + el.id;
            else if (el.className && typeof el.className === 'string') {
                const cls = el.className.trim().split(/\\s+/)[0];
                if (cls) selector = el.tagName.toLowerCase() + '.' + cls;
            }
            if (!selector) selector = el.tagName.toLowerCase();

            seen.add(text);
            results.push({tag: el.tagName.toLowerCase(), text: text, selector: selector});
            if (results.length >= 80) break;
        }
        return results;
    }
    """
    try:
        raw = page.evaluate(js_code)
        return raw[:max_cards] if isinstance(raw, list) else []
    except Exception:
        return []


def _find_clickable_cards(page: Any, max_cards: int) -> list[Any]:
    """Find clickable job card elements on the page. Tries CSS selectors first,
    then falls back to JS-based element discovery."""
    candidates: list[Any] = []

    for sel in _CARD_CLICK_SELECTORS:
        try:
            elements = page.locator(sel).all()
            for el in elements:
                try:
                    if el.is_visible():
                        candidates.append(el)
                except Exception:
                    continue
            if len(candidates) >= max_cards:
                break
        except Exception:
            continue

    # If CSS selectors found nothing, try JS-based discovery and convert to locators
    if not candidates:
        js_cards = _find_clickable_cards_js(page, max_cards * 2)
        for card_info in js_cards:
            try:
                # Try by text first (most reliable for SPA components)
                el = page.get_by_text(card_info["text"], exact=True).first
                if el.is_visible():
                    candidates.append(el)
                    continue
            except Exception:
                pass
            try:
                # Try by selector
                sel = card_info.get("selector", "")
                if sel:
                    el = page.locator(sel).first
                    if el.is_visible():
                        candidates.append(el)
            except Exception:
                continue
            if len(candidates) >= max_cards:
                break

    # Deduplicate by bounding box (roughly)
    unique: list[Any] = []
    seen_boxes: list[tuple[float, float, float, float]] = []
    for el in candidates:
        try:
            box = el.bounding_box()
            if box is None:
                unique.append(el)  # Can't get box, include anyway (JS-based elements)
                continue
            key = (round(box["x"], -1), round(box["y"], -1),
                   round(box["x"] + box["width"], -1), round(box["y"] + box["height"], -1))
            is_dup = False
            for sb in seen_boxes:
                if (abs(key[0] - sb[0]) < 30 and abs(key[1] - sb[1]) < 30 and
                        abs(key[2] - sb[2]) < 30 and abs(key[3] - sb[3]) < 30):
                    is_dup = True
                    break
            if not is_dup:
                seen_boxes.append(key)
                unique.append(el)
        except Exception:
            continue
        if len(unique) >= max_cards:
            break

    return unique[:max_cards]


def _extract_detail_text(page: Any) -> str:
    """Try to extract text from detail panel/drawer content."""
    for sel in _DETAIL_CONTENT_SELECTORS:
        try:
            panel = page.locator(sel).first
            if panel.is_visible():
                text = panel.inner_text()
                if len(text) > 50:  # meaningful content threshold
                    return text
        except Exception:
            continue
    # Fallback: body text (may include card list + detail)
    return _extract_body_text(page)


def _close_detail_panel(page: Any) -> None:
    """Try to close any open detail panel/drawer."""
    for sel in _CLOSE_BUTTON_SELECTORS:
        try:
            btn = page.locator(sel).first
            if btn.is_visible():
                btn.click(timeout=3000)
                page.wait_for_timeout(1500)
                return
        except Exception:
            continue
    # Fallback: press Escape
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(1000)
    except Exception:
        pass


def browse_interact_mode(
    page: Any, url: str, out_dir: Path, max_cards: int, wait_ms: int
) -> dict[str, Any]:
    """Click through job cards on a list page, collecting detail text from each."""

    # First, capture the list page baseline text
    _scroll_to_load(page, wait_ms)
    for _ in range(3):
        _dismiss_consent(page)
    page.wait_for_timeout(wait_ms)

    # Expand category sections (Moka pattern: "X-STAR顶尖人才 共3个职位")
    cats_clicked = _expand_categories(page, wait_ms)

    list_text = _extract_body_text(page)

    # Find clickable cards
    cards = _find_clickable_cards(page, max_cards)
    if not cards:
        # No cards found — return list text as-is
        short_hash, text_path, screenshot_path = _save_evidence(list_text, out_dir)
        _save_screenshot(page, screenshot_path)
        return {
            "status": "ok",
            "url": url,
            "title": page.title(),
            "content_hash": short_hash,
            "text_path": str(text_path),
            "screenshot_path": str(screenshot_path),
            "text_length": len(list_text),
            "cards_clicked": 0,
            "cards_found": 0,
            "categories_expanded": cats_clicked,
            "note": "No clickable job cards detected; returned list page text. "
                    "Try --mode list or manual exploration with playwright skill.",
        }

    # Click each card and collect detail text
    detail_sections: list[str] = [f"=== LIST PAGE ===\n{list_text}"]
    clicked = 0
    failed = 0
    start_time = time.time()
    time_budget = 120  # Max 2 minutes total for interact mode

    for i, card in enumerate(cards):
        if time.time() - start_time > time_budget:
            detail_sections.append(f"\n=== TIMEOUT: stopped after {clicked} cards ({failed} failed) ===")
            break
        try:
            # Scroll card into view
            card.scroll_into_view_if_needed()
            page.wait_for_timeout(500)

            # Capture pre-click body text for change detection
            pre_text = _extract_body_text(page)

            # Click the card
            card.click(timeout=3000)
            page.wait_for_timeout(min(wait_ms, 2000))
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass

            # Check if a detail panel appeared OR page navigated
            post_text = _extract_body_text(page)

            # If URL changed, we navigated to a detail page
            if page.url != url:
                # We're on a detail page — extract and go back
                detail_text = _extract_detail_text(page)
                detail_sections.append(f"\n=== JOB {i + 1} ({page.url}) ===\n{detail_text}")
                page.go_back(timeout=10000)
                page.wait_for_timeout(wait_ms)
                try:
                    page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass
                clicked += 1
            elif len(post_text) > len(pre_text) + 50:
                # New content appeared (detail panel/drawer)
                detail_text = _extract_detail_text(page)
                detail_sections.append(f"\n=== JOB {i + 1} ===\n{detail_text}")
                _close_detail_panel(page)
                page.wait_for_timeout(1000)
                clicked += 1
            else:
                # No visible change — card might not be interactive
                failed += 1

        except Exception:
            failed += 1
            # Try to recover: go back to list page if navigated away
            try:
                if page.url != url:
                    page.goto(url, wait_until="domcontentloaded", timeout=15000)
                    page.wait_for_timeout(wait_ms)
            except Exception:
                pass
            continue

    full_text = "\n".join(detail_sections)
    short_hash, text_path, screenshot_path = _save_evidence(full_text, out_dir)
    _save_screenshot(page, screenshot_path)

    return {
        "status": "ok",
        "url": url,
        "title": page.title(),
        "content_hash": short_hash,
        "text_path": str(text_path),
        "screenshot_path": str(screenshot_path),
        "text_length": len(full_text),
        "cards_clicked": clicked,
        "cards_found": len(cards),
        "cards_failed": failed,
        "categories_expanded": cats_clicked,
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Render career site to plain text via Playwright")
    parser.add_argument("url", help="Career site URL to browse")
    parser.add_argument("--mode", choices=["list", "detail", "interact"], default="list",
                        help="Page type: list, detail, or interact (click-through cards)")
    parser.add_argument("--out", default="output/evidence",
                        help="Output directory for evidence files (default: output/evidence)")
    parser.add_argument("--max-pages", type=int, default=5,
                        help="Max pages to paginate in list mode (default: 5)")
    parser.add_argument("--max-cards", type=int, default=50,
                        help="Max job cards to click in interact mode (default: 50)")
    parser.add_argument("--wait", type=int, default=3000,
                        help="Wait time in ms after page load/scroll/click (default: 3000)")
    parser.add_argument("--ignore-cache", action="store_true",
                        help="Skip cache check, always re-fetch the page")

    args = parser.parse_args()
    url = args.url
    out_dir = Path(args.out)

    # ---- Cache check (before browser launch) ----
    if not args.ignore_cache:
        cached = _check_cache(out_dir, url)
        if cached is not None:
            print(json.dumps(cached, ensure_ascii=False))
            sys.exit(0)

    try:
        from playwright.sync_api import sync_playwright, Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError
    except ImportError:
        result = {
            "status": "error",
            "url": url,
            "error": "Playwright is not installed. Run: pip install playwright && playwright install chromium",
        }
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(1)

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1920, "height": 1080},
            )
            page = context.new_page()

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(args.wait)
                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
            except PlaywrightTimeoutError:
                result = {
                    "status": "error",
                    "url": url,
                    "error": "Timeout loading page (30s)",
                }
                print(json.dumps(result, ensure_ascii=False))
                browser.close()
                sys.exit(1)
            except PlaywrightError as e:
                result = {
                    "status": "blocked",
                    "url": url,
                    "error": f"Navigation failed: {e}",
                }
                print(json.dumps(result, ensure_ascii=False))
                browser.close()
                sys.exit(1)

            if args.mode == "detail":
                result = browse_detail_mode(page, url, out_dir, args.wait)
            elif args.mode == "interact":
                result = browse_interact_mode(page, url, out_dir, args.max_cards, args.wait)
            else:
                result = browse_list_mode(page, url, out_dir, args.max_pages, args.wait)

            # Persist cache entry
            ch = result.get("content_hash")
            if ch:
                _save_cache(out_dir, url, ch)

            browser.close()
            print(json.dumps(result, ensure_ascii=False))
            sys.exit(0)

    except Exception as exc:
        result = {
            "status": "error",
            "url": url,
            "error": str(exc),
        }
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    main()
