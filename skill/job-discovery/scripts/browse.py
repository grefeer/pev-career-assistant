#!/usr/bin/env python3
"""browse.py — Render a career site URL to plain text via Playwright.

Usage:
  browse.py <url> [--mode list|detail|interact|search|search-interact|click|parallel-fetch] [--out <dir>]
              [--max-pages N] [--max-cards N] [--wait MS]
              [--cache-mode use|revalidate|off] [--ignore-cache]
              [--search-terms TERMS] [--search-strategy first_match|each|broad]
              [--fallback full|none]

Modes:
  list     — Listing/search page. Scrolls, paginates, collects visible text.
  detail   — Single job detail page. Opens URL, waits, returns body text.
  interact — Click-through mode: finds job cards on a list page, clicks each to
             reveal hidden detail panels/drawers, collects expanded text.
  search   — Search-first mode: finds a search box on the page, enters keywords,
             then browses only the filtered results. Falls back to full list mode
             if search is unavailable or produces zero results (with --fallback full).
             Use --search-terms for comma-separated keywords, --search-strategy
             to control how multiple terms are tried.
  click          - Agent-driven pagination: click a target (--click-text /
                   --click-selector / --click-auto) up to --click-count times.
  parallel-fetch - v1.6 fast path: detect URL-keyed pagination (click next -> read URL
                   -> click prev -> read URL -> diff to find page/size params), pre-compute
                   all page URLs, fetch concurrently via a thread pool (--concurrency),
                   write page_01..NN.txt. Auto-falls back to click mode when URL-keyed
                   pagination is not detectable or a fetch errors (3 retries / timeout).
                   Returns status=blocked (not fallback) on a captcha/anti-bot wall.

Cache modes:
  use         — Return cached result if URL+hash match (default, fastest).
  revalidate  — Always re-browse; compare new content_hash against cache.
                Returns cached result only if content_hash matches (page unchanged).
  off         — Never use cache; always re-browse and overwrite evidence.
  --ignore-cache  — Deprecated alias for --cache-mode off.

Output (stdout): JSON object with status, content_hash, text_path, screenshot_path.
Exit code 0 on success, 1 on error.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import math
import re
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


_PUBLIC_JOB_TITLE_KEYS = ("title", "name", "positionName", "jobTitle")
_PUBLIC_JOB_BODY_KEYS = (
    "jobDuty", "jobDescription", "description", "responsibilities",
    "requirements", "requirement", "content", "duty",
)
_PUBLIC_JOB_TOTAL_KEYS = ("total", "totalCount", "totalElements", "count")


def is_safe_public_url(value: str) -> bool:
    """Allow only public HTTP(S) targets, including after DNS resolution."""
    try:
        parsed = urlparse(value)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return False
        if parsed.username is not None or parsed.password is not None:
            return False
        ip = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        return _hostname_resolves_only_to_public_ips(parsed.hostname) if "parsed" in locals() else False
    return ip.is_global


def _hostname_resolves_only_to_public_ips(hostname: str | None) -> bool:
    if not hostname:
        return False
    try:
        addresses = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False
    resolved: set[str] = {entry[4][0] for entry in addresses if entry[4]}
    if not resolved:
        return False
    try:
        return all(ipaddress.ip_address(address).is_global for address in resolved)
    except ValueError:
        return False


def install_public_network_guard(context: Any) -> None:
    """Abort browser requests whose final hostname is not publicly routable.

    The guard runs on every request, so redirects and model-discovered detail
    links get the same policy check as the source URL.
    """
    def guard(route: Any) -> None:
        scheme = urlparse(route.request.url).scheme.lower()
        if scheme in {"http", "https"} and not is_safe_public_url(route.request.url):
            route.abort()
            return
        route.continue_()

    context.route("**/*", guard)


class PublicJobEvidenceCollector:
    """Capture only job-shaped records from JSON the public page already loads.

    The collector never creates requests or guesses endpoints.  It observes
    Playwright responses for the current public page, retains only records with
    both a title and substantial JD text, and serializes a compact evidence
    projection.  This makes API-backed career pages fast without becoming a
    site adapter.
    """

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []
        self.expected_count: int | None = None
        self._seen: set[str] = set()

    def attach(self, page: Any) -> None:
        page.on("response", self._on_response)

    def _on_response(self, response: Any) -> None:
        try:
            content_type = str(response.headers.get("content-type") or "").lower()
            if "json" not in content_type:
                return
            self.feed_payload(response.json())
        except Exception:
            return

    def feed_payload(self, payload: Any) -> None:
        for mapping in _walk_json_mappings(payload):
            for total_key in _PUBLIC_JOB_TOTAL_KEYS:
                value = mapping.get(total_key)
                try:
                    number = int(value)
                except (TypeError, ValueError):
                    continue
                if 0 < number <= 10000:
                    self.expected_count = max(self.expected_count or 0, number)
            record = _public_job_record(mapping)
            if record is None:
                continue
            identity = "|".join(str(record.get(key) or "") for key in ("id", "title", "location"))
            if identity in self._seen:
                continue
            self._seen.add(identity)
            self.records.append(record)

    def evidence_text(self) -> str:
        sections = ["=== PUBLIC JSON JOB EVIDENCE ==="]
        for index, record in enumerate(self.records, start=1):
            sections.append(
                f"=== PUBLIC JOB {index} ===\n" + json.dumps(record, ensure_ascii=False)
            )
        return "\n".join(sections)


def _walk_json_mappings(value: Any) -> list[dict[str, Any]]:
    """Iteratively visit JSON mappings without retaining arbitrary payloads."""
    found: list[dict[str, Any]] = []
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, dict):
            found.append(current)
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)
    return found


def _public_job_record(mapping: dict[str, Any]) -> dict[str, Any] | None:
    title = next((str(mapping.get(key) or "").strip() for key in _PUBLIC_JOB_TITLE_KEYS if mapping.get(key)), "")
    body = next((str(mapping.get(key) or "").strip() for key in _PUBLIC_JOB_BODY_KEYS if mapping.get(key)), "")
    if not title or len(body) < 50:
        return None
    return {
        "id": mapping.get("id") or mapping.get("code"),
        "title": title,
        "department": mapping.get("department") or mapping.get("jobName"),
        "location": mapping.get("workLocationName") or mapping.get("workLocation") or mapping.get("location"),
        "responsibilities": body,
    }


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


def _check_cache(out_dir: Path, url: str, cache_mode: str) -> dict[str, Any] | None:
    """Return cached result if URL is cached AND content file still exists.

    Args:
        cache_mode:
            "use" — Return cached result immediately (no browser launch).
            "revalidate" — Always re-browse; compare content_hash after.
            "off" — Never return cached.
    """
    if cache_mode == "off":
        return None
    if cache_mode == "revalidate":
        return None  # Always re-browse, cache check happens post-browse
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

# Load-more / view-more buttons: content-appending pagination common on SPAs
# that have NO 'next page' control (the page grows in place, URL unchanged).
# e.g. mokahr '查看更多职位' (deeproute) exposes the full list with one click.
# Tried LAST in _find_next_page_button (after next-page texts AND CSS selectors)
# so a real next-page match always wins; only sites with neither fall through.
# exact=False so trailing chevrons / counts like '查看更多职位>>' or '加载更多(10)'
# still match the long phrase.
_LOAD_MORE_TEXTS = [
    "查看更多职位", "查看更多岗位", "查看更多职位信息", "查看全部职位", "查看全部岗位",
    "加载更多", "加载更多职位", "更多职位", "更多岗位", "展开更多", "展开全部",
    "View more positions", "View more jobs", "Load more", "Show more", "See more jobs",
]


# ---------------------------------------------------------------------------
# Search mode — keyword filtering via search boxes
# ---------------------------------------------------------------------------

# Priority-ordered selectors for search input fields across common career platforms.
# Ordered from most-specific (least false positives) to generic fallback.
_SEARCH_INPUT_SELECTORS = [
    # Moka-style
    "input[placeholder*='搜索职位']",
    "input[placeholder*='搜索岗位']",
    # zhiye.com
    "input[placeholder*='请输入职位']",
    "input[placeholder*='请输入岗位']",
    "input[placeholder*='职位名称']",
    "input[placeholder*='岗位名称']",
    # Feishu / generic
    "input[placeholder*='Search']",
    "input[placeholder*='search']",
    "input[placeholder*='搜索']",
    "input[placeholder*='关键词']",
    "input[placeholder*='关键字']",
    # Semantic / aria
    "input[aria-label*='search']",
    "input[aria-label*='搜索']",
    "input[type='search']",
    # CSS class patterns
    "[class*='search'] input[type='text']",
    "[class*='Search'] input[type='text']",
    "[class*='search-input']",
    ".ant-input-search input",
    ".el-input__inner[placeholder*='搜索']",
    # Broad last-resort fallback — only matched if nothing above worked
    "input[type='text']:not([placeholder*='邮箱']):not([placeholder*='手机']):not([placeholder*='电话'])",
]

# Priority-ordered selectors for search submit buttons.
_SEARCH_BUTTON_SELECTORS = [
    "button:has-text('搜索')",
    "button:has-text('Search')",
    "a:has-text('搜索')",
    "[aria-label*='搜索']",
    "[aria-label*='search']",
    ".search-btn", ".search-button",
    "[class*='search'] button",
    "button[type='submit']",
]

# Results-count indicators for validating that search actually filtered.
_RESULT_COUNT_SELECTORS = [
    "[class*='result-count']", "[class*='total']",
    "[class*='count']", "[class*='Count']",
    "span:has-text('个职位')", "span:has-text('条结果')",
    "div:has-text('个职位')", ":has-text('个职位')",
]

# Default search timeout (ms) for waiting after search action.
_SEARCH_WAIT_MS = 2000
_SEARCH_TYPE_DELAY_MS = 100


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
                ".el-pagination button:last-child", ".t-pagination__btn-next",
                # Mioffice / atsx design system (xiaomi, bytedance, etc.)
                ".atsx-pagination-next", ".atsx-pagination-next a",
                # Generic fallbacks
                "[class*='pagination-next']:not([class*='prev'])",
                "li[class*='page-next']"]:
        try:
            el = page.locator(sel).first
            if el.is_visible():
                return el
        except Exception:
            continue
    # Load-more / view-more buttons (content-appending SPAs with no next-page
    # control). Tried LAST so a real next-page text/CSS match always wins; only
    # sites with neither (e.g. mokahr '查看更多职位') fall through to here.
    for text in _LOAD_MORE_TEXTS:
        try:
            btn = page.get_by_text(text, exact=False).first
            if btn.is_visible():
                return btn
        except Exception:
            continue
    return None


_PREV_PAGE_TEXTS = ["Prev", "Previous", "Previous Page", "<", "«", "上一页", "上一頁", "上页"]


def _find_prev_page_button(page: Any) -> Any | None:
    """Mirror of ``_find_next_page_button`` for the previous-page control.

    Used by the v1.6 pagination-detection flow to click back to page 1 after
    probing page 2. Falls back to ``page.go_back()`` in the caller if no prev
    control is found (browser back button reliably returns to the prior URL for
    URL-keyed pagination).
    """
    for text in _PREV_PAGE_TEXTS:
        try:
            btn = page.get_by_text(text, exact=True).first
            if btn.is_visible():
                return btn
        except Exception:
            continue
    for sel in [".prev", ".pagination-prev", "[aria-label='Previous']", "[aria-label='previous']",
                "a[rel='prev']", "button.prev-page", ".ant-pagination-prev",
                ".el-pagination button:first-child", ".t-pagination__btn-prev",
                ".atsx-pagination-prev", ".atsx-pagination-prev a",
                "[class*='pagination-prev']:not([class*='next'])",
                "li[class*='page-prev']"]:
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
            page.wait_for_load_state(
                "networkidle", timeout=_card_interaction_idle_timeout_ms(wait_ms)
            )
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


def _save_page_files(all_texts: list[str], out_dir: Path) -> list[str]:
    """Stash each collected page's text as its own numbered file.

    Writes ``page_01.txt`` ... ``page_NN.txt`` under ``<out_dir>/pages/``. Each
    file holds one page's body text (no ``--- PAGE BREAK ---`` separators), so a
    downstream extractor sub-agent can read a single small file and pull JDs
    from just that page instead of re-reading the whole concatenated blob. This
    is the on-disk backing for per-page progressive disclosure + parallel
    per-page extraction.

    ``all_texts`` already contains only changed pages (identical pages are
    skipped by the pagination loops), so each numbered file is unique content.
    Returns the list of written file paths (strings), in page order.
    """
    if not all_texts:
        return []
    pages_dir = out_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    paths: list[str] = []
    for idx, text in enumerate(all_texts, start=1):
        p = pages_dir / f"page_{idx:02d}.txt"
        p.write_text(text, encoding="utf-8")
        paths.append(str(p))
    return paths


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
    terminal_evidence: str | None = None

    _scroll_to_load(page, wait_ms)
    for _ in range(3):
        _dismiss_consent(page)
    all_texts.append(_extract_body_text(page))

    while page_num < max_pages:
        next_btn = _find_next_page_button(page)
        if next_btn is None:
            terminal_evidence = "next_control_absent"
            break
        try:
            old_url = page.url
            next_btn.click(timeout=5000)
            page.wait_for_timeout(wait_ms)
            try:
                page.wait_for_load_state("networkidle", timeout=4000)
            except Exception:
                pass
            if page.url == old_url:
                new_text = _extract_body_text(page)
                if new_text == all_texts[-1]:
                    terminal_evidence = "page_content_repeated"
                    break
                all_texts.append(new_text)
            else:
                _scroll_to_load(page, wait_ms)
                all_texts.append(_extract_body_text(page))
            page_num += 1
        except Exception:
            break

    full_text = "\n\n--- PAGE BREAK ---\n\n".join(all_texts)
    page_files = _save_page_files(all_texts, out_dir)
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
        "terminal_evidence": terminal_evidence,
        "truncated_by_max_pages": terminal_evidence is None and page_num >= max_pages,
        "page_count": len(all_texts),
        "page_files": page_files,
    }


# ---------------------------------------------------------------------------
# Agent-driven pagination (click mode)
# ---------------------------------------------------------------------------

# Security gate: refuse to click a target whose text looks like a login /
# captcha / anti-bot control. The agent is told not to bypass these, but this is
# a code-level backstop so a misbehaving prompt cannot drive the browser into a
# login or verification flow. Paginator text ("加载更多" / "下一页" / ">" / numbers)
# never contains these.
_CLICK_BLOCK_KEYWORDS = [
    "验证码", "captcha", "滑块", "登录", "登 录", "扫码", "robot",
    "环境异常", "完成验证后即可继续访问",
    "请在微信客户端打开", "请长按识别二维码",
]


def _find_click_target(page: Any, click_text: str | None, click_selector: str | None) -> Any | None:
    """Locate the agent-specified click target. Selector wins over text.

    Returns the first visible matching element, or None.
    """
    if click_selector:
        try:
            el = page.locator(click_selector).first
            if el.is_visible():
                return el
        except Exception:
            pass
    if click_text:
        try:
            el = page.get_by_text(click_text, exact=False).first
            if el.is_visible():
                return el
        except Exception:
            pass
    return None


def browse_click_mode(
    page: Any,
    url: str,
    out_dir: Path,
    click_text: str | None,
    click_selector: str | None,
    click_count: int,
    wait_ms: int,
    click_auto: bool = False,
) -> dict[str, Any]:
    """Agent-driven pagination: click a target N times, collecting text per click.

    The page is already loaded. Click the agent-specified target (by visible text
    or CSS selector) up to ``click_count`` times, extracting body text after each
    click. Handles both "next page" buttons (URL changes) and "load more" buttons
    (content appends in place). Stops early when the target disappears or the page
    stops changing (end of pagination).

    The agent chooses the target by reading [PAGE_TEXT] from a prior ``list`` call
    and passing the paginator's visible text (e.g. "加载更多", "下一页") or a CSS
    selector. This lifts the hardcoded-selector limitation of ``list`` mode for
    SPAs (e.g. Mioffice) whose paginator ``_find_next_page_button`` does not match.

    ``click_auto``: when the paginator is an icon-only arrow with no clickable text
    (Mioffice/atsx), pass ``--click-auto`` instead and browse will re-detect the
    next-page arrow each iteration via the same selector set as list mode.

    Security: a target whose text contains a login/captcha/anti-bot keyword is
    refused (returns status=blocked) so the browser is never driven into a login
    or verification flow.
    """
    if click_auto and not click_text and not click_selector:
        target_resolver = lambda: _find_next_page_button(page)  # noqa: E731
    elif click_text or click_selector:
        target_resolver = lambda: _find_click_target(page, click_text, click_selector)  # noqa: E731
    else:
        return {
            "status": "error",
            "url": url,
            "mode": "click",
            "error": "click mode requires --click-text, --click-selector, or --click-auto",
        }
    # Security backstop: never click a login/captcha/anti-bot control.
    for tgt_text in (click_text, click_selector):
        if not tgt_text:
            continue
        low = tgt_text.lower()
        for kw in _CLICK_BLOCK_KEYWORDS:
            if kw.lower() in low:
                return {
                    "status": "blocked",
                    "url": url,
                    "mode": "click",
                    "reason": (
                        f"click target {tgt_text!r} looks like login/captcha/"
                        "anti-bot - refused (security hard gate)"
                    ),
                }

    all_texts: list[str] = [_extract_body_text(page)]
    clicks_effective = 0
    end_reason = ""

    for _ in range(click_count):
        target = target_resolver()
        if target is None:
            end_reason = "click target not found (end of pagination, or wrong text/selector)"
            break
        try:
            target.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            pass
        try:
            target.click(timeout=5000)
            page.wait_for_timeout(wait_ms)
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            _scroll_to_load(page, wait_ms)
            for _ in range(3):
                _dismiss_consent(page)
            new_text = _extract_body_text(page)
            if new_text == all_texts[-1]:
                end_reason = "page did not change after click (end reached)"
                break
            all_texts.append(new_text)
            clicks_effective += 1
        except Exception as exc:
            end_reason = f"click failed: {exc}"
            break

    full_text = "\n\n--- PAGE BREAK ---\n\n".join(all_texts)
    page_files = _save_page_files(all_texts, out_dir)
    short_hash, text_path, screenshot_path = _save_evidence(full_text, out_dir)
    _save_screenshot(page, screenshot_path)

    return {
        "status": "ok",
        "url": url,
        "mode": "click",
        "click_text": click_text,
        "click_selector": click_selector,
        "click_auto": click_auto,
        "title": page.title(),
        "content_hash": short_hash,
        "text_path": str(text_path),
        "screenshot_path": str(screenshot_path),
        "text_length": len(full_text),
        "clicks_requested": click_count,
        "clicks_effective": clicks_effective,
        "pages_collected": len(all_texts),
        "end_reached": clicks_effective < click_count,
        "end_reason": end_reason,
        # A missing auto-detected paginator or a no-change click is an observed
        # terminal signal.  A selector/text chosen by an agent can be wrong, so
        # its absence is deliberately NOT treated as proof of completion.
        "terminal_evidence": (
            "next_control_absent" if click_auto and "target not found" in end_reason
            else "page_content_repeated" if "did not change" in end_reason
            else None
        ),
        "truncated_by_max_pages": not end_reason and clicks_effective >= click_count,
        "page_count": len(all_texts),
        "page_files": page_files,
        "listing_count": (
            int(value) if (value := _scan_body_count(all_texts[-1])) is not None else None
        ),
        "jd_detail_evidence": _has_jd_detail_evidence(full_text),
    }


# ---------------------------------------------------------------------------
# Parallel URL fetch (v1.6) - detect URL-keyed pagination, pre-compute all
# page URLs, fetch concurrently via a thread pool, auto-fallback to click mode.
# ---------------------------------------------------------------------------

# Shared browser profile (used by the main launch path and the pool workers).
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class _Blocked(Exception):
    """Raised when a fetched page contains a captcha / anti-bot / login wall.

    Per the security hard gates the mode must NOT retry or fall back through a
    wall - it surfaces as status=blocked so the agent stops.
    """


# Phrases that unambiguously indicate a verification / anti-bot wall in fetched
# page TEXT. Deliberately NARROW: ``_CLICK_BLOCK_KEYWORDS`` (used for click
# targets) also lists ``登录`` / ``扫码`` / ``robot`` / ``验证码``, but those
# appear in ordinary page headers / job descriptions (a "Login" nav link, a JD
# mentioning SMS 验证码) and would false-positive if scanned against full page
# text. Only the specific WeChat-style wall phrases block a parallel fetch.
_PAGE_BLOCK_PHRASES = [
    "环境异常",
    "完成验证后即可继续访问",
    "请在微信客户端打开",
    "请长按识别二维码",
]

# Default --search-terms value (shared by the argparse default and the
# parallel-fetch dispatch, so the mode can tell "agent explicitly asked to search"
# from "the argparse default leaked in" and avoid an unintended keyword search).
_DEFAULT_SEARCH_TERMS = "AI,人工智能,Agent,大模型,算法"


def _parse_total_pages(text: str | None) -> int | None:
    """Extract an explicit page-count (e.g. '16 页', '1/16', '16 pages')."""
    if not text:
        return None
    m = re.search(r"(\d+)\s*(?:页|pages?|/pages?)", text, re.IGNORECASE)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\s*/\s*(\d+)", text)  # "1 / 16" -> 16
    if m:
        return int(m.group(2))
    return None


def _parse_total_items(text: str | None) -> int | None:
    """Best-effort item-count from a result-count line ('共 151 个职位' -> 151)."""
    if not text:
        return None
    nums = [int(n) for n in re.findall(r"\d+", text)]
    if not nums:
        return None
    return max(nums)  # the total is usually the largest number on the line


def _compute_total_pages(
    count_text: str | None, size_val: int | None, max_pages: int,
) -> tuple[int, int | None]:
    """Decide how many pages to fetch. Prefers an explicit page count, then
    item-count / page-size, else caps at ``max_pages`` (dedup drops the tail)."""
    pages = _parse_total_pages(count_text)
    if pages:
        pages = max(1, pages)
        return min(max_pages, pages), pages
    if size_val:
        items = _parse_total_items(count_text)
        if items:
            pages = max(1, math.ceil(items / size_val))
            return min(max_pages, pages), pages
    return max_pages, None


def _scan_body_count(body: str | None) -> str | None:
    """Scan rendered body text for a total-jobs count when the result-count
    selectors miss it (e.g. xiaomi renders '开启新的工作（151）' in full-width
    parens, with no '个职位' substring for the selectors to anchor on).

    Returns the matched digit string (caller parses via ``_parse_total_items``),
    or None. Patterns tried in priority order: a 2-4 digit number in full/half
    width parens, 'N 个职位' / 'N 职位', 'N results', '共 N'.
    """
    if not body:
        return None
    for pat in (
        r"[（(]\s*(\d{2,4})\s*[）)]",        # （151）  /  (151)
        r"(\d{2,4})\s*个?职位",               # 151 个职位 / 151 职位
        r"(\d{2,4})\s*results?",              # 151 results
        r"(\d{1,4})\s*结果",                   # 20 结果
        r"共\s*(\d{2,4})",                    # 共 151
    ):
        m = re.search(pat, body)
        if m:
            return m.group(1)
    return None


def _has_jd_detail_evidence(text: str | None) -> bool:
    """Whether rendered text contains an actual role-detail section."""
    normalized = (text or "").casefold()
    return any(marker in normalized for marker in (
        "职位描述", "岗位职责", "任职要求", "工作职责", "岗位要求",
        "job description", "responsibilities", "qualifications",
    ))


def _detect_pagination(page: Any, retries: int) -> dict[str, Any] | None:
    """Probe URL-keyed pagination by clicking next, reading the URL, going back,
    and diffing the two URLs to find the page-number / page-size query params.

    Returns ``None`` when URL-keyed pagination is not detectable (no paginator,
    no URL change on click = 'load more' style, or path/opaque pagination) so
    the caller can fall back to serial click mode. Returns a dict with
    ``page_param`` / ``size_param`` / ``size_val`` / ``base_url`` on success.
    """
    url_1 = page.url
    url_2: str | None = None
    for _ in range(max(1, retries)):
        # Re-find each attempt: the paginator may re-render after a click.
        next_btn = _find_next_page_button(page)
        if next_btn is None:
            return None  # no paginator -> single page; caller handles
        try:
            next_btn.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            pass
        try:
            next_btn.click(timeout=5000)
            page.wait_for_timeout(2000)
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
        except Exception:
            continue  # click itself failed -> retry
        if page.url == url_1:
            # Click succeeded but URL unchanged -> 'load more' / no-op style.
            # Not URL-keyed; fall back fast instead of burning the retry budget.
            return None
        url_2 = page.url
        break
    if url_2 is None:
        return None  # exhausted retries without a successful advancing click
    # Return to page 1 so the (eventual) click-fallback path starts clean.
    prev = _find_prev_page_button(page)
    if prev is not None:
        try:
            prev.click(timeout=5000)
            page.wait_for_timeout(1500)
        except Exception:
            try:
                page.go_back(timeout=5000)
            except Exception:
                pass
    else:
        try:
            page.go_back(timeout=5000)
        except Exception:
            pass
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass

    q1 = dict(parse_qsl(urlparse(url_1).query, keep_blank_values=True))
    q2 = dict(parse_qsl(urlparse(url_2).query, keep_blank_values=True))
    page_param = None
    for k, v2 in q2.items():
        v1 = q1.get(k, "")
        if v1 == v2:
            continue  # unchanged between pages
        # Param changed (or newly appeared) - is it a numeric page increment?
        try:
            n2 = int(v2)
        except ValueError:
            continue  # non-numeric change (token, slug) - not the page param
        if v1 == "":
            # Param absent on page 1, appears on page 2 (xiaomi: page-1 URL is a
            # bare share_token; current/limit only show up from page 2). Accept
            # it as the page param when page 2's value is 2.
            if n2 == 2:
                page_param = k
                break
            continue
        try:
            n1 = int(v1)
        except ValueError:
            continue
        if n2 == n1 + 1:
            page_param = k
            break
    if page_param is None:
        return None  # path-based / opaque pagination -> fall back to click

    # Size param: scan q2 (page 2 carries the full param set, including limit,
    # even when page 1's URL omits it). base_url = url_2 for the same reason -
    # building page URLs from url_2 preserves limit + every other param.
    size_param = None
    size_val = None
    for cand in ("limit", "pageSize", "page_size", "size", "perPage",
                 "per_page", "count", "rows", "rowCount"):
        if cand in q2:
            size_param = cand
            try:
                size_val = int(q2[cand])
            except ValueError:
                size_val = None
            break
    return {
        "page_param": page_param,
        "size_param": size_param,
        "size_val": size_val,
        "base_url": url_2,  # page-2 URL: full param set, page_param reset per page
    }


def _substitute_page_param(base_url: str, page_param: str, page_no: int) -> str:
    """Return ``base_url`` with its ``page_param`` query value set to ``page_no``."""
    p = urlparse(base_url)
    qsl = parse_qsl(p.query, keep_blank_values=True)
    found = False
    new_q: list[tuple[str, str]] = []
    for k, v in qsl:
        if k == page_param:
            new_q.append((k, str(page_no)))
            found = True
        else:
            new_q.append((k, v))
    if not found:
        new_q.append((page_param, str(page_no)))
    return urlunparse(p._replace(query=urlencode(new_q)))


def _dedup_adjacent(texts: list[str | None]) -> list[str]:
    """Drop None and exact-adjacent-duplicate page texts.

    Out-of-range pages on some sites echo the page-1 (or last valid) body; the
    downstream ``deduplicate.py`` handles cross-page content dedup, but dropping
    adjacent identical blobs here keeps page files honest and avoids feeding
    the extractors stale echoes.
    """
    out: list[str] = []
    for t in texts:
        if not t:
            continue
        if out and t.strip() == out[-1].strip():
            continue
        out.append(t)
    return out


# Per-worker persistent browser (Java-thread-pool analog: N worker threads, each
# with one long-lived browser, pulling URLs from the queue). Amortizes browser
# launch to ``concurrency`` instead of one per page.
_THREAD_LOCAL = threading.local()
_CREATED: list[tuple[Any, Any]] = []  # (playwright, browser) pairs for teardown
_CREATED_LOCK = threading.Lock()


def _worker_init() -> None:
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)
    _THREAD_LOCAL.pw = pw
    _THREAD_LOCAL.browser = browser
    with _CREATED_LOCK:
        _CREATED.append((pw, browser))


def _worker_shutdown() -> None:
    with _CREATED_LOCK:
        pairs = list(_CREATED)
        _CREATED.clear()
    for pw, browser in pairs:
        try:
            browser.close()
        except Exception:
            pass
        try:
            pw.stop()
        except Exception:
            pass


def _fetch_one(url: str, wait_ms: int) -> str:
    browser = getattr(_THREAD_LOCAL, "browser", None)
    if browser is None:
        raise RuntimeError("worker browser not initialized")
    context = browser.new_context(
        user_agent=_BROWSER_UA, viewport={"width": 1920, "height": 1080}
    )
    install_public_network_guard(context)
    page = context.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(wait_ms)
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        _scroll_to_load(page, wait_ms)
        for _ in range(3):
            _dismiss_consent(page)
        text = _extract_body_text(page)
        low = text.lower()
        for kw in _PAGE_BLOCK_PHRASES:
            if kw.lower() in low:
                raise _Blocked(f"anti-bot/captcha wall on {url}: {kw!r}")
        return text
    finally:
        try:
            context.close()
        except Exception:
            pass


def _fetch_one_with_retry(url: str, wait_ms: int, retries: int) -> str:
    last_exc: Exception | None = None
    for _ in range(max(1, retries)):
        try:
            return _fetch_one(url, wait_ms)
        except _Blocked:
            raise  # walls are not retried (security gate)
        except Exception as exc:
            last_exc = exc
    assert last_exc is not None
    raise last_exc


def _parallel_fetch(urls: list[str], wait_ms: int, concurrency: int, retries: int) -> list[str]:
    """Fetch all page URLs concurrently via a thread pool. Raises ``_Blocked`` if
    any page hit a captcha/anti-bot wall, or ``RuntimeError`` on a non-wall error
    after all retries (so the caller can fall back to serial click mode)."""
    texts: list[str | None] = [None] * len(urls)
    block_reason: str | None = None
    error_reason: str | None = None
    try:
        # NOTE: ThreadPoolExecutor has no `finalizer` kwarg (unlike
        # multiprocessing.Pool); worker browsers are torn down manually below via
        # the module-level _CREATED registry.
        with ThreadPoolExecutor(
            max_workers=max(1, concurrency),
            initializer=_worker_init,
        ) as ex:
            fut_to_idx = {
                ex.submit(_fetch_one_with_retry, u, wait_ms, retries): i
                for i, u in enumerate(urls)
            }
            for fut in as_completed(fut_to_idx):
                i = fut_to_idx[fut]
                try:
                    texts[i] = fut.result()
                except _Blocked as b:
                    if block_reason is None:
                        block_reason = str(b)
                except Exception as exc:  # noqa: BLE001 - surface for fallback
                    if error_reason is None:
                        error_reason = str(exc)
    finally:
        _worker_shutdown()  # close every worker's persistent browser + playwright
    if block_reason is not None:
        raise _Blocked(block_reason)
    if error_reason is not None:
        raise RuntimeError(error_reason)
    return texts  # type: ignore[return-value]


def browse_parallel_fetch_mode(
    url: str,
    out_dir: Path,
    max_pages: int,
    wait_ms: int,
    search_terms: list[str] | None = None,
    concurrency: int = 4,
    detect_retries: int = 3,
) -> dict[str, Any]:
    """v1.6 fast path: detect URL-keyed pagination, pre-compute all page URLs,
    fetch them concurrently via a thread pool, write ``page_01..NN.txt``.

    Self-contained: owns its browser lifecycle (does not reuse a caller-opened
    page). On any failure that makes URL pre-computation impossible (no paginator,
    no URL change = 'load more', opaque path/POST pagination) OR a non-wall error
    during parallel fetch, it transparently falls back to serial ``click`` mode
    (v1.5 path) so completeness never regresses. A captcha/anti-bot wall is NOT
    fallen back through - it surfaces as ``status=blocked`` (security gate).

    Optional ``search_terms``: if the page exposes a search box, search the
    keywords first (user step 2), then detect pagination on the filtered
    results URL. Search is best-effort; if no box is found the flow proceeds
    unfiltered.
    """
    from playwright.sync_api import sync_playwright

    # ---- Phase 1: detect (sync, own browser) ----
    detect: dict[str, Any] | None = None
    count_text: str | None = None
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=_BROWSER_UA, viewport={"width": 1920, "height": 1080}
        )
        install_public_network_guard(context)
        page = context.new_page()
        public_job_collector = PublicJobEvidenceCollector()
        public_job_collector.attach(page)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(wait_ms)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            _scroll_to_load(page, wait_ms)
            for _ in range(3):
                _dismiss_consent(page)

            if search_terms:
                # Best-effort search (user step 2). Failure does not abort.
                try:
                    from_cur = _perform_search(page, search_terms[0], _SEARCH_WAIT_MS)
                    _searched = from_cur[0]
                except Exception:
                    _searched = False
                # After a search, settle the page before reading totals / paginating.
                _scroll_to_load(page, wait_ms)
                for _ in range(3):
                    _dismiss_consent(page)

            body_1 = _extract_body_text(page)
            # SPA-shell fast-fail: mokahr / feishu card-SPAs render <500 chars
            # under a plain load (they need search-interact card click-through,
            # not pagination). Don't waste time on detect+click - return page 1
            # only so the agent's existing thin-result retry triggers
            # search-interact. Matches the list-mode threshold in
            # single-url-extraction.md ("If [PAGE_TEXT] < ~500 chars ... retry
            # ONCE with --mode search-interact").
            if len(body_1.strip()) < 500:
                # Definitively empty shell: 0 chars of rendered body text AND
                # no public JSON job records captured. This is a HARD BLOCK
                # (anti-bot-gated SPA that silently no-ops in headless - the app
                # bundle loads but never mounts and fires no job-list XHR; or a
                # dead/404 URL), NOT a recoverable card-SPA that search-interact
                # could fix (a 0-char shell has no rendered UI = no search box to
                # drive). Returning ``status=blocked`` here makes the workflow
                # stop immediately instead of burning a ~45s search-interact
                # retry that cannot succeed, and it respects security gate #2
                # (report the block; never attempt to circumvent anti-bot).
                # Verified un-renderable in headless via live probes:
                # didi (silent anti-bot signing), netease (anti-bot + redirects
                # to social-recruitment), baidu (302 -> /jobs/404 -> about:blank).
                if not body_1.strip() and not public_job_collector.records:
                    page_title = page.title()
                    _screenshot_path = out_dir / "blocked_empty_shell.png"
                    _save_screenshot(page, _screenshot_path)
                    _empty_hash = f"sha256_{hashlib.sha256(b'').hexdigest()[:16]}"
                    browser.close()
                    return {
                        "status": "blocked",
                        "url": url,
                        "mode": "parallel-fetch",
                        "used_path": "spa_shell_empty_no_evidence",
                        "reason": "page rendered 0 chars of body text and no public job JSON evidence",
                        "title": page_title,
                        "content_hash": _empty_hash,
                        "text_path": "",
                        "screenshot_path": str(_screenshot_path),
                        "text_length": 0,
                        "page_count": 0,
                        "page_files": [],
                    }
                # The SPA DOM may be empty while its public XHR already
                # supplied structured job records. Preserve that observed JSON
                # evidence instead of writing a zero-byte page and discarding
                # the collector's work.
                observed_text = public_job_collector.evidence_text() if public_job_collector.records else body_1
                page_files = _save_page_files([observed_text], out_dir)
                short_hash, text_path, screenshot_path = _save_evidence(observed_text, out_dir)
                _save_screenshot(page, screenshot_path)
                page_title = page.title()
                browser.close()
                return {
                    "status": "ok",
                    "url": url,
                    "mode": "parallel-fetch",
                    "used_path": "spa_shell_no_pagination",
                    "title": page_title,
                    "content_hash": short_hash,
                    "text_path": str(text_path),
                    "screenshot_path": str(screenshot_path),
                    "text_length": len(observed_text),
                    "page_count": 1,
                    "page_files": page_files,
                }
            count_text = _read_result_count_text(page) or _scan_body_count(body_1)
            detect = _detect_pagination(page, detect_retries)

            if detect is None:
                # FALLBACK A: Card-SPAs commonly expose a complete public list
                # without URL-keyed pagination.  Their list text is not JD
                # evidence, though: extracting from it creates title-only
                # candidates.  Follow the bounded public detail links in this
                # same browser invocation, rather than relying on an LLM to
                # correctly schedule a second browse call after it has already
                # seen the list.  This is generic capability detection, not a
                # site strategy: if no cards/details are present, retain the
                # historic serial-pagination fallback below.
                detail_result = browse_interact_mode(
                    page, url, out_dir, max_cards=50, wait_ms=wait_ms,
                    collector=public_job_collector,
                )
                if detail_result.get("jd_detail_evidence"):
                    detail_result["used_path"] = "interact_fallback_no_detect"
                    browser.close()
                    return detail_result

                # A non-paginated page with no public detail cards may still be
                # a conventional load-more/next-page list.  Preserve the old
                # bounded click fallback for that case.
                result = browse_click_mode(
                    page, url, out_dir, None, None, max_pages, wait_ms, click_auto=True
                )
                result["used_path"] = "click_fallback_no_detect"
                browser.close()
                return result
        finally:
            try:
                browser.close()
            except Exception:
                pass

    # ---- Compute page URLs ----
    total_pages, declared_total_pages = _compute_total_pages(
        count_text, detect.get("size_val"), max_pages,
    )
    page_param = detect["page_param"]
    urls = [
        _substitute_page_param(detect["base_url"], page_param, i)
        for i in range(1, total_pages + 1)
    ]

    # ---- Phase 2: parallel fetch ----
    try:
        texts = _parallel_fetch(urls, wait_ms, concurrency, detect_retries)
    except _Blocked as b:
        return {
            "status": "blocked",
            "url": url,
            "mode": "parallel-fetch",
            "reason": str(b),
        }
    except Exception as exc:  # noqa: BLE001 - fallback path
        # FALLBACK B: parallel fetch errored -> re-open and serial-click.
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=_BROWSER_UA, viewport={"width": 1920, "height": 1080}
            )
            install_public_network_guard(context)
            page = context.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(wait_ms)
                _scroll_to_load(page, wait_ms)
                for _ in range(3):
                    _dismiss_consent(page)
                result = browse_click_mode(
                    page, url, out_dir, None, None, total_pages, wait_ms, click_auto=True
                )
                result["used_path"] = f"click_fallback_fetch_error ({exc})"
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
            return result

    texts = _dedup_adjacent(texts)
    page_files = _save_page_files(texts, out_dir)
    full_text = "\n\n--- PAGE BREAK ---\n\n".join(texts)
    short_hash, text_path, screenshot_path = _save_evidence(full_text, out_dir)

    return {
        "status": "ok",
        "url": url,
        "mode": "parallel-fetch",
        "used_path": "parallel",
        "title": "",  # title not retained across the pool workers; harmless
        "content_hash": short_hash,
        "text_path": str(text_path),
        "screenshot_path": str(screenshot_path),
        "text_length": len(full_text),
        "page_count": len(texts),
        "page_files": page_files,
        "pagination": {
            "pages_collected": len(texts),
            "total_pages": total_pages,
            "declared_total_pages": declared_total_pages,
            "page_param": page_param,
            "size_param": detect.get("size_param"),
            "size_val": detect.get("size_val"),
            "concurrency": concurrency,
        },
        "terminal_evidence": (
            "finite_page_range_exhausted"
            if declared_total_pages is not None and total_pages == declared_total_pages
            else None
        ),
        "truncated_by_max_pages": (
            declared_total_pages is not None and declared_total_pages > max_pages
        ),
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
# Search mode helpers
# ---------------------------------------------------------------------------

def _find_search_input(page: Any) -> Any | None:
    """Find a visible search input element on the page.

    Tries priority-ordered selectors from _SEARCH_INPUT_SELECTORS.
    For the broad fallback selector (last resort), applies additional
    heuristics: rejects inputs smaller than 100px wide or with visible
    password/email/phone placeholder text.
    """
    for sel in _SEARCH_INPUT_SELECTORS:
        try:
            elements = page.locator(sel).all()
            for el in elements:
                try:
                    if not el.is_visible():
                        continue
                    # For the broad fallback selector, apply size heuristic
                    box = el.bounding_box()
                    if box and box["width"] < 100:
                        continue  # Too narrow to be a search box
                    return el
                except Exception:
                    continue
        except Exception:
            continue
    return None


def _find_search_button(page: Any) -> Any | None:
    """Find a visible search submit button near the search input."""
    for sel in _SEARCH_BUTTON_SELECTORS:
        try:
            btn = page.locator(sel).first
            if btn.is_visible():
                return btn
        except Exception:
            continue
    return None


def _count_job_cards(page: Any) -> int:
    """Estimate the number of visible job cards/positions on the page.

    Uses JavaScript with Set-based element deduplication so a card matching
    multiple selectors (e.g. both "[class*='job-card']" and "a[href*='job']")
    is only counted once. Caps at 200 to avoid runaway counting on huge pages.

    Used to detect whether a search actually narrowed results vs client-side
    fake filtering (where all cards remain in the DOM, just hidden).
    """
    js_code = """
    () => {
        const patterns = [
            '[class*="job-card"]', '[class*="JobCard"]', '[class*="job-item"]',
            '[class*="position"]', '[class*="card"] li', '.job-list > *',
            '[class*="list"] > li', 'a[href*="job"]', 'a[href*="position"]',
        ];
        const seen = new Set();
        for (const sel of patterns) {
            try {
                const els = document.querySelectorAll(sel);
                for (const el of els) {
                    if (seen.has(el)) continue;
                    const rect = el.getBoundingClientRect();
                    if (rect.width > 0 && rect.height > 0) {
                        seen.add(el);
                    }
                }
            } catch(e) {}
            if (seen.size > 5) break;
        }
        return Math.min(seen.size, 200);
    }
    """
    try:
        return page.evaluate(js_code)
    except Exception:
        return 0


def _read_result_count_text(page: Any) -> str | None:
    """Try to read a result-count indicator (e.g. '共 42 个职位')."""
    for sel in _RESULT_COUNT_SELECTORS:
        try:
            el = page.locator(sel).first
            if el.is_visible():
                text = el.inner_text()
                if text and len(text) < 50:
                    return text.strip()
        except Exception:
            continue
    return None


def _perform_search(page: Any, term: str, wait_ms: int) -> tuple[bool, str]:
    """Locate the search box, enter a keyword, and trigger the search.

    Tries three trigger strategies in order:
      1. Press Enter (works for most sites)
      2. Click a visible search button
      3. Type and wait (real-time filtering without explicit submit)

    Returns (success, details_string).
    """
    search_input = _find_search_input(page)
    if search_input is None:
        return False, "No search input found on page"

    # Clear existing text and type the search term
    try:
        search_input.click(timeout=3000)
        page.wait_for_timeout(300)
        # Triple-click to select all existing text, then type
        search_input.click(timeout=3000, click_count=3)
        page.wait_for_timeout(200)
        search_input.fill(term)
        page.wait_for_timeout(_SEARCH_TYPE_DELAY_MS)
    except Exception as exc:
        return False, f"Failed to type search term: {exc}"

    # Strategy 1: Press Enter
    try:
        search_input.press("Enter")
        page.wait_for_timeout(wait_ms)
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        # Check if URL changed (server-side search)
        result_count = _read_result_count_text(page)
        if result_count:
            return True, f"Enter-key search triggered — result indicator: {result_count}"
        visible_cards = _count_job_cards(page)
        if visible_cards >= 1:
            return True, f"Enter-key search triggered — {visible_cards} visible cards"
    except Exception:
        pass

    # Strategy 2: Click search button
    search_btn = _find_search_button(page)
    if search_btn is not None:
        try:
            search_btn.click(timeout=3000)
            page.wait_for_timeout(wait_ms)
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            result_count = _read_result_count_text(page)
            if result_count:
                return True, f"Button-click search triggered — result indicator: {result_count}"
            visible_cards = _count_job_cards(page)
            if visible_cards >= 1:
                return True, f"Button-click search triggered — {visible_cards} visible cards"
        except Exception:
            pass

    # Strategy 3: Assume real-time filtering (just typing is enough)
    page.wait_for_timeout(wait_ms)
    visible_cards = _count_job_cards(page)
    return True, f"Real-time filter assumed — {visible_cards} visible cards"


def browse_search_mode(
    page: Any,
    url: str,
    out_dir: Path,
    max_pages: int,
    wait_ms: int,
    search_terms: list[str],
    strategy: str,
    fallback: str,
) -> dict[str, Any]:
    """Search-first browsing: enter keywords into search box, then browse filtered results.

    Args:
        search_terms: List of keywords to try (e.g. ["AI", "Agent", "人工智能"]).
        strategy:
            - "first_match": Try terms in order; stop at the first that yields >0 results.
            - "each": Try every term; merge and deduplicate results.
            - "broad": Only use the first term (assumed broadest).
        fallback:
            - "full": If search is unavailable or returns 0 results, fall back
              to full list-mode browse.
            - "none": Return empty result if search fails.

    Returns the standard result dict.
    """
    # Initial page load
    _scroll_to_load(page, wait_ms)
    for _ in range(3):
        _dismiss_consent(page)
    page.wait_for_timeout(wait_ms)

    # Pre-search baseline: count and URL
    pre_count = _count_job_cards(page)
    pre_url = page.url

    # Detect search capability
    search_input = _find_search_input(page)
    if search_input is None:
        if fallback == "full":
            return browse_list_mode(page, url, out_dir, max_pages, wait_ms)
        return {
            "status": "empty",
            "url": url,
            "title": page.title(),
            "content_hash": "",
            "text_path": "",
            "screenshot_path": "",
            "text_length": 0,
            "search_attempted": True,
            "search_error": "No search input found on page",
            "fallback_used": False,
        }

    # Determine which terms to try
    if strategy == "broad":
        terms_to_try = search_terms[:1]
    else:
        terms_to_try = search_terms

    all_texts: list[str] = []
    search_log: list[dict[str, Any]] = []
    successful_terms: list[str] = []

    for term in terms_to_try:
        # Reset: go back to original URL if we navigated away
        if page.url != pre_url:
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(wait_ms)
                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
            except Exception:
                continue

        ok, detail = _perform_search(page, term, _SEARCH_WAIT_MS)
        post_count = _count_job_cards(page)
        result_indicator = _read_result_count_text(page)

        entry = {
            "term": term,
            "search_ok": ok,
            "detail": detail,
            "pre_count": pre_count,
            "post_count": post_count,
            "result_indicator": result_indicator,
        }
        search_log.append(entry)

        if not ok:
            continue

        # Client-side fake filter detection:
        # If post_count == pre_count (all cards still visible), the search
        # may be client-side CSS hiding rather than true filtering. Warn but
        # still collect — we can't reliably tell without deeper DOM inspection.
        if post_count > 0 and post_count == pre_count and pre_count > 10:
            entry["warning"] = (
                f"Post-search card count ({post_count}) equals pre-search ({pre_count}). "
                "Search may be client-side fake filter — results may be incomplete. "
                "Consider --fallback full for complete coverage."
            )

        if post_count > 0:
            # Collect filtered results
            page_text = _extract_body_text(page)
            all_texts.append(f"=== SEARCH: '{term}' | {detail} ===\n{page_text}")
            successful_terms.append(term)

            if strategy == "first_match":
                break  # Stop after first successful term
        elif fallback == "full" and strategy == "first_match":
            # This term gave 0 results but we'll try the next term
            continue

    # If no search succeeded
    if not all_texts:
        if fallback == "full":
            # Navigate back to unfiltered view first
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(wait_ms)
                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
            except Exception:
                pass
            return browse_list_mode(page, url, out_dir, max_pages, wait_ms)
        return {
            "status": "empty",
            "url": url,
            "title": page.title(),
            "content_hash": "",
            "text_path": "",
            "screenshot_path": "",
            "text_length": 0,
            "search_attempted": True,
            "search_terms_tried": len(terms_to_try),
            "search_successful": False,
            "search_log": search_log,
            "fallback_used": False,
        }

    # Merge all search result pages
    full_text = "\n\n".join(all_texts)

    # Remember the search-filtered URL for recovery during interact phase
    search_url = page.url

    # Paginate through filtered results if needed (fewer pages than full list)
    page_num = 1
    while page_num < max_pages:
        next_btn = _find_next_page_button(page)
        if next_btn is None:
            break
        try:
            old_url = page.url
            next_btn.click(timeout=5000)
            page.wait_for_timeout(wait_ms)
            try:
                page.wait_for_load_state("networkidle", timeout=4000)
            except Exception:
                pass
            if page.url == old_url:
                new_text = _extract_body_text(page)
                if new_text in full_text:
                    break
                full_text += "\n\n--- PAGE BREAK ---\n\n" + new_text
            else:
                _scroll_to_load(page, wait_ms)
                full_text += "\n\n--- PAGE BREAK ---\n\n" + _extract_body_text(page)
            search_url = page.url
            page_num += 1
        except Exception:
            break

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
        "search_attempted": True,
        "search_successful": True,
        "search_terms_matched": successful_terms,
        "search_log": search_log,
        "pre_search_card_count": pre_count,
        "fallback_used": False,
        "search_url": search_url,
    }


def browse_search_interact_mode(
    page: Any,
    url: str,
    out_dir: Path,
    max_pages: int,
    wait_ms: int,
    search_terms: list[str],
    strategy: str,
    fallback: str,
    max_cards: int,
) -> dict[str, Any]:
    """Search-then-interact: keyword filtering + click-through of filtered cards.

    This is the optimal mode for high-page-count career sites (Moka, zhiye.com,
    Feishu) where the search box is available.  It:
      1. Finds and uses the search box (hard gate — no search = fallback)
      2. Paginates through filtered results (fewer pages than full list)
      3. Clicks each visible card to expand detail panels and capture full JDs

    It falls back to browse_search_mode (no interact) if no clickable cards are
    found after filtering, and to browse_list_mode if search itself is unavailable.

    Returns the standard result dict with extra search_* and interact_* fields.
    """
    # ── Phase 1: Search filter (reuse browse_search_mode logic) ──────────
    _scroll_to_load(page, wait_ms)
    for _ in range(3):
        _dismiss_consent(page)
    page.wait_for_timeout(wait_ms)

    pre_count = _count_job_cards(page)
    pre_url = page.url

    search_input = _find_search_input(page)
    if search_input is None:
        if fallback == "full":
            return browse_list_mode(page, url, out_dir, max_pages, wait_ms)
        return {
            "status": "empty",
            "url": url,
            "title": page.title(),
            "content_hash": "",
            "text_path": "",
            "screenshot_path": "",
            "text_length": 0,
            "search_attempted": True,
            "search_error": "No search input found on page",
            "fallback_used": False,
        }

    terms_to_try = search_terms if strategy != "broad" else search_terms[:1]

    all_texts: list[str] = []
    search_log: list[dict[str, Any]] = []
    successful_terms: list[str] = []
    search_url = url

    for term in terms_to_try:
        if page.url != pre_url:
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(wait_ms)
                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
            except Exception:
                continue

        ok, detail = _perform_search(page, term, _SEARCH_WAIT_MS)
        post_count = _count_job_cards(page)
        result_indicator = _read_result_count_text(page)

        entry = {
            "term": term, "search_ok": ok, "detail": detail,
            "pre_count": pre_count, "post_count": post_count,
            "result_indicator": result_indicator,
        }
        search_log.append(entry)

        if not ok:
            continue

        if post_count > 0 and post_count == pre_count and pre_count > 10:
            entry["warning"] = (
                f"Post-search card count ({post_count}) equals pre-search ({pre_count}). "
                "Search may be client-side fake filter — results may be incomplete."
            )

        if post_count > 0:
            page_text = _extract_body_text(page)
            all_texts.append(f"=== SEARCH: '{term}' | {detail} ===\n{page_text}")
            successful_terms.append(term)
            search_url = page.url
            if strategy == "first_match":
                break
        elif fallback == "full" and strategy == "first_match":
            continue

    if not all_texts:
        if fallback == "full":
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(wait_ms)
                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
            except Exception:
                pass
            return browse_list_mode(page, url, out_dir, max_pages, wait_ms)
        return {
            "status": "empty", "url": url, "title": page.title(),
            "content_hash": "", "text_path": "", "screenshot_path": "",
            "text_length": 0,
            "search_attempted": True,
            "search_terms_tried": len(terms_to_try),
            "search_successful": False,
            "search_log": search_log,
            "fallback_used": False,
        }

    list_text = "\n\n".join(all_texts)

    # ── Phase 2: Click through filtered cards ──────────────────────────
    # Expand category sections (Moka: "X-STAR顶尖人才 共3个职位")
    cats_clicked = (
        _expand_categories(page, wait_ms)
        if _should_expand_categories(len(_find_clickable_cards(page, 1)))
        else 0
    )

    interact_text, clicked, found, failed = _interact_on_cards(
        page, search_url, search_url, out_dir, max_cards, wait_ms,
        label_prefix="SEARCH-JOB"
    )

    if found == 0:
        # No clickable cards on filtered view — fall back to plain search result
        full_text = list_text
        short_hash, text_path, screenshot_path = _save_evidence(full_text, out_dir)
        _save_screenshot(page, screenshot_path)
        return {
            "status": "ok", "url": url, "title": page.title(),
            "content_hash": short_hash,
            "text_path": str(text_path), "screenshot_path": str(screenshot_path),
            "text_length": len(full_text),
            "pagination": {"pages_collected": 1, "max_allowed": max_pages},
            "search_attempted": True, "search_successful": True,
            "search_terms_matched": successful_terms, "search_log": search_log,
            "pre_search_card_count": pre_count, "fallback_used": False,
            "interact_attempted": True, "cards_clicked": 0,
            "cards_found": 0, "cards_failed": 0,
            "categories_expanded": cats_clicked,
            "note": "Search filtered successfully but no clickable cards found. "
                    "Returned filtered list text only.",
        }

    # ── Phase 3: Combine search + interact output ──────────────────────
    full_text = f"=== FILTERED LIST (search: {', '.join(successful_terms)}) ===\n{list_text}\n{interact_text}"
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
        "pagination": {"pages_collected": 1, "max_allowed": max_pages},
        "search_attempted": True,
        "search_successful": True,
        "search_terms_matched": successful_terms,
        "search_log": search_log,
        "pre_search_card_count": pre_count,
        "fallback_used": False,
        "interact_attempted": True,
        "cards_clicked": clicked,
        "cards_found": found,
        "cards_failed": failed,
        "categories_expanded": cats_clicked,
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


def _should_expand_categories(initial_card_count: int) -> bool:
    """Expand broad category selectors only when no job card is already usable."""
    return initial_card_count == 0


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

    # CSS is fast but some virtualized SPAs expose only the first visible card
    # through those selectors. Always supplement it with the DOM-wide JS scan
    # so the remaining job links are not silently missed.
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
                if _boxes_substantially_overlap(box, sb):
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


def _boxes_substantially_overlap(
    box: dict[str, float], rounded_other: tuple[float, float, float, float],
) -> bool:
    """Whether a child title link substantially overlaps an existing card box."""
    ax1, ay1 = float(box["x"]), float(box["y"])
    ax2, ay2 = ax1 + float(box["width"]), ay1 + float(box["height"])
    bx1, by1, bx2, by2 = rounded_other
    width = max(0.0, min(ax2, bx2) - max(ax1, bx1))
    height = max(0.0, min(ay2, by2) - max(ay1, by1))
    overlap = width * height
    smaller_area = min((ax2 - ax1) * (ay2 - ay1), (bx2 - bx1) * (by2 - by1))
    return smaller_area > 0 and overlap / smaller_area >= 0.8


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


def _card_interaction_idle_timeout_ms(wait_ms: int) -> int:
    """Bound the optional network-idle wait after a card action.

    Career SPAs frequently keep analytics, polling, or SSE connections open;
    waiting eight seconds for ``networkidle`` once per public card turns a
    20-position list into a multi-minute crawl.  Visible-detail extraction
    still has the caller's explicit render wait, while this optional settle
    check is kept short and bounded.
    """
    return max(500, min(1500, wait_ms * 2))


def _interact_on_cards(
    page: Any,
    current_url: str,
    recover_url: str,
    out_dir: Path,
    max_cards: int,
    wait_ms: int,
    label_prefix: str = "JOB",
) -> tuple[str, int, int, int]:
    """Click through job cards on an already-loaded page, collecting detail text.

    This is the shared interact core used by both browse_interact_mode and the
    search-then-interact path.  It expects the page to already be loaded and
    scrolled, with cards visible.

    Args:
        page: Playwright page object (already loaded).
        current_url: Expected URL of the list page (for change detection).
        recover_url: URL to navigate back to if we drift away.
        out_dir: Output directory (unused but kept for interface consistency).
        max_cards: Max number of cards to click.
        wait_ms: Wait time between actions.
        label_prefix: Prefix for section headers ("JOB" or "SEARCH-JOB").

    Returns:
        (combined_text, clicked, found, failed)
    """
    cards = _find_clickable_cards(page, max_cards)
    if not cards:
        return ("", 0, 0, 0)

    detail_sections: list[str] = []
    clicked = 0
    failed = 0
    start_time = time.time()
    time_budget = 120  # Max 2 minutes

    for i, card in enumerate(cards):
        if time.time() - start_time > time_budget:
            detail_sections.append(
                f"\n=== TIMEOUT: stopped after {clicked} cards ({failed} failed) ==="
            )
            break
        try:
            card.scroll_into_view_if_needed()
            page.wait_for_timeout(500)
            pre_text = _extract_body_text(page)

            card.click(timeout=3000)
            page.wait_for_timeout(min(wait_ms, 2000))
            try:
                page.wait_for_load_state(
                    "networkidle", timeout=_card_interaction_idle_timeout_ms(wait_ms)
                )
            except Exception:
                pass

            post_text = _extract_body_text(page)

            if page.url != current_url:
                detail_text = _extract_detail_text(page)
                detail_sections.append(
                    f"\n=== {label_prefix} {i + 1} ({page.url}) ===\n{detail_text}"
                )
                page.go_back(wait_until="domcontentloaded", timeout=2500)
                page.wait_for_timeout(wait_ms)
                current_url = page.url
                clicked += 1
            elif len(post_text) > len(pre_text) + 50:
                detail_text = _extract_detail_text(page)
                detail_sections.append(
                    f"\n=== {label_prefix} {i + 1} ===\n{detail_text}"
                )
                _close_detail_panel(page)
                page.wait_for_timeout(1000)
                clicked += 1
            else:
                failed += 1

        except Exception:
            failed += 1
            try:
                if page.url != recover_url:
                    page.goto(recover_url, wait_until="domcontentloaded", timeout=15000)
                    page.wait_for_timeout(wait_ms)
                    current_url = page.url
            except Exception:
                pass
            continue

    return ("\n".join(detail_sections), clicked, len(cards), failed)


def browse_interact_mode(
    page: Any, url: str, out_dir: Path, max_cards: int, wait_ms: int,
    collector: PublicJobEvidenceCollector | None = None,
) -> dict[str, Any]:
    """Click through job cards on a list page, collecting detail text from each."""

    # First, capture the list page baseline text
    _scroll_to_load(page, wait_ms)
    for _ in range(3):
        _dismiss_consent(page)
    page.wait_for_timeout(wait_ms)

    # Expand category sections (Moka pattern: "X-STAR顶尖人才 共3个职位")
    cats_clicked = (
        _expand_categories(page, wait_ms)
        if _should_expand_categories(len(_find_clickable_cards(page, 1)))
        else 0
    )

    list_pages = [_extract_body_text(page)]
    list_text = list_pages[0]

    # Some public career pages already receive structured JD bodies in their
    # own JSON responses.  Complete visible pagination first and use that
    # direct evidence instead of opening and returning from every card.
    if collector is not None and collector.records and collector.expected_count:
        advances = 0
        while len(collector.records) < collector.expected_count and advances < 20:
            next_btn = _find_next_page_button(page)
            if next_btn is None:
                break
            before_text = _extract_body_text(page)
            try:
                next_btn.click(timeout=5000)
                page.wait_for_timeout(wait_ms)
                try:
                    page.wait_for_load_state(
                        "networkidle", timeout=_card_interaction_idle_timeout_ms(wait_ms)
                    )
                except Exception:
                    pass
            except Exception:
                break
            next_text = _extract_body_text(page)
            if next_text == before_text:
                break
            list_pages.append(next_text)
            advances += 1

        if len(collector.records) >= collector.expected_count:
            list_text = "\n\n--- PAGE BREAK ---\n\n".join(list_pages)
            full_text = f"=== LIST PAGE ===\n{list_text}\n{collector.evidence_text()}"
            page_files = _save_page_files([full_text], out_dir)
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
                "cards_clicked": 0,
                "cards_found": 0,
                "cards_failed": 0,
                "categories_expanded": 0,
                "page_count": len(page_files),
                "page_files": page_files,
                "listing_count": collector.expected_count,
                "jd_detail_evidence": True,
                "terminal_evidence": "public_json_pages_exhausted",
                "truncated_by_max_cards": False,
                "used_path": "public_json_evidence",
            }

    interact_text, clicked, found, failed = _interact_on_cards(
        page, url, url, out_dir, max_cards, wait_ms, label_prefix="JOB"
    )

    # A career homepage often exposes exactly one "view all jobs" card. That
    # first interaction navigates to the real listing but is not a JD itself.
    # Follow it once, then interact with the actual job cards on the listing.
    # This is bounded navigation (one transition, then <= max_cards details),
    # not a site-specific adapter or an unbounded agent loop.
    list_url = _navigated_list_url(start_url=url, interact_text=interact_text, cards_found=found)
    if list_url:
        try:
            page.goto(list_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(wait_ms)
            _scroll_to_load(page, wait_ms)
            list_text = _extract_body_text(page)
            detail_urls = _detail_urls_from_current_page(page, list_url, max_cards)
            if detail_urls:
                second_text, second_clicked, second_found, second_failed = _fetch_detail_urls(
                    page, detail_urls, wait_ms, label_prefix="DETAIL"
                )
            else:
                second_text, second_clicked, second_found, second_failed = _interact_on_cards(
                    page, list_url, list_url, out_dir, max_cards, wait_ms, label_prefix="DETAIL"
                )
            interact_text = second_text
            clicked += second_clicked
            found += second_found
            failed += second_failed
        except Exception:
            pass
    else:
        # A conventional public listing can expose detail cards one page at a
        # time.  Collect the current page first, then advance only while cards
        # remain under the global cap.  This is deliberately page-agnostic: no
        # URL template or site adapter is required.
        while found < max_cards:
            next_btn = _find_next_page_button(page)
            if next_btn is None:
                break
            before_text = _extract_body_text(page)
            try:
                next_btn.click(timeout=5000)
                page.wait_for_timeout(wait_ms)
                try:
                    page.wait_for_load_state(
                        "networkidle", timeout=_card_interaction_idle_timeout_ms(wait_ms)
                    )
                except Exception:
                    pass
                _scroll_to_load(page, wait_ms)
            except Exception:
                break
            next_text = _extract_body_text(page)
            if next_text == before_text:
                break
            list_pages.append(next_text)
            next_detail_text, next_clicked, next_found, next_failed = _interact_on_cards(
                page, page.url, page.url, out_dir, max_cards - found, wait_ms,
                label_prefix="JOB",
            )
            interact_text += "\n" + next_detail_text
            clicked += next_clicked
            found += next_found
            failed += next_failed
            if next_found == 0:
                break

        list_text = "\n\n--- PAGE BREAK ---\n\n".join(list_pages)

    if found == 0:
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

    full_text = f"=== LIST PAGE ===\n{list_text}\n{interact_text}"
    page_files = _save_page_files([full_text], out_dir)
    listing_count_raw = _scan_body_count(list_text)
    listing_count = int(listing_count_raw) if listing_count_raw is not None else None
    detail_complete = listing_count is not None and clicked >= listing_count
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
        "cards_found": found,
        "cards_failed": failed,
        "categories_expanded": cats_clicked,
        "page_count": len(page_files),
        "page_files": page_files,
        "listing_count": listing_count,
        "jd_detail_evidence": _has_jd_detail_evidence(interact_text),
        "terminal_evidence": "detail_links_exhausted" if detail_complete else None,
        "truncated_by_max_cards": found >= max_cards and not detail_complete,
    }


def _navigated_list_url(*, start_url: str, interact_text: str, cards_found: int) -> str | None:
    """Extract one homepage-to-list navigation URL captured by interaction."""
    if cards_found < 1:
        return None
    for match in re.finditer(r"=== JOB \d+ \(([^)]+)\)", interact_text):
        candidate = match.group(1).strip()
        # Moka and many SPA career homepages use a plural /jobs route for the
        # listing and a singular /job route for a detail. Follow only the
        # listing transition; a detail card is already captured as evidence.
        if candidate and candidate != start_url and "#/jobs/" in candidate:
            return candidate
    return None


def _detail_urls_from_current_page(page: Any, list_url: str, max_cards: int) -> list[str]:
    """Extract unique hash/detail links from a public career listing page."""
    try:
        hrefs = page.evaluate(
            "() => Array.from(document.querySelectorAll('a[href]')).map(a => a.getAttribute('href'))"
        )
    except Exception:
        return []
    base = list_url.split("#", 1)[0]
    urls: list[str] = []
    seen: set[str] = set()
    for href in hrefs if isinstance(hrefs, list) else []:
        value = str(href or "").strip()
        if "#/job/" not in value:
            continue
        full_url = value if value.startswith(("http://", "https://")) else base + value
        if full_url in seen:
            continue
        seen.add(full_url)
        urls.append(full_url)
        if len(urls) >= max_cards:
            break
    return urls


def _fetch_detail_urls(
    page: Any, urls: list[str], wait_ms: int, *, label_prefix: str,
) -> tuple[str, int, int, int]:
    """Boundedly visit deterministic public detail links and retain their text."""
    sections: list[str] = []
    failed = 0
    for index, detail_url in enumerate(urls, start=1):
        try:
            page.goto(detail_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(wait_ms)
            try:
                page.wait_for_load_state(
                    "networkidle", timeout=_card_interaction_idle_timeout_ms(wait_ms)
                )
            except Exception:
                pass
            text = _extract_detail_text(page)
            if len(text.strip()) < 50:
                failed += 1
                continue
            sections.append(f"=== {label_prefix} {index} ({detail_url}) ===\n{text}")
        except Exception:
            failed += 1
    return "\n".join(sections), len(sections), len(urls), failed


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Render career site to plain text via Playwright")
    parser.add_argument("url", help="Career site URL to browse")
    parser.add_argument("--mode", choices=["list", "detail", "interact", "search", "search-interact",
                                            "click", "parallel-fetch"],
                        default="list",
                        help="Page type: list, detail, interact (click-through cards), "
                             "search (keyword filter first), search-interact "
                             "(search + click-through filtered cards), click "
                             "(agent-driven pagination: click a target N times), "
                             "or parallel-fetch (v1.6: detect URL-keyed pagination, "
                             "pre-compute all page URLs, fetch concurrently via a "
                             "thread pool, auto-fallback to click mode)")
    parser.add_argument("--out", default="output/evidence",
                        help="Output directory for evidence files (default: output/evidence)")
    parser.add_argument("--max-pages", type=int, default=5,
                        help="Max pages to paginate in list/search mode (default: 5)")
    parser.add_argument("--max-cards", type=int, default=50,
                        help="Max job cards to click in interact/search-interact mode (default: 50)")
    parser.add_argument("--wait", type=int, default=3000,
                        help="Wait time in ms after page load/scroll/click (default: 3000)")
    parser.add_argument("--ignore-cache", action="store_true",
                        help="[Deprecated] Use --cache-mode off instead. Skip cache check.")
    parser.add_argument("--cache-mode", choices=["use", "revalidate", "off"],
                        default="use",
                        help="Cache strategy: use (return cached if exists), "
                             "revalidate (always re-browse but skip LLM if hash matches), "
                             "off (never cache, always re-browse). Default: use")
    parser.add_argument("--search-terms", default=_DEFAULT_SEARCH_TERMS,
                        help="Comma-separated keywords for search/search-interact mode "
                             "(default: AI,人工智能,Agent,大模型,算法)")
    parser.add_argument("--search-strategy", choices=["first_match", "each", "broad"],
                        default="first_match",
                        help="How to try multiple search terms (default: first_match)")
    parser.add_argument("--fallback", choices=["full", "none"], default="full",
                        help="Fallback when search is unavailable or returns 0 results (default: full)")
    parser.add_argument("--click-text", default=None,
                        help="click mode: visible text of the element to click "
                             "(e.g. '加载更多', '下一页', '>'). Substring match.")
    parser.add_argument("--click-selector", default=None,
                        help="click mode: CSS selector of the element to click. "
                             "Wins over --click-text when both are given.")
    parser.add_argument("--click-count", type=int, default=10,
                        help="click mode: max times to click the target (default: 10). "
                             "Stops early when the target disappears or the page stops changing.")
    parser.add_argument("--click-auto", action="store_true",
                        help="click mode: auto-detect the next-page arrow each click "
                             "(uses the same selector set as list-mode pagination). "
                             "Use when the paginator is an icon-only arrow with no "
                             "clickable text (e.g. Mioffice/atsx sites).")
    parser.add_argument("--concurrency", type=int, default=4,
                        help="parallel-fetch mode: max concurrent page workers "
                             "(default: 4). Each worker holds one persistent headless "
                             "browser; the Java-thread-pool analog.")
    parser.add_argument("--detect-retries", type=int, default=3,
                        help="parallel-fetch mode: retries for the detect probe and "
                             "per-page fetch (default: 3). After exhausting retries on "
                             "a non-wall error the mode falls back to serial click.")

    args = parser.parse_args()
    url = args.url
    out_dir = Path(args.out)

    if not is_safe_public_url(url):
        print(json.dumps({
            "status": "blocked", "url": url,
            "error": "unsafe_or_non_public_url",
        }, ensure_ascii=False))
        return

    # Parse search terms unconditionally (used by both search and search-interact modes)
    search_terms = [t.strip() for t in args.search_terms.split(",") if t.strip()]

    # Resolve cache mode: --ignore-cache flag overrides to "off"
    cache_mode = "off" if args.ignore_cache else args.cache_mode

    # A URL-keyed cache is valid only for plain ``list`` rendering.  Every
    # other mode changes what evidence is collected (card details, search
    # filtering, a named detail page, click count, or parallel pagination).
    # Reusing a list result for ``search-interact`` is particularly dangerous:
    # it makes the caller believe it saw JD bodies while it only saw titles.
    if args.mode != "list":
        cache_mode = "off"

    # ---- Cache check (before browser launch) ----
    cached = _check_cache(out_dir, url, cache_mode)
    if cached is not None:
        try:
            json_bytes = json.dumps(cached, ensure_ascii=False).encode("utf-8")
            sys.stdout.buffer.write(json_bytes + b"\n")
            sys.stdout.buffer.flush()
        except (OSError, AttributeError):
            sys.stdout.reconfigure(encoding="utf-8")
            print(json.dumps(cached, ensure_ascii=False))
        sys.exit(0)

    # Log search parameters for audit
    if args.mode in ("search", "search-interact"):
        sys.stderr.write(
            f"[{args.mode}] terms={search_terms} strategy={args.search_strategy} "
            f"fallback={args.fallback}\n"
        )
        sys.stderr.flush()

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

    # parallel-fetch is self-contained (owns its browser lifecycle, may fan out
    # to a thread pool of browsers), so dispatch it before main's single-browser
    # block and exit without touching the URL-keyed cache.
    if args.mode == "parallel-fetch":
        # Only search when the agent EXPLICITLY passed --search-terms; the
        # argparse default (_DEFAULT_SEARCH_TERMS) must not trigger an
        # unintended keyword filter on the parallel path.
        pf_search_terms = (
            search_terms if args.search_terms != _DEFAULT_SEARCH_TERMS else None
        )
        result = browse_parallel_fetch_mode(
            url, out_dir, args.max_pages, args.wait,
            pf_search_terms, args.concurrency, args.detect_retries,
        )
        try:
            json_bytes = json.dumps(result, ensure_ascii=False).encode("utf-8")
            sys.stdout.buffer.write(json_bytes + b"\n")
            sys.stdout.buffer.flush()
        except (OSError, AttributeError):
            sys.stdout.reconfigure(encoding="utf-8")
            print(json.dumps(result, ensure_ascii=False))
        sys.exit(0)

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
            install_public_network_guard(context)
            page = context.new_page()
            public_job_collector = PublicJobEvidenceCollector()
            public_job_collector.attach(page)

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
                result = browse_interact_mode(
                    page, url, out_dir, args.max_cards, args.wait, public_job_collector,
                )
            elif args.mode == "search":
                result = browse_search_mode(
                    page, url, out_dir, args.max_pages, args.wait,
                    search_terms, args.search_strategy, args.fallback,
                )
            elif args.mode == "search-interact":
                result = browse_search_interact_mode(
                    page, url, out_dir, args.max_pages, args.wait,
                    search_terms, args.search_strategy, args.fallback,
                    args.max_cards,
                )
            elif args.mode == "click":
                result = browse_click_mode(
                    page, url, out_dir,
                    args.click_text, args.click_selector, args.click_count,
                    args.wait, args.click_auto,
                )
            else:
                result = browse_list_mode(page, url, out_dir, args.max_pages, args.wait)

            # Persist only plain list-mode entries.  Non-list modes are
            # parameter- and evidence-type-dependent, so writing them under a
            # URL-only key would poison later list/detail reads.
            ch = result.get("content_hash")
            if ch and args.mode == "list":
                _save_cache(out_dir, url, ch)

            browser.close()
            # Write JSON via stdout buffer to avoid Windows GBK/UTF-8 corruption
            try:
                json_bytes = json.dumps(result, ensure_ascii=False).encode("utf-8")
                sys.stdout.buffer.write(json_bytes + b"\n")
                sys.stdout.buffer.flush()
            except (OSError, AttributeError):
                sys.stdout.reconfigure(encoding="utf-8")
                print(json.dumps(result, ensure_ascii=False))
            sys.exit(0)

    except Exception as exc:
        result = {
            "status": "error",
            "url": url,
            "error": str(exc),
        }
        try:
            json_bytes = json.dumps(result, ensure_ascii=False).encode("utf-8")
            sys.stdout.buffer.write(json_bytes + b"\n")
            sys.stdout.buffer.flush()
        except (OSError, AttributeError):
            sys.stdout.reconfigure(encoding="utf-8")
            print(json.dumps(result, ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    main()
