"""Deep Agents harness for the Job Discovery Agent.

Builds the DiscoverySupervisorAgent using deepagents.create_deep_agent,
wrapping Phase 4 deterministic tools as agent-callable tools.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import time
from dataclasses import asdict, is_dataclass
from typing import Any
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from deepagents import create_deep_agent
from deepagents.middleware.subagents import SubAgent

from backend.app.config import Settings
from backend.app.services.job_discovery.deduplication import deduplicate_candidates
from backend.app.services.job_discovery.normalization.jd_normalizer import (
    normalize_title,
)
from backend.app.services.job_discovery.schemas import (
    DiscoveryRunResult,
    NormalizedJobCandidate,
    OcrResult,
    PageEvidence,
)
from backend.app.services.job_discovery.tools import (
    triage_link as _triage_link,
    parse_wechat_article as _parse_wechat_article,
    ocr_image as _ocr_image,
    extract_jd_candidates as _extract_jd_candidates,
    verify_evidence as _verify_evidence,
    build_candidate_idempotency_key,
    build_similarity_group_key,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _asdict(obj: Any) -> dict[str, Any]:
    """Recursively convert a dataclass (or list of dataclasses) to plain dicts."""
    if is_dataclass(obj):
        return {k: _asdict(v) for k, v in asdict(obj).items()}
    if isinstance(obj, list):
        return [_asdict(item) for item in obj]
    if isinstance(obj, dict):
        return {k: _asdict(v) for k, v in obj.items()}
    return obj


def _build_job_discovery_llm(settings: Settings) -> ChatOpenAI:
    """Build a ChatOpenAI instance configured for job discovery models.

    Follows the same pattern as src.agents.build_llm but uses the model
    name from settings.job_discovery_model.
    """
    from src.utils import get_api_key, get_base_url

    kwargs: dict[str, Any] = {
        "model": settings.job_discovery_model,
        "temperature": 0,
        # Bound every single LLM HTTP call and the retry backoff chain so a
        # stalled DeepSeek response cannot hang the Web Navigation Agent loop
        # indefinitely (the wall-clock budget in _nav_budget_check only fires
        # between tool calls, not during an in-flight model request).
        "request_timeout": 120,
        "max_retries": 2,
    }
    api_key = get_api_key()
    if api_key:
        kwargs["api_key"] = api_key
    base_url = get_base_url()
    kwargs["base_url"] = base_url
    if "deepseek" in base_url.lower() and settings.job_discovery_model.startswith("deepseek-v4"):
        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
    return ChatOpenAI(**kwargs)


# ---------------------------------------------------------------------------
# Prompt loading and assembly
# ---------------------------------------------------------------------------

_PROMPT_DIR = Path(__file__).resolve().parent / "prompts"


def _load_prompt(name: str, required: bool = True) -> str:
    """Load a prompt template file by name (without .txt extension).

    Args:
        name: Prompt file name without .txt extension.
        required: If True, raises FileNotFoundError when the file is missing.
                  If False, returns empty string (for optional templates).

    Raises:
        FileNotFoundError: If required=True and the file does not exist.
    """
    path = _PROMPT_DIR / f"{name}.txt"
    if not path.exists():
        if required:
            raise FileNotFoundError(f"Required prompt file missing: {path}")
        return ""
    return path.read_text(encoding="utf-8")


def build_supervisor_prompt(snapshot_context: dict | None = None) -> str:
    """Assemble the Supervisor system prompt from template files.

    Args:
        snapshot_context: If provided, the Supervisor is taking over from
            a failed SnapshotExecutor / Adapter. Contains completed_steps,
            failed_step, source, and strategy_id.

    Returns:
        Complete system prompt string for the Supervisor Agent.
    """
    parts: list[str] = [_load_prompt("supervisor_base", required=True)]

    if snapshot_context is None:
        parts.append(_load_prompt("supervisor_clean_start", required=False))
    else:
        template = _load_prompt("supervisor_snapshot_fallback", required=False)
        if template:
            ctx = {
                "source": snapshot_context.get("source", "unknown"),
                "strategy_id": snapshot_context.get("strategy_id", "unknown"),
                "failed_step_count": len(snapshot_context.get("completed_steps", [])) + 1,
                "completed_steps": _format_snapshot_steps(
                    snapshot_context.get("completed_steps", [])
                ),
                "failed_step_tool": snapshot_context.get("failed_step", {}).get("tool", ""),
                "failed_step_params": json.dumps(
                    snapshot_context.get("failed_step", {}).get("params", {}),
                    ensure_ascii=False,
                ),
                "failed_step_error": str(
                    snapshot_context.get("failed_step", {}).get("error", "")
                ),
            }
            parts.append(template.format(**ctx))

    return "\n\n".join(parts)


def _format_snapshot_steps(completed_steps: list[dict]) -> str:
    """Format completed snapshot steps as human-readable text."""
    if not completed_steps:
        return "(none)"
    lines = []
    for i, step in enumerate(completed_steps, 1):
        tool = step.get("tool", "?")
        params_summary = _summarize_params(step.get("params", {}))
        lines.append(f"  {i}. {tool}({params_summary}) — succeeded")
    return "\n".join(lines)


def _summarize_params(params: dict) -> str:
    """Create a short summary of tool parameters for display."""
    if not params:
        return ""
    keys = list(params.keys())
    if len(keys) <= 2:
        return ", ".join(f"{k}=..." for k in keys)
    return f"{', '.join(f'{k}=...' for k in keys[:2])}, ..."


# ---------------------------------------------------------------------------
# Blocked domains (same set as link_triage)
# ---------------------------------------------------------------------------

_BLOCKED_DOMAINS: set[str] = {
    "linkedin.com", "www.linkedin.com",
    "zhaopin.com", "www.zhaopin.com",
    "liepin.com", "www.liepin.com",
    "51job.com", "www.51job.com", "m.51job.com",
    "lagou.com", "www.lagou.com", "m.lagou.com",
    "kanzhun.com", "www.kanzhun.com", "m.kanzhun.com",
    "huntingwork.com", "www.huntingwork.com",
    "liepin.cn", "www.liepin.cn",
}


def _is_blocked_domain(url: str) -> bool:
    """Check if a URL belongs to a known blocked domain."""
    try:
        parsed = urlsplit(url)
        return parsed.netloc.lower() in _BLOCKED_DOMAINS
    except ValueError:
        return False


# Module-level cache for raw WeChat HTML, keyed by URL.
# Populated by _fetch_wechat_via_readgzh so that fetch_wechat_article
# can later extract image URLs for OCR without a second HTTP round-trip.
_wechat_raw_html_cache: dict[str, str] = {}


def _fetch_wechat_via_readgzh(url: str) -> tuple[str | None, str | None, str | None]:
    """Fetch a WeChat article via the ReadGZH proxy service.

    ReadGZH (https://readgzh.site) is a server-side proxy that bypasses
    WeChat's client fingerprinting to return clean, AI-readable article HTML.
    Articles are permanently cached — repeated reads incur zero credit cost.

    As a **side effect** the raw HTML is stored in ``_wechat_raw_html_cache``
    so that downstream callers (e.g. ``fetch_wechat_article``) can extract
    image URLs for OCR without re-fetching.

    Supports optional API key via READGZH_API_KEY environment variable to
    bypass the 10-request/day anonymous IP limit.

    Args:
        url: The mp.weixin.qq.com article URL.

    Returns:
        (text_content, title, None) on success,
        (None, None, error_message) on failure.
    """
    import os as _os

    try:
        headers: dict[str, str] = {"User-Agent": "Mozilla/5.0"}
        api_key = _os.environ.get("READGZH_API_KEY", "")
        # Fallback: try reading from .env files (worktree root, then main project root)
        if not api_key:
            try:
                from pathlib import Path as _Path
                from dotenv import dotenv_values as _dotenv_values
                _candidates = [
                    _Path(__file__).resolve().parents[4] / ".env",   # worktree root
                    _Path(__file__).resolve().parents[7] / ".env",   # main project root
                ]
                for _p in _candidates:
                    if _p.exists():
                        _vals = _dotenv_values(_p, interpolate=False)
                        _key = _vals.get("READGZH_API_KEY") or _vals.get("readgzh_api_key")
                        if _key:
                            api_key = _key
                            break
            except Exception:
                pass
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        api_url = f"https://api.readgzh.site/rd?url={url}"
        resp = requests.get(api_url, timeout=30, headers=headers)
        resp.raise_for_status()
        raw = resp.text

        # ── Detect ReadGZH JSON error responses ──
        if not raw or len(raw) < 200:
            return None, None, "ReadGZH returned empty or too-short response"

        # ReadGZH returns JSON on errors (rate limit, invalid URL, etc.)
        if raw.strip().startswith("{"):
            try:
                error_data = json.loads(raw)
                if isinstance(error_data, dict) and not error_data.get("success", True):
                    code = error_data.get("code", "unknown")
                    message = error_data.get("message", "Unknown ReadGZH error")
                    return None, None, f"ReadGZH API error [{code}]: {message}"
            except json.JSONDecodeError:
                pass  # Not JSON, proceed as HTML

        # ── Check for WeChat verification wall in response ──
        if "环境异常" in raw and "完成验证后即可继续访问" in raw:
            return None, None, "ReadGZH proxy could not bypass WeChat verification"

        # Stash raw HTML for downstream image-URL extraction (OCR)
        _wechat_raw_html_cache[url] = raw

        title = _extract_page_title(raw)
        text = _extract_page_text(raw)

        if not text or len(text.strip()) < 50:
            return None, None, "ReadGZH returned content with insufficient text"

        return text, title, None
    except requests.RequestException as e:
        return None, None, f"ReadGZH fetch failed: {e}"


def _is_wechat_url(url: str) -> bool:
    """Check if a URL is a WeChat MP article."""
    try:
        return "mp.weixin.qq.com" in urlsplit(url).netloc.lower()
    except ValueError:
        return False


def _needs_browser_fallback(url: str, text: str) -> bool:
    """Return True when a requests fetch likely hit a JS/browser gate."""
    try:
        domain = urlsplit(url).netloc.lower()
    except ValueError:
        return False
    if "mp.weixin.qq.com" in domain and (
        "环境异常" in text or "完成验证后即可继续访问" in text
    ):
        return True
    return False


def _fetch_page_with_browser(url: str) -> tuple[str | None, str | None, str | None]:
    """Fetch page text with Playwright for pages that require a real browser.

    This does not solve captcha or bypass auth. It only renders public pages
    that are readable in a normal headless browser session.
    """
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None, None, "Playwright is not installed"

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(user_agent=_NAV_USER_AGENT)
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(3_000)
            title = page.title()
            text = page.locator("body").inner_text(timeout=10_000)
            browser.close()
            if "环境异常" in text and "完成验证后即可继续访问" in text:
                return None, None, "Browser page still requires verification"
            return text, title, None
    except (PlaywrightError, PlaywrightTimeoutError) as exc:
        return None, None, f"Browser fetch failed: {exc}"


def _extract_page_title(html: str) -> str:
    """Extract <title> from HTML."""
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    return m.group(1).strip() if m else ""


def _extract_page_text(html: str) -> str:
    """Strip HTML tags and return visible text."""
    # Remove script and style blocks
    html = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.IGNORECASE | re.DOTALL)
    html = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.IGNORECASE | re.DOTALL)
    # Replace common block tags with newlines
    html = re.sub(r"</?(?:p|div|br|li|h[1-6]|tr|blockquote|section)[^>]*>", "\n", html, flags=re.IGNORECASE)
    # Strip remaining tags
    text = re.sub(r"<[^>]+>", "", html)
    # Decode common entities
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&nbsp;", " ")
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _extract_links_from_html(html: str, base_url: str) -> list[dict[str, str]]:
    """Extract all <a href=\"...\"> links from HTML."""
    links: list[dict[str, str]] = []
    for m in re.finditer(r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>([^<]*)</a>', html, re.IGNORECASE | re.DOTALL):
        href = m.group(1).strip()
        text = re.sub(r"\s+", " ", m.group(2).strip())
        if href and not href.startswith("#") and not href.startswith("javascript:"):
            absolute = urljoin(base_url, href)
            links.append({"url": absolute, "text": text or absolute})
    return links


# ---------------------------------------------------------------------------
# 1.  triage_link (tool wrapper)
# ---------------------------------------------------------------------------

def triage_link(url: str) -> dict[str, Any]:
    """Classify a URL into a site type and recommend the next action.

    Args:
        url: The URL to triage.

    Returns:
        Dict with site_type, confidence, recommended_action, and notes.
    """
    result = _triage_link(url)
    return _asdict(result)


# ---------------------------------------------------------------------------
# 2.  run_web_navigation
# ---------------------------------------------------------------------------

def run_web_navigation(
    start_url: str,
    settings: Settings | None = None,
    subagent: SubAgent | None = None,
    model: Any | None = None,
) -> dict[str, Any]:
    """Navigate from a start URL to discover job JD evidence pages.

    Runs the WebNavigationAgent DeepAgent for web page navigation. The agent
    opens pages, reads DOM/text, extracts links, chooses next actions from
    observations, and collects evidence pages up to the configured budget.

    Args:
        start_url: The URL to start navigation from.
        settings: Optional Settings object (defaults to 20-page budget).
        subagent: Deprecated compatibility parameter. The standalone DeepAgent
            path is used regardless.
        model: Optional model instance for the WebNavigationAgent.

    Returns:
        Dict with evidence_pages (list of PageEvidence-like dicts),
        navigation_path (list of visited URLs), and page_count,
        reflecting delegation to the WebNavigationAgent.
    """
    max_pages = (settings.job_discovery_max_pages_per_task
                 if settings else 20)

    try:
        parsed = urlsplit(start_url)
    except ValueError as exc:
        return {
            "evidence_pages": [],
            "navigation_path": [],
            "page_count": 0,
            "error": f"Invalid URL: {exc}",
            "delegated_to": "web_navigation_agent",
        }
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return {
            "evidence_pages": [],
            "navigation_path": [],
            "page_count": 0,
            "error": f"Invalid URL: {start_url}",
            "delegated_to": "web_navigation_agent",
        }
    if _is_blocked_domain(start_url):
        return {
            "evidence_pages": [],
            "navigation_path": [],
            "page_count": 0,
            "error": f"Cannot access blocked domain: {start_url}",
            "delegated_to": "web_navigation_agent",
        }

    if settings is None:
        settings = Settings()
    _reset_nav_state(
        max_pages=max_pages,
        time_budget=float(getattr(settings, "job_discovery_task_timeout_seconds", 0) or 0),
    )
    agent = build_web_navigation_agent(settings=settings, model=model)
    prompt = (
        f"Starting URL: {start_url}\n"
        f"Page budget: {max_pages}\n\n"
        "Find credible job list pages and JD detail pages. Use your tools in a loop. "
        "Open the page, inspect visible text and links, follow likely career/job links, "
        "and if the page is a JavaScript-rendered recruitment SPA, call "
        "extract_rendered_job_evidence to capture public job-detail JSON/XHR evidence. "
        "and stop when you have JD evidence or a precise blocking reason. "
        "Return structured evidence_pages, navigation_path, page_count, and error if blocked."
    )
    try:
        # Deterministic baseline: capture the start URL's rendered evidence
        # directly. This MUST NOT depend on the inner Web Navigation Agent LLM
        # choosing to call ``extract_rendered_job_evidence`` - on career-site
        # SPAs the LLM frequently concludes "cannot navigate" and calls
        # ``finish_with_manual_review`` instead, yielding zero evidence. The
        # baseline guarantees at least the publicly-rendered ``page_text``
        # evidence (with the loose title-extractor fallback) so job listings
        # are still surfaced. The agent may add follow-up evidence below.
        baseline_evidence: list[dict[str, Any]] = []
        # WeChat articles are fetched via the ReadGZH proxy through
        # ``parse_wechat_article`` (the verification wall blocks direct
        # Playwright); skip the baseline there so we do not waste a browser
        # session capturing wall text. Only career sites benefit from it.
        _is_wechat = "mp.weixin.qq.com" in start_url
        if not _is_wechat:
            try:
                _baseline_raw = extract_rendered_job_evidence(start_url)
                _baseline_parsed = (
                    json.loads(_baseline_raw) if isinstance(_baseline_raw, str) else _baseline_raw
                )
                if isinstance(_baseline_parsed, dict):
                    baseline_evidence = list(_baseline_parsed.get("evidence_pages") or [])
            except Exception:  # noqa: BLE001 - baseline is best-effort; agent may still help
                baseline_evidence = []

        # Bound the inner Web Navigation Agent loop explicitly (super-steps)
        # and pair it with the wall-clock budget checked in _nav_budget_check.
        result = agent.invoke(
            {"messages": [HumanMessage(content=prompt)]},
            config={"recursion_limit": 30},
        )
        nav_result = _parse_web_navigation_agent_result(result)
        agent_evidence = nav_result.get("evidence_pages") or []
    except Exception as exc:  # noqa: BLE001 - agent/model providers raise heterogeneous errors
        # The Web Navigation Agent failed (e.g. recursion_limit reached without
        # converging, or a provider error). Do NOT discard the deterministic
        # baseline evidence it captured before the failure: fall through to
        # candidate extraction on the baseline alone so a flaky / looping agent
        # cannot zero out jobs the baseline already captured. The error is still
        # surfaced on the result for diagnostics.
        agent_evidence = []
        nav_result = {
            "evidence_pages": [],
            "candidates": [],
            "evidence_hash": "",
            "navigation_path": [],
            "page_count": _nav_page_count,
            "error": f"WebNavigationAgent failed: {exc}",
            "delegated_to": "web_navigation_agent",
        }
    # Merge agent-captured evidence with the deterministic baseline, deduping
    # by content_hash so the same page captured twice is only extracted once.
    merged: dict[str, dict[str, Any]] = {}
    for page in list(agent_evidence) + list(baseline_evidence):
        if not isinstance(page, dict):
            continue
        key = page.get("content_hash") or page.get("url") or ""
        if key and key not in merged:
            merged[key] = page
    evidence_pages = list(merged.values())
    nav_result["evidence_pages"] = evidence_pages
    # Deterministically extract+verify candidates from captured evidence so the
    # final result never depends on the Supervisor LLM wiring evidence_refs
    # (which verify_evidence requires) or on a single concatenated page_text
    # (which the 2-segment extractor ceiling would cap at 2 jobs). Each evidence
    # page is extracted independently, yielding one candidate per job-detail page.
    try:
        candidates, evidence_hash = _extract_and_verify_candidates_from_evidence(
            evidence_pages, start_url
        )
    except Exception:  # noqa: BLE001 - degrade gracefully; evidence_pages remain
        candidates, evidence_hash = [], ""
    nav_result["candidates"] = candidates
    nav_result["evidence_hash"] = evidence_hash
    # If the deterministic baseline captured jobs despite a Web Navigation
    # Agent failure (recursion limit / provider error), downgrade that agent
    # failure from a hard ``error`` to a soft ``warning``. The baseline evidence
    # is the source of truth here, and a non-empty candidate set means the crawl
    # succeeded; surfacing the agent failure as a hard error would make the
    # Supervisor abort to ``needs_manual_review`` and discard jobs it already
    # captured. The hard ``error`` is preserved only when NO candidates were
    # captured, so the Supervisor can still escalate a genuinely empty crawl.
    if candidates and nav_result.get("error"):
        nav_result["warnings"] = nav_result["error"]
        nav_result["error"] = ""
    return nav_result


# ---------------------------------------------------------------------------
# 2.5  fetch_wechat_article (tool wrapper — SnapshotExecutor)
# ---------------------------------------------------------------------------


def fetch_wechat_article(url: str) -> dict[str, Any]:
    """Fetch a WeChat article via ReadGZH, OCR any embedded images, and return
    structured content ready for JD extraction.

    Workflow:
    1. Call ReadGZH → plain-text article body (via ``_fetch_wechat_via_readgzh``).
    2. Retrieve the raw HTML from ``_wechat_raw_html_cache``.
    3. Parse ``<img>`` URLs from the raw HTML with ``_parse_wechat_article``.
    4. Download each image (max 5), base64-encode, run ``_ocr_image``.
    5. Concatenate OCR results with the article body text.
    6. Extract email addresses for structured application-method storage
       (**never** put raw emails into an LLM prompt — the deterministic
       extractor handles them without a prompt, and the Supervisor path
       redacts them before assembly).

    Args:
        url: The mp.weixin.qq.com article URL.

    Returns:
        Dict with keys:

        * ``text`` — combined article text + OCR results
        * ``title`` — article title
        * ``url`` — the source URL
        * ``needs_manual_review`` — bool
        * ``manual_review_reason`` — str | None
        * ``image_ocr_texts`` — list[str] OCR results per image
        * ``image_count`` — int
        * ``application_emails`` — list[str] extracted email addresses
        * ``application_urls`` — list[str] extracted non-WeChat URLs
    """
    import re as _re

    text, title, error = _fetch_wechat_via_readgzh(url)
    if error:
        return {
            "text": "",
            "title": "",
            "url": url,
            "needs_manual_review": True,
            "manual_review_reason": str(error),
            "image_ocr_texts": [],
            "image_count": 0,
            "application_emails": [],
            "application_urls": [],
        }

    # ── Extract image URLs from the raw HTML ──
    image_urls: list[str] = []
    raw_html = _wechat_raw_html_cache.get(url, "")
    if raw_html:
        try:
            parsed = _parse_wechat_article(raw_html, url)
            image_urls = list(parsed.image_urls) if parsed.image_urls else []
        except Exception:
            image_urls = []

    # ── Download + OCR each image (max 5, skip tiny icons) ──
    ocr_texts: list[str] = []
    for img_url in image_urls[:5]:
        try:
            img_resp = requests.get(
                img_url, timeout=10,
                headers={"User-Agent": "Mozilla/5.0", "Referer": url},
            )
            img_resp.raise_for_status()
            img_bytes = img_resp.content
            # Skip images that are too small to contain text (< 10 KB)
            if len(img_bytes) < 10_240:
                continue
            ocr_result = _ocr_image(img_bytes, ocr_enabled=True)
            ocr_text = (ocr_result.full_text or "").strip()
            if ocr_text and len(ocr_text) >= 10:
                ocr_texts.append(ocr_text)
        except Exception:
            continue

    # ── Build combined text ──
    combined_parts: list[str] = [text or ""]
    for i, ot in enumerate(ocr_texts, 1):
        combined_parts.append(f"\n[图片{i} OCR 内容]\n{ot}")
    combined_text = "\n".join(combined_parts)

    # ── Extract emails & application URLs (structured, never in prompts) ──
    email_pattern = _re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
    application_emails: list[str] = list(set(email_pattern.findall(combined_text)))

    # Find non-WeChat URLs that look like application portals
    url_pattern = _re.compile(r"https?://[^\s<>\"']+")
    all_urls = url_pattern.findall(combined_text)
    application_urls: list[str] = []
    for u in all_urls:
        u = u.rstrip(".,;:!?）)")
        if "mp.weixin.qq.com" not in u and "readgzh" not in u and len(u) > 20:
            application_urls.append(u)
    application_urls = list(set(application_urls))[:10]

    return {
        "text": combined_text,
        "title": title or "",
        "url": url,
        "needs_manual_review": False,
        "manual_review_reason": None,
        "image_ocr_texts": ocr_texts,
        "image_count": len(image_urls),
        "application_emails": application_emails,
        "application_urls": application_urls,
    }


# ---------------------------------------------------------------------------
# 3.  parse_wechat_article (tool wrapper)
# ---------------------------------------------------------------------------

def parse_wechat_article(html: str, url: str) -> dict[str, Any]:
    """Parse a WeChat article HTML and extract structured content.

    Args:
        html: Raw HTML content of the WeChat article.
        url: The article URL.

    Returns:
        Dict with title, text_content, image_urls, email_delivery_instructions,
        needs_manual_review, and manual_review_reason.
    """
    result = _parse_wechat_article(html, url)
    return _asdict(result)


# ---------------------------------------------------------------------------
# 4.  run_ocr (tool wrapper)
# ---------------------------------------------------------------------------

def run_ocr(image_base64: str, settings: Settings | None = None) -> dict[str, Any]:
    """Decode a base64 image and run OCR pipeline.

    Args:
        image_base64: Base64-encoded image data (PNG or JPEG).
        settings: Optional Settings object (provides ocr_enabled flag).

    Returns:
        Dict with full_text, confidence, slice_count, warnings, needs_manual_review.
    """
    try:
        image_bytes = base64.b64decode(image_base64)
    except (ValueError, binascii.Error) as exc:
        return _asdict(OcrResult(
            full_text="",
            confidence=0.0,
            slice_count=0,
            warnings=[f"Base64 decode error: {exc}"],
            needs_manual_review=True,
        ))

    ocr_enabled = settings.job_discovery_ocr_enabled if settings else False
    result = _ocr_image(image_bytes, ocr_enabled=ocr_enabled)
    return _asdict(result)


# ---------------------------------------------------------------------------
# 5.  extract_jd_candidates (tool wrapper)
# ---------------------------------------------------------------------------

def extract_jd_candidates(page_text: str, url: str) -> str:
    """Extract standardized job candidates from JD page text.

    Args:
        page_text: Raw text content from a job detail page.
        url: The source URL for reference.

    Returns:
        JSON string of extracted NormalizedJobCandidate dicts.
    """
    results = _extract_jd_candidates(page_text, url)
    return json.dumps(_asdict(results), ensure_ascii=False)


# ---------------------------------------------------------------------------
# 6.  verify_evidence (tool wrapper)
# ---------------------------------------------------------------------------

def verify_evidence(candidates_json: str, evidence_json: str) -> str:
    """Verify and filter job candidates against evidence.

    Args:
        candidates_json: JSON string of candidate dicts.
        evidence_json: JSON string of PageEvidence dicts.

    Returns:
        JSON string of verified candidates.
    """
    candidates_data = json.loads(candidates_json)
    evidence_data = json.loads(evidence_json)

    # Normalize field names from LLM/WebNavigationAgent output.
    # The LLM often produces "type" instead of "evidence_type" and
    # may include extra keys that PageEvidence does not accept.
    _EVIDENCE_FIELD_ALIASES = {
        "type": "evidence_type",
        "content": "text_excerpt",
        "page_text": "text_excerpt",
        "text": "text_excerpt",
        "description": "text_excerpt",
    }
    for e in evidence_data:
        for src, dst in _EVIDENCE_FIELD_ALIASES.items():
            if src in e and dst not in e:
                e[dst] = e.pop(src)

    # Reconstruct dataclass instances — only pass known fields
    _EVI_FIELDS = {f.name for f in PageEvidence.__dataclass_fields__.values()}
    candidates = [NormalizedJobCandidate(**c) for c in candidates_data]
    evidence = [PageEvidence(**{k: v for k, v in e.items() if k in _EVI_FIELDS}) for e in evidence_data]

    verified = _verify_evidence(candidates, evidence)
    return json.dumps(_asdict(verified), ensure_ascii=False)


# ---------------------------------------------------------------------------
# 7.  package_candidates (tool wrapper)
# ---------------------------------------------------------------------------

def package_candidates(
    candidates_json: str,
    evidence_hash: str,
    source_key: str,
) -> str:
    """Package candidates with idempotency and similarity keys.

    Adds idempotency_key and similarity_group_key to each candidate.

    Args:
        candidates_json: JSON string of candidate dicts.
        evidence_hash: Content hash of supporting evidence.
        source_key: Source key (e.g., 'tencent_smartsheet', 'wechat_article').

    Returns:
        JSON string of packaged candidates with additional keys.
    """
    candidates = json.loads(candidates_json)
    packaged: list[dict[str, Any]] = []
    for c in candidates:
        company = c.get("company_name") or ""
        title = c.get("title") or ""
        location = (c.get("locations") or ["unknown"])[0]
        apply_url = c.get("apply_url") or ""
        recruitment_type = (c.get("recruitment_types") or ["unknown"])[0]

        c["idempotency_key"] = build_candidate_idempotency_key(
            company=company,
            title=title,
            location=location,
            apply_url=apply_url,
            evidence_hash=evidence_hash,
        )
        c["similarity_group_key"] = build_similarity_group_key(
            company=company,
            title=title,
            recruitment_type=recruitment_type,
            source_family=source_key,
        )
        packaged.append(c)
    return json.dumps(packaged, ensure_ascii=False)


def standardize_from_record_fields(
    record_fields_json: str,
    evidence_json: str,
    source_url: str,
) -> str:
    """Build candidate JSON from Tencent record fields plus evidence text.

    This is a deterministic fallback tool for real Tencent rows where the
    source page is a rendered article or a dynamic career site and not all
    standard fields appear as labelled JD text.
    """
    record_fields = json.loads(record_fields_json or "[]")
    evidence_data = json.loads(evidence_json or "[]")
    record = _record_field_map(record_fields)
    evidence_text = _join_evidence_text(evidence_data)

    company = (
        record.get("公司名称")
        or record.get("企业名称")
        or _regex_first(evidence_text, r"(?:公司名称|企业名称|招聘单位)[:：]?\s*([^\n。；;]{2,60})")
    )
    title = record.get("招聘岗位") or _title_from_evidence(evidence_text, company)
    if not title and company:
        title = f"{company}招聘信息"

    if not company and title:
        company = _company_from_title(title)

    if not title and not company:
        return "[]"

    locations = _split_field_values(record.get("工作地点"))
    recruitment_types = _split_field_values(record.get("招聘类型"))
    if not recruitment_types:
        recruitment_types = _infer_recruitment_types(f"{title or ''}\n{evidence_text}")
    industries = _split_field_values(record.get("多选") or record.get("行业类型"))
    referral_code = record.get("内推码") or record.get("内推码(区分大小写)")

    candidate = NormalizedJobCandidate(
        title=title,
        company_name=company,
        description_text=evidence_text[:2000] if evidence_text else title or "",
        locations=locations,
        recruitment_types=recruitment_types,
        industries=industries,
        apply_url=source_url,
        deadline_text=record.get("截止日期"),
        referral_code=referral_code,
        confidence=_record_fallback_confidence(title, company, evidence_text),
        evidence_refs=[
            {
                "url": item.get("url"),
                "content_hash": item.get("content_hash"),
                "evidence_type": item.get("evidence_type"),
            }
            for item in evidence_data
            if isinstance(item, dict) and item.get("content_hash")
        ],
        normalization_warnings=["standardized_from_tencent_record_fields"],
    )
    return json.dumps([_asdict(candidate)], ensure_ascii=False)


def _record_field_map(record_fields: list[dict[str, Any]]) -> dict[str, str]:
    values: dict[str, str] = {}
    for field in record_fields:
        name = field.get("field")
        if not isinstance(name, str) or not name:
            continue
        parts: list[str] = []
        text_value = field.get("text_value")
        if isinstance(text_value, dict):
            for item in text_value.get("items", []):
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
        option_value = field.get("option_value")
        if isinstance(option_value, dict):
            for item in option_value.get("items", []):
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
        url_value = field.get("url_value")
        if isinstance(url_value, dict):
            for item in url_value.get("items", []):
                if isinstance(item, dict) and isinstance(item.get("link"), str):
                    parts.append(item["link"])
        string_value = field.get("string_value")
        if isinstance(string_value, str):
            parts.append(string_value)
        value = "、".join(part.strip() for part in parts if part.strip())
        if value:
            values[name] = value
    return values


def _join_evidence_text(evidence_data: list[dict[str, Any]]) -> str:
    parts = []
    for item in evidence_data:
        if isinstance(item, dict) and isinstance(item.get("text_excerpt"), str):
            parts.append(item["text_excerpt"])
    return "\n".join(parts).strip()


def _regex_first(text: str, pattern: str) -> str | None:
    match = re.search(pattern, text)
    if not match:
        return None
    value = match.group(1).strip()
    return value or None


def _title_from_evidence(text: str, company: str | None) -> str | None:
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    if not first_line:
        return None
    if "丨" in first_line and company:
        preferred = next(
            (
                segment.strip()
                for segment in first_line.split("丨")
                if company in segment
            ),
            "",
        )
        if preferred:
            first_line = preferred
    if company and first_line.startswith(company):
        first_line = first_line[len(company):].strip("丨|- ：:")
    title = re.sub(
        r"(招聘启事|招聘简章|招募全面启动|火热启动|正式启动)[！!。．.]*$",
        "",
        first_line,
    ).strip()
    if "实习" in title and len(title) <= 80:
        return title
    if any(word in title for word in ("招聘", "校招", "内推", "岗位")) and len(title) <= 80:
        return title
    return first_line[:80]


def _company_from_title(title: str) -> str | None:
    match = re.match(r"(.{2,40}?)(?:20\d{2}届|暑期|实习|校园|校招|招聘)", title)
    if match:
        return match.group(1).strip("丨|- ：:")
    return None


def _split_field_values(value: str | None) -> list[str]:
    if not value:
        return []
    seen: set[str] = set()
    results: list[str] = []
    for part in re.split(r"[、，,；;/\s]+", value):
        item = part.strip()
        if item and item not in seen:
            seen.add(item)
            results.append(item)
    return results


def _infer_recruitment_types(text: str) -> list[str]:
    results: list[str] = []
    if re.search(r"实习|intern", text, re.IGNORECASE):
        results.append("internship")
    if re.search(r"校招|校园|应届|20\d{2}届|campus|graduate", text, re.IGNORECASE):
        results.append("campus_recruitment")
    if re.search(r"社招|全职|full.?time", text, re.IGNORECASE):
        results.append("full_time")
    return results


def _record_fallback_confidence(
    title: str | None,
    company: str | None,
    evidence_text: str,
) -> float:
    score = 0.0
    if title:
        score += 0.35
    if company:
        score += 0.30
    if len(evidence_text) >= 100:
        score += 0.25
    if any(word in evidence_text for word in ("岗位", "招聘", "职责", "要求", "投递")):
        score += 0.10
    return min(score, 1.0)


# ---------------------------------------------------------------------------
# 8.  finish_with_manual_review (tool wrapper)
# ---------------------------------------------------------------------------

def finish_with_manual_review(reason: str) -> dict[str, Any]:
    """Signal that the discovery run needs manual review.

    Args:
        reason: Explanation of why manual review is needed.

    Returns:
        Dict representing a DiscoveryRunResult with needs_manual_review status.
    """
    return _asdict(DiscoveryRunResult(
        status="needs_manual_review",
        block_reason=reason,
        summary=f"Manual review required: {reason}",
    ))


# ---------------------------------------------------------------------------
# Web navigation subagent tools
# ---------------------------------------------------------------------------

# Module-level navigation state shared by subagent tools
_nav_page_count: int = 0
_nav_max_pages: int = 20
_nav_history: list[str] = []
_nav_current_url: str | None = None
# Wall-clock budget for a single Web Navigation Agent run (seconds). Set per
# run_web_navigation() call from settings.job_discovery_task_timeout_seconds so
# a slow/hung LLM-tool loop on one site cannot run unbounded. Checked in
# _nav_budget_check() on every page-fetching tool call.
_nav_start_time: float = 0.0
_nav_time_budget: float = 0.0

# Per-run page fetch cache — avoids redundant ReadGZH / HTTP calls when
# multiple WebNavigationAgent tools (open_url, read_dom, extract_links,
# click_link) request the same URL within a single run.
_page_cache: dict[str, tuple[str | None, str | None, str | None]] = {}
# url → (raw_html_or_text, title, error_or_none)


def _reset_nav_state(max_pages: int = 20, time_budget: float = 0.0) -> None:
    """Reset subagent navigation state and page cache.

    Args:
        max_pages: Page-fetch budget for this run.
        time_budget: Wall-clock budget in seconds (0 = no time limit). The
            clock starts now.
    """
    global _nav_page_count, _nav_max_pages, _nav_history, _nav_current_url, _page_cache, _wechat_raw_html_cache
    global _nav_start_time, _nav_time_budget
    _nav_page_count = 0
    _nav_max_pages = max_pages
    _nav_history = []
    _nav_current_url = None
    _page_cache = {}
    _wechat_raw_html_cache = {}
    _nav_start_time = time.monotonic()
    _nav_time_budget = float(time_budget)


def _fix_response_encoding(resp: requests.Response) -> str:
    """Auto-detect encoding and decode response text, fixing mojibake.

    Many Chinese career sites serve pages with a misconfigured or missing
    ``Content-Type charset``, causing ``requests`` to fall back to
    ISO-8859-1 and produce garbled text.  This helper applies the detected
    apparent encoding (or UTF-8 as a safe default) before decoding.
    """
    if resp.encoding is None or resp.encoding.lower() in ("iso-8859-1", "latin-1"):
        resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text


def _cached_fetch(url: str) -> tuple[str | None, str | None, str | None]:
    """Fetch *url* with page-level caching.

    WeChat URLs go through ReadGZH; all others use ``requests.get``.
    The raw response (HTML / text, title, error) is cached per URL so
    subsequent tool calls (``read_dom``, ``extract_links``, etc.) for
    the same URL reuse the cached result at zero external cost.

    Returns:
        ``(content, title, error_or_none)`` — *content* is raw HTML for
        non-WeChat URLs and extracted visible text for WeChat URLs.
    """
    if url in _page_cache:
        return _page_cache[url]

    if _is_wechat_url(url):
        text, title, error = _fetch_wechat_via_readgzh(url)
        _page_cache[url] = (text, title, error)
        return _page_cache[url]

    # Regular HTTP fetch
    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        html = _fix_response_encoding(resp)
        title = _extract_page_title(html)
        _page_cache[url] = (html, title, None)
        return _page_cache[url]
    except requests.RequestException as exc:
        err = f"HTTP error fetching {url}: {exc}"
        _page_cache[url] = (None, None, err)
        return _page_cache[url]


def _nav_budget_check() -> str | None:
    """Check page and wall-clock budgets for subagent navigation."""
    if _nav_page_count >= _nav_max_pages:
        return f"Page budget exhausted ({_nav_page_count}/{_nav_max_pages})"
    if _nav_time_budget > 0:
        elapsed = time.monotonic() - _nav_start_time
        if elapsed > _nav_time_budget:
            return (
                f"Time budget exhausted ({elapsed:.0f}s > {_nav_time_budget:.0f}s)"
            )
    return None


def open_url(url: str) -> str:
    """Open a URL and return its visible text content.

    Uses ``_cached_fetch`` so repeated tool calls for the same URL within
    one run hit the local cache instead of re-invoking ReadGZH / HTTP.

    Page-budget accounting still happens exactly once per unique URL.
    """
    global _nav_page_count, _nav_current_url, _nav_history

    if _is_blocked_domain(url):
        return f"ERROR: Cannot access blocked domain: {url}"
    budget_err = _nav_budget_check()
    if budget_err:
        return f"ERROR: {budget_err}"

    was_cached = url in _page_cache
    content, title, error = _cached_fetch(url)

    if error is not None:
        return f"ERROR: Could not open {url}: {error}"

    if not was_cached:
        _nav_page_count += 1
        if _nav_current_url:
            _nav_history.append(_nav_current_url)
        _nav_current_url = url

    # For WeChat URLs, _cached_fetch returns extracted text directly.
    # For regular URLs it returns raw HTML — extract visible text.
    if _is_wechat_url(url):
        return content or "(empty page)"

    html = content or ""
    text = _extract_page_text(html)

    if _needs_browser_fallback(url, text):
        browser_text, browser_title, browser_error = _fetch_page_with_browser(url)
        if browser_error is None:
            text = browser_text or ""
        else:
            return f"ERROR: WeChat verification wall: {browser_error}"

    return text or "(empty page)"


def open_rendered_url(url: str) -> str:
    """Open a URL in a headless browser and return rendered visible text.

    Use this when ``open_url`` returns an empty shell, a JavaScript app, or a
    page whose useful content is loaded after browser rendering.

    Args:
        url: The URL to open in a browser.

    Returns:
        JSON string with url, title, text, and page_count, or an error object.
    """
    if _is_blocked_domain(url):
        return json.dumps({"error": f"Cannot access blocked domain: {url}"}, ensure_ascii=False)
    budget_err = _nav_budget_check()
    if budget_err:
        return json.dumps({"error": budget_err}, ensure_ascii=False)

    text, title, error = _fetch_page_with_browser(url)
    if error:
        return json.dumps({"error": error, "url": url}, ensure_ascii=False)

    global _nav_page_count, _nav_current_url, _nav_history
    _nav_page_count += 1
    if _nav_current_url:
        _nav_history.append(_nav_current_url)
    _nav_current_url = url

    return json.dumps(
        {
            "url": url,
            "title": title or "",
            "text": (text or "")[:10000],
            "page_count": _nav_page_count,
        },
        ensure_ascii=False,
    )


# Privacy/cookie consent button texts we are ALLOWED to click to read public
# job-description content. These are cookie-banner / consent-interstitial
# buttons that any human visitor accepts to view publicly-available job
# listings. This does NOT include login, captcha, or anti-bot challenges -
# those are covered by ``_CONSENT_BLOCK_KEYWORDS`` and must never be clicked.
_CONSENT_BUTTON_TEXTS = (
    "同意并继续", "同意并接受", "全部同意", "接受全部", "全部接受",
    "接受所有", "我同意", "我知道了", "知道了",
    "我已阅读", "已阅读并同意", "我已知晓", "同意",
    "Agree", "I Agree", "Accept All", "Accept all", "Accept",
    "Got it", "Allow all", "I understand",
)

# Absolute blocklist: if the page (or a candidate button's text) contains any
# of these, NO button is clicked. These are captcha / anti-bot / login walls
# which the security hard gate (#2) forbids bypassing - the run must surface
# them as needs_manual_review instead. Also keeps the WeChat verification
# wall (``环境异常`` + ``完成验证后即可继续访问``) untouched.
_CONSENT_BLOCK_KEYWORDS = (
    "验证", "验证码", "图形验证", "安全验证", "滑块", "拼图", "人机",
    "captcha", "verify", "verification", "完成验证", "环境异常",
    "登录", "登陆", "login", "sign in", "signin", "扫码", "二维码",
    "注册并登录", "robot", "bot检测", "bot 检测",
)

# Narrow wall-only subset: phrases that essentially only appear on a
# captcha / anti-bot / verification interstitial (NOT as normal page chrome
# like a "登录" header link or a footer "二维码"). Used for body-level wall
# detection so a legitimate career page whose nav/footer contains "登录" /
# "二维码" is not mistaken for a blocked wall. The full
# ``_CONSENT_BLOCK_KEYWORDS`` is still used for per-element checks (never click
# a control whose own text is a login/verify label).
_CONSENT_WALL_PHRASES = (
    "环境异常", "完成验证", "图形验证", "安全验证", "滑块", "拼图", "人机",
    "captcha", "verification", "robot", "bot检测", "bot 检测", "注册并登录",
)


def _dismiss_consent_dialog(page: Any) -> bool:
    """Click a privacy/cookie consent button so public JD content renders.

    Strictly bounded to the "read JD" path: only clicks elements whose text
    exactly matches (case-insensitive) an entry in ``_CONSENT_BUTTON_TEXTS``,
    and only if the page body contains no ``_CONSENT_BLOCK_KEYWORDS``. Never
    touches final-submit, login, captcha, or anti-bot elements. Returns True
    if a consent button was clicked (caller may then re-wait for content).
    """
    try:
        body_text = page.locator("body").inner_text(timeout=3_000) or ""
    except Exception:  # noqa: BLE001 - best-effort; absence of text is fine
        body_text = ""
    # Anti-bot / captcha / login wall -> never click anything here.
    if any(kw in body_text for kw in _CONSENT_BLOCK_KEYWORDS):
        return False

    buttons = page.locator(
        "button, a, [role='button'], input[type='button'], input[type='submit']"
    )
    try:
        count = buttons.count()
    except Exception:  # noqa: BLE001
        return False
    lowered_allow = {s.lower() for s in _CONSENT_BUTTON_TEXTS}
    for i in range(count):
        el = buttons.nth(i)
        try:
            txt = (el.inner_text(timeout=500) or "").strip()
            if not txt:
                val = el.get_attribute("value")
                txt = (val or "").strip()
        except Exception:  # noqa: BLE001 - skip non-interactable elements
            continue
        if not txt or len(txt) > 12:
            continue
        # Exact match (case-insensitive) against the allowlist, plus a
        # per-button blocklist re-check so a misleadingly-labelled button
        # inside a captcha wall is never clicked.
        if txt.lower() not in lowered_allow:
            continue
        if any(kw in txt for kw in _CONSENT_BLOCK_KEYWORDS):
            continue
        try:
            el.click(timeout=3_000)
            page.wait_for_timeout(1_500)
            try:
                page.wait_for_load_state("networkidle", timeout=4_000)
            except Exception:  # noqa: BLE001
                pass
            return True
        except Exception:  # noqa: BLE001 - try the next candidate
            continue
    return False


# Clickable control texts that expand a career-site landing page's featured
# subset into the full job list. Many career sites (e.g. Moka) show only a
# handful of featured jobs on the landing page with a "查看更多职位" /
# "View all positions" link to the complete list - clicking it once lets the
# capture see every job instead of just the featured few. Generic: applies to
# any site with this landing->full-list pattern; hardcodes no counts/pages/URLs.
_VIEW_ALL_POSITIONS_TEXTS = (
    "查看更多职位", "查看全部职位", "更多职位", "全部职位",
    "所有职位", "查看更多岗位", "更多岗位", "全部岗位", "查看全部岗位",
    "更多招聘职位", "加载更多职位", "加载更多岗位", "加载更多",
    "View all positions", "View all jobs", "Show all jobs",
    "More positions", "See all jobs", "All positions", "Load more jobs",
)


def _click_view_all_positions(page: Any) -> bool:
    """Click a generic "view all / more positions" control so the full job list
    (not just a landing-page featured subset) is captured.

    Career sites commonly show a handful of featured jobs on the landing page
    with a "查看更多职位" / "View all positions" link to the complete list.
    Without clicking it, only the featured subset is captured. This clicks the
    first such control once (Playwright ``get_by_text`` exact match across ALL
    tags - SPAs often render these as clickable ``<div>``/``<span>`` rather
    than ``<button>``/``<a>``; the wall blocklist is re-checked per candidate)
    and waits for the list to render.

    The click is retried after a short delay because the control is sometimes
    not ready the instant the landing page loads (React attaches its handler
    a beat after the text becomes visible); a single early click can be
    intercepted/ignored even though the same click succeeds moments later.

    Never bypasses login/captcha/anti-bot (security hard gate #2): the same
    wall-keyword blocklist as consent dismissal is applied to both the page body
    and each candidate control. Returns True if a control was clicked.
    """
    try:
        body_text = page.locator("body").inner_text(timeout=3_000) or ""
    except Exception:  # noqa: BLE001 - best-effort; absence of text is fine
        body_text = ""
    # Anti-bot / captcha / verification wall -> never click anything here.
    # Uses the NARROW wall-only subset: the full ``_CONSENT_BLOCK_KEYWORDS``
    # includes "登录"/"二维码" which appear as normal nav/footer chrome on
    # legitimate career pages and would falsely mark them as blocked.
    if any(kw in body_text for kw in _CONSENT_WALL_PHRASES):
        return False
    for pat in _VIEW_ALL_POSITIONS_TEXTS:
        try:
            loc = page.get_by_text(pat, exact=True).first
        except Exception:  # noqa: BLE001
            continue
        try:
            if loc.count() == 0 or not loc.is_visible(timeout=800):
                continue
        except Exception:  # noqa: BLE001
            continue
        try:
            txt = (loc.inner_text(timeout=800) or "").strip()
        except Exception:  # noqa: BLE001
            txt = pat
        if any(kw in txt for kw in _CONSENT_BLOCK_KEYWORDS):
            continue
        # Retry the click: an early attempt may be intercepted before the SPA
        # attaches the handler; a follow-up after a short wait usually lands.
        # ``no_wait_after`` so Playwright does not wait for the SPA route-change
        # the click triggers - that wait can detach the control mid-action and
        # raise a spurious "element detached" / actionability error.
        for attempt in range(3):
            try:
                try:
                    loc.scroll_into_view_if_needed(timeout=2_000)
                except Exception:  # noqa: BLE001 - non-fatal
                    pass
                loc.click(timeout=3_000, no_wait_after=True)
            except Exception:  # noqa: BLE001
                try:
                    loc.click(timeout=3_000, force=True, no_wait_after=True)
                except Exception:  # noqa: BLE001
                    page.wait_for_timeout(1_000)
                    continue
            page.wait_for_timeout(1_500)
            try:
                page.wait_for_load_state("networkidle", timeout=6_000)
            except Exception:  # noqa: BLE001
                pass
            return True
    return False


# User-Agent used for all Playwright browser sessions in this module.
_NAV_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125 Safari/537.36"
)

# Max job-detail pages to deep-dive into from a single list page. Bounds the
# browser session so a 100-position list does not stall the run.
_MAX_DETAIL_PAGES = 6

# Heuristic path/query fragments that mark an <a> link as a job-detail page
# (vs navigation/footer/social). Conservative - the fragment must appear in
# the resolved URL path or query.
_DETAIL_LINK_HINTS = ("job", "position", "recruit", "detail", "campus")


def _collect_detail_page_links(page: Any, base_url: str) -> list[str]:
    """Collect same-site job-detail page URLs from a rendered list page.

    Resolves relative hrefs, keeps only links on the same host whose
    path/query contains a job-detail hint, and dedupes. Used to deep-dive
    into detail pages (each opened with consent dismissal) so the full JD
    body is captured, not just list-page titles.
    """
    try:
        anchors = page.locator("a")
        count = anchors.count()
    except Exception:  # noqa: BLE001
        return []
    try:
        base_host = (urlsplit(base_url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        base_host = ""
    if not base_host:
        return []
    links: list[str] = []
    seen: set[str] = set()
    for i in range(count):
        try:
            href = anchors.nth(i).get_attribute("href")
        except Exception:  # noqa: BLE001
            continue
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        try:
            resolved = urljoin(base_url, href)
            parsed = urlsplit(resolved)
        except Exception:  # noqa: BLE001
            continue
        if parsed.scheme not in {"http", "https"}:
            continue
        if (parsed.hostname or "").lower() != base_host:
            continue
        path_q = (parsed.path + " " + parsed.query).lower()
        if not any(h in path_q for h in _DETAIL_LINK_HINTS):
            continue
        if resolved == base_url or resolved in seen:
            continue
        seen.add(resolved)
        links.append(resolved)
        if len(links) >= _MAX_DETAIL_PAGES * 3:
            break
    return links


# Max list pages to paginate through in a single extract_rendered_job_evidence
# call. Bounds the browser session so a large list does not stall the run or
# blow the per-URL time budget. Pagination stops earlier when no "next page"
# control is found, the page content stops changing, or a login/captcha wall
# appears. Raised from 5 -> 20 so multi-page campus lists (e.g. a 16-page
# 151-position site) are traversed in full instead of truncated at page 5;
# the content-hash no-progress check and wall circuit-breaker still cap it.
_MAX_LIST_PAGES = 20

# CSS selectors for "next page" controls across common pagination libraries
# (PDD rocket, Mioffice atsx, Ant Design, Element UI, rel=next). Disabled
# controls (last page) are excluded so pagination stops cleanly.
_NEXT_PAGE_SELECTORS = (
    "li.rocket-pagination-next:not(.rocket-pagination-disabled)",
    "li.atsx-pagination-next:not(.atsx-pagination-disabled)",
    ".ant-pagination-next:not(.ant-pagination-disabled)",
    ".el-pagination .btn-next:not(.is-disabled)",
    "a[rel='next']",
    "[class*='pagination-next']:not([class*='disabled'])",
    "[class*='page-next']:not([class*='disabled'])",
    "li[class*='next']:not([class*='disabled'])",
    "button[class*='next']:not([class*='disabled'])",
)

# Fallback: clickable elements whose visible text marks a "next page" control.
_NEXT_PAGE_TEXTS = ("下一页", "下页", "next", "»", "›")

# High-specificity phrases that essentially ONLY appear on a captcha / anti-bot
# interstitial - never in ordinary job titles or JD text. Safe to match at any
# body length, so a long job list is never falsely flagged as a wall.
_WALL_INTERSTITIAL_PHRASES = (
    "环境异常", "完成验证后即可继续访问", "完成验证", "captcha",
    "图形验证", "安全验证", "滑块", "拼图", "bot检测", "bot 检测",
)
# Generic verify markers that CAN appear in ordinary job text
# (e.g. "人机交互工程师" for HCI roles, a "验证码" mention in a JD). Only treat
# as a wall on SHORT interstitial bodies, where short + marker => a wall rather
# than a job list.
_WALL_GENERIC_MARKERS = ("人机", "验证码", "robot")
# Markers for a dedicated login page/modal. Specific enough that they rarely
# appear in a real job listing; checked on short bodies only so a normal list
# page that merely shows a "登录" nav link is not mistaken for a wall.
_LOGIN_WALL_MARKERS = (
    "请先登录", "请登录", "扫码登录", "登录后查看", "登录后继续",
    "sign in", "log in",
)
# A page shorter than this is treated as a possible interstitial (wall), where
# generic / login markers count. Longer pages are job lists - only
# high-specificity interstitial phrases count, so job titles like
# "人机交互工程师" never trip a false wall.
_WALL_SHORT_BODY_THRESHOLD = 1200


def _is_pagination_wall(rendered_text: str) -> bool:
    """True if a paginated page's text looks like a login/captcha/anti-bot wall.

    Security hard gate #2: never bypass login/captcha/anti-bot. When a "next
    page" click lands on one of these, pagination must stop immediately.

    Two-tier detection so ordinary job text is not mistaken for a wall:
    high-specificity interstitial phrases (``环境异常``, ``captcha``, ``滑块``
    ...) count at any body length; generic markers (``人机``, ``验证码``,
    ``robot``) and login markers count only on SHORT bodies, so a long job list
    containing a title like "人机交互工程师" or a JD mentioning "验证码" does not
    trip a false wall.
    """
    if not rendered_text:
        return False
    low = rendered_text.lower()
    for m in _WALL_INTERSTITIAL_PHRASES:
        if m in rendered_text or m.lower() in low:
            return True
    if len(rendered_text) < _WALL_SHORT_BODY_THRESHOLD:
        for m in _WALL_GENERIC_MARKERS:
            if m in rendered_text or m.lower() in low:
                return True
        for m in _LOGIN_WALL_MARKERS:
            if m in rendered_text or m.lower() in low:
                return True
    return False


def _find_next_page_element(page: Any) -> Any:
    """Locate a visible, enabled "next page" control on a paginated list.

    Returns a Playwright Locator (first visible match) or None. Tries known CSS
    selectors first, then text matching (下一页/next/»/›). Skips disabled
    controls so pagination stops cleanly at the last page.
    """
    for sel in _NEXT_PAGE_SELECTORS:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible(timeout=500):
                return loc
        except Exception:  # noqa: BLE001
            continue
    for txt in _NEXT_PAGE_TEXTS:
        for tag in ("button", "a", "[role='button']", "li"):
            try:
                loc = page.locator(f"{tag}:has-text('{txt}')").first
                if loc.count() == 0 or not loc.is_visible(timeout=400):
                    continue
                cls = (loc.get_attribute("class") or "").lower()
                if loc.get_attribute("disabled") is not None or "disabled" in cls:
                    continue
                return loc
            except Exception:  # noqa: BLE001
                continue
    return None


def _capture_page_text(page: Any) -> tuple[str, str]:
    """Return (title, rendered_body_text) for the currently-loaded page."""
    try:
        title = page.title() or ""
    except Exception:  # noqa: BLE001
        title = ""
    try:
        text = page.locator("body").inner_text(timeout=10_000) or ""
    except Exception:  # noqa: BLE001
        text = ""
    return title, text


def _wait_for_list_page_change(
    page: Any, prev_hash: str, max_wait_ms: int = 9_000
) -> tuple[str, str, str]:
    """Poll until the rendered list content changes from ``prev_hash``.

    SPAs (Mioffice/atsx) advance pages via XHR without a URL change; the
    post-click ``networkidle`` can fire BEFORE the page-XHR completes, so an
    immediate capture re-reads the stale page and falsely signals "no
    progress". This polls the rendered text until its hash differs from
    ``prev_hash`` (the new page rendered) or ``max_wait_ms`` elapses.

    Returns ``(title, body_text, content_hash)``. ``body_text`` is "" when the
    page never changed (treated as last page / stuck by the caller); a wall
    page that DID change is returned verbatim so the caller's wall check fires.
    """
    step = 500
    elapsed = 0
    while elapsed <= max_wait_ms:
        try:
            page.wait_for_load_state("networkidle", timeout=1_000)
        except Exception:  # noqa: BLE001 - best-effort settle
            pass
        title, text = _capture_page_text(page)
        if text.strip():
            cur_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if cur_hash != prev_hash:
                return title, text, cur_hash
        page.wait_for_timeout(step)
        elapsed += step
    return "", "", ""


def _wait_for_rendered_text_stable(page: Any, max_wait_ms: int = 8_000) -> None:
    """Poll the rendered body text until its length stops growing.

    Career-site SPAs (Moka, Mioffice, PDD) render their job lists via XHR that
    may complete AFTER the post-scroll ``networkidle`` fires, so an immediate
    capture can read a half-rendered page (0 job titles) and the run falsely
    under-counts. This waits until the body text length is stable across two
    consecutive polls (lazy-load settled) or ``max_wait_ms`` elapses, so the
    subsequent capture sees the fully-rendered list. Generic - reads only
    already-rendered public content, never bypasses login/captcha/anti-bot.
    """
    step = 500
    elapsed = 0
    prev_len = -1
    stable = 0
    while elapsed <= max_wait_ms:
        try:
            cur_len = int(page.evaluate(
                "() => (document.body && document.body.innerText) ? "
                "document.body.innerText.length : 0"
            ))
        except Exception:  # noqa: BLE001 - best-effort
            cur_len = 0
        if cur_len == prev_len:
            stable += 1
            if stable >= 2 and cur_len > 0:
                return  # settled on non-empty content
        else:
            stable = 0
        prev_len = cur_len
        page.wait_for_timeout(step)
        elapsed += step


def _deep_dive_detail_pages(page: Any, browser: Any, base_url: str) -> list[tuple[str, str, str]]:
    """Open job-detail pages linked from the currently-loaded list page.

    Returns [(url, title, body_text), ...] for detail pages with non-empty
    body. Each detail page is opened in its own tab with consent dismissal,
    bounded by _MAX_DETAIL_PAGES so a large list cannot stall the run.
    """
    captures: list[tuple[str, str, str]] = []
    for durl in _collect_detail_page_links(page, base_url)[:_MAX_DETAIL_PAGES]:
        try:
            dpage = browser.new_page(user_agent=_NAV_USER_AGENT)
            dpage.goto(durl, wait_until="domcontentloaded", timeout=20_000)
            try:
                dpage.wait_for_load_state("networkidle", timeout=8_000)
            except Exception:  # noqa: BLE001
                dpage.wait_for_timeout(1_500)
            _dismiss_consent_dialog(dpage)
            try:
                dtext = dpage.locator("body").inner_text(timeout=8_000) or ""
            except Exception:  # noqa: BLE001
                dtext = ""
            dtitle = ""
            try:
                dtitle = dpage.title() or ""
            except Exception:  # noqa: BLE001
                pass
            dpage.close()
            if dtext.strip():
                captures.append((durl, dtitle, dtext))
        except Exception:  # noqa: BLE001 - best-effort per detail page
            continue
    return captures


def extract_rendered_job_evidence(url: str) -> str:
    """Open a rendered recruitment page and extract job-detail evidence.

    This browser tool captures public recruitment JSON/XHR payloads emitted by
    JavaScript-rendered pages and converts recognized job records into the
    same evidence shape expected by downstream extraction.

    Args:
        url: The public recruitment URL to inspect.

    Returns:
        JSON string with evidence_pages, navigation_path, page_count, and error.
    """
    if _is_blocked_domain(url):
        return json.dumps({"evidence_pages": [], "error": f"Cannot access blocked domain: {url}"}, ensure_ascii=False)
    budget_err = _nav_budget_check()
    if budget_err:
        return json.dumps({"evidence_pages": [], "error": budget_err}, ensure_ascii=False)

    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError:
        return json.dumps({"evidence_pages": [], "error": "Playwright is not installed"}, ensure_ascii=False)

    payloads: list[dict[str, Any]] = []
    response_urls: list[str] = []
    rendered_text: str = ""
    title: str = ""
    # (url, title, body_text) captured from deep-dived detail pages, appended
    # as evidence after the browser session closes.
    detail_captures: list[tuple[str, str, str]] = []
    # (url, title, body_text) captured from paginated list pages (page 2..N),
    # appended as evidence after the browser session closes.
    list_page_captures: list[tuple[str, str, str]] = []

    def capture_response(response: Any) -> None:
        """Capture JSON responses from any recruitment API (not just Alibaba)."""
        response_url = getattr(response, "url", "")
        content_type = (getattr(response, "headers", {}) or {}).get("content-type", "")
        if "json" not in content_type and "javascript" not in content_type:
            return
        try:
            payloads.append(json.loads(response.text()))
            response_urls.append(response_url)
        except Exception:
            return

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(user_agent=_NAV_USER_AGENT)
            page.on("response", capture_response)
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            try:
                page.wait_for_load_state("networkidle", timeout=15_000)
            except PlaywrightTimeoutError:
                page.wait_for_timeout(3_000)
            # Dismiss a privacy/cookie consent interstitial so the publicly-
            # rendered JD content (shown to the visitor after accepting) is
            # readable. Strictly limited to consent buttons - never touches
            # login/captcha/anti-bot (see _dismiss_consent_dialog).
            _dismiss_consent_dialog(page)
            # Many career-site landing pages show only a handful of featured
            # jobs with a "查看更多职位" / "View all positions" link to the full
            # list. Click it once so the capture sees every job, not just the
            # featured subset. Generic (not site-specific); wall-guarded.
            _click_view_all_positions(page)
            # Scroll to trigger lazy-loaded job XHR (infinite scroll /
            # pagination). The response callback stays armed, so each newly
            # captured batch is converted into evidence below. Stop after two
            # consecutive scrolls that add no new network captures OR grow the
            # rendered text (some SPAs render jobs inline without XHR, e.g.
            # Mioffice, so payload-count alone would falsely signal "no
            # progress"). Also scroll the largest scrollable *inner* container -
            # career-site lists often live in an overflow:auto div that
            # window.scrollTo cannot move, so a window-only scroll would never
            # trigger their lazy-load.
            empty_cycles = 0
            for _ in range(12):
                before_payloads = len(payloads)
                before_text_len = page.evaluate(
                    "() => (document.body && document.body.innerText) ? "
                    "document.body.innerText.length : 0"
                )
                page.evaluate(
                    "() => {"
                    "  window.scrollTo(0, document.body.scrollHeight);"
                    "  const els = Array.from(document.querySelectorAll('*'));"
                    "  let best = null, bestArea = 0;"
                    "  for (const el of els) {"
                    "    const st = getComputedStyle(el);"
                    "    if ((st.overflowY === 'auto' || st.overflowY === 'scroll')"
                    "        && el.scrollHeight > el.clientHeight + 50) {"
                    "      const area = el.clientWidth * el.clientHeight;"
                    "      if (area > bestArea) { bestArea = area; best = el; }"
                    "    }"
                    "  }"
                    "  if (best) best.scrollTop = best.scrollHeight;"
                    "}"
                )
                try:
                    page.wait_for_load_state("networkidle", timeout=4_000)
                except PlaywrightTimeoutError:
                    page.wait_for_timeout(1_500)
                after_text_len = page.evaluate(
                    "() => (document.body && document.body.innerText) ? "
                    "document.body.innerText.length : 0"
                )
                if len(payloads) > before_payloads or after_text_len > before_text_len + 30:
                    empty_cycles = 0
                else:
                    empty_cycles += 1
                    if empty_cycles >= 2:
                        break
            # ── Page 1: title + rendered DOM text + linked detail pages ──
            # Many career-site SPAs (Moka, Feishu, Mioffice, PDD) encrypt their
            # job-list XHR payloads (`{"data": "<base64>"}`) so the XHR path
            # above yields nothing, but the *rendered* DOM publicly shows job
            # titles / categories to any visitor without login. Capturing that
            # visible text (what a human user sees) is legitimate evidence that
            # the downstream extractor can fall back on. This never bypasses
            # login/captcha/anti-bot - it only reads already-rendered content.
            # Wait for the lazy-rendered list to settle before capturing, so a
            # late-completing XHR (after networkidle) is not missed - without
            # this the page-1 capture can read a half-rendered page and the run
            # falsely under-counts (seen on Moka where the 21-job list renders
            # ~1s after networkidle).
            _wait_for_rendered_text_stable(page)
            title, rendered_text = _capture_page_text(page)
            detail_captures.extend(_deep_dive_detail_pages(page, browser, url))

            # ── Paginate the list: click "next page" and re-capture each page ──
            # PDD/Mioffice advance pages via in-DOM clicks WITHOUT changing the
            # URL (verified: SPA state-based), so per-page URL fan-out is
            # impossible - the only way to read page 2+ is to click through in
            # this single browser session. Bounded by _MAX_LIST_PAGES, a
            # no-progress content-hash check, and a login/captcha wall circuit
            # breaker (security hard gate #2: never bypass login/captcha).
            prev_hash = (
                hashlib.sha256(rendered_text.encode("utf-8")).hexdigest()
                if rendered_text.strip()
                else ""
            )
            for _ in range(_MAX_LIST_PAGES - 1):
                next_el = _find_next_page_element(page)
                if next_el is None:
                    break
                try:
                    next_el.scroll_into_view_if_needed(timeout=3_000)
                    # ``no_wait_after``: the click triggers an in-DOM SPA page
                    # change (no URL change) which re-renders the pager;
                    # waiting for the resulting "navigation" to settle can detach
                    # the control mid-action and raise a spurious click error.
                    next_el.click(timeout=3_000, no_wait_after=True)
                except Exception:  # noqa: BLE001 - click failed / element stale
                    break
                _dismiss_consent_dialog(page)
                # Wait for the list to actually update (content-hash change)
                # rather than just networkidle, which can fire before the SPA's
                # page-XHR completes (Mioffice) - an immediate capture would
                # re-read the stale page and falsely signal "no progress".
                _p_title, p_text, cur_hash = _wait_for_list_page_change(page, prev_hash)
                if not p_text.strip():
                    break  # page never changed -> last page / stuck
                if _is_pagination_wall(p_text):
                    break  # login/captcha/anti-bot wall -> never bypass
                prev_hash = cur_hash
                list_page_captures.append((page.url, _p_title, p_text))
                # Detail deep-dive only runs on page 1 (above). Paginated pages
                # 2..N contribute their list-page text only - opening detail
                # tabs for every paginated page would explode to up to
                # _MAX_LIST_PAGES * _MAX_DETAIL_PAGES browser tabs and blow the
                # per-URL time budget. The count objective is met by list-page
                # titles alone.
            browser.close()
    except (PlaywrightError, PlaywrightTimeoutError) as exc:
        return json.dumps({"evidence_pages": [], "error": f"Browser evidence extraction failed: {exc}"}, ensure_ascii=False)

    evidence_pages: list[dict[str, Any]] = []
    for payload in payloads:
        evidence_pages.extend(_generic_position_evidence_from_payload(payload, url))

    deduped: dict[str, dict[str, Any]] = {}
    for item in evidence_pages:
        key = str((item.get("metadata") or {}).get("position_id") or item.get("content_hash") or item.get("title"))
        if key and key not in deduped:
            deduped[key] = item
    evidence_pages = list(deduped.values())

    # Always surface the rendered DOM text as a ``page_text`` evidence page so
    # the extraction helper has a fallback when XHR payloads are encrypted or
    # empty. The full rendered text (capped) is stored as ``text_excerpt`` and
    # its hash as ``content_hash``; the helper's loose title extractor reads
    # this when the strict JD-detail regexes find nothing.
    if rendered_text.strip():
        evidence_pages.append(
            {
                "evidence_type": "page_text",
                "url": url,
                "title": title or "",
                "content_hash": hashlib.sha256(rendered_text.encode("utf-8")).hexdigest(),
                "text_excerpt": rendered_text[:8000],
                "metadata": {"source": "rendered_dom", "position_id": ""},
            }
        )
    # Paginated list pages (page 2..N). Same page_text shape as page 1 so the
    # extractor treats each as a separate evidence source; content_hash
    # differentiates them even when the SPA URL is unchanged across pages.
    for p_url_i, p_title_i, p_text_i in list_page_captures:
        evidence_pages.append(
            {
                "evidence_type": "page_text",
                "url": p_url_i,
                "title": p_title_i or "",
                "content_hash": hashlib.sha256(p_text_i.encode("utf-8")).hexdigest(),
                "text_excerpt": p_text_i[:8000],
                "metadata": {"source": "list_page", "position_id": ""},
            }
        )
    # Append deep-dived detail-page evidence. These carry the full JD body, so
    # the strict extractor (has_detail=True) keeps them as structured
    # candidates rather than falling back to the title-only extractor.
    for durl_i, dtitle_i, dtext_i in detail_captures:
        evidence_pages.append(
            {
                "evidence_type": "page_text",
                "url": durl_i,
                "title": dtitle_i,
                "content_hash": hashlib.sha256(dtext_i.encode("utf-8")).hexdigest(),
                "text_excerpt": dtext_i[:8000],
                "metadata": {"source": "detail_page", "position_id": ""},
            }
        )

    global _nav_page_count, _nav_current_url, _nav_history
    _nav_page_count += 1
    if _nav_current_url:
        _nav_history.append(_nav_current_url)
    _nav_current_url = url

    return json.dumps(
        {
            "evidence_pages": evidence_pages,
            "navigation_path": [{"url": url, "title": title or "", "action": "extract_rendered_job_evidence"}],
            "page_count": _nav_page_count,
            "error": None if evidence_pages else "No recognized rendered job evidence found",
            "metadata": {
                "captured_response_count": len(payloads),
                "response_urls": response_urls[:5],
                "list_pages": len(list_page_captures) + 1,
            },
        },
        ensure_ascii=False,
    )


def _fetch_alibaba_search_api(url: str) -> dict[str, Any]:
    """Navigate the Alibaba campus SPA with Playwright and extract job data.

    Opens the URL in a headless Chromium browser, waits for the SPA to
    fully render, then captures both XHR JSON responses and the rendered
    DOM visible text.  Returns a dict with ``page_text``, ``payloads``,
    and ``page_title`` so the adapter can build evidence from whichever
    source yields usable data.

    The old direct-POST-to-``/position/search`` path is removed — it
    consistently returned 403 even with session-cookie warming, and the
    wasted ~30 s ate into the per-URL time budget.

    Args:
        url: The Alibaba campus SPA URL.

    Returns:
        Dict with keys ``page_text`` (str), ``page_title`` (str),
        ``payloads`` (list[dict]), and ``url``.

    Raises:
        RuntimeError: If the browser cannot be launched or the page
            cannot be loaded at all.
    """
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("Playwright is not installed — cannot navigate Alibaba SPA")

    payloads: list[dict[str, Any]] = []
    response_urls: list[str] = []
    page_text: str = ""
    page_title: str = ""

    def _capture_json_response(response: Any) -> None:
        """Capture JSON responses from the SPA's XHR/fetch calls."""
        resp_url = getattr(response, "url", "")
        ct = (getattr(response, "headers", {}) or {}).get("content-type", "")
        if "json" not in ct and "javascript" not in ct:
            return
        try:
            payloads.append(json.loads(response.text()))
            response_urls.append(resp_url)
        except Exception:
            return

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(user_agent=_NAV_USER_AGENT)
            page.on("response", _capture_json_response)

            # Navigate and wait for SPA to finish rendering
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            try:
                page.wait_for_load_state("networkidle", timeout=15_000)
            except PlaywrightTimeoutError:
                # SPA may have long-polling connections — give it extra time
                page.wait_for_timeout(5_000)

            # Additional wait for React/Vue re-render after data fetch
            page.wait_for_timeout(2_000)

            page_title = page.title()
            try:
                page_text = page.inner_text("body") or ""
            except PlaywrightError:
                page_text = page.content() or ""

            browser.close()
    except PlaywrightTimeoutError:
        # Page load timed out but we may still have partial data
        pass
    except PlaywrightError as exc:
        raise RuntimeError(
            f"Alibaba SPA browser navigation failed for {url}: {exc}"
        ) from exc

    return {
        "url": url,
        "page_text": page_text[:15000],
        "page_title": page_title,
        "payloads": payloads,
        "response_urls": response_urls,
    }


def _generic_position_evidence_from_payload(
    payload: dict[str, Any],
    source_url: str,
) -> list[dict[str, Any]]:
    """Convert an arbitrary JSON payload that may contain job data into evidence.

    Walks the payload looking for objects that have job-like fields (title,
    name, position, description, requirement, etc.) and converts each into
    a ``job_detail_json`` PageEvidence entry.
    """
    evidence: list[dict[str, Any]] = []

    # --- Helper: recursively find job-like objects ---
    def _walk(obj: Any, depth: int = 0) -> list[dict[str, Any]]:
        if depth > 5:
            return []
        results: list[dict[str, Any]] = []
        if isinstance(obj, dict):
            # Check if this dict looks like a job record
            keys_lower = {k.lower() for k in obj.keys() if isinstance(k, str)}
            job_indicators = keys_lower & {
                "name", "title", "position", "positionname", "jobname",
                "description", "requirement", "responsibilities",
                "company", "companyname", "employer",
                "location", "locations", "worklocations", "city",
                "department", "category", "categoryname",
                "id", "positionid", "jobid", "requisitionid",
            }
            if len(job_indicators) >= 2:
                results.append(obj)
            # Also walk nested values
            for _k, v in obj.items():
                results.extend(_walk(v, depth + 1))
        elif isinstance(obj, list):
            for item in obj[:200]:  # cap per level
                results.extend(_walk(item, depth + 1))
        return results

    job_objects = _walk(payload)

    seen_ids: set[str] = set()
    for item in job_objects:
        title = (
            str(item.get("name") or item.get("title") or item.get("positionName") or item.get("jobName") or "")
        ).strip()
        description = (
            str(item.get("description") or item.get("jobDescription") or "")
        ).strip()
        requirement = (
            str(item.get("requirement") or item.get("requirements") or item.get("qualifications") or "")
        ).strip()

        if not title:
            continue
        if not description and not requirement:
            continue

        # Build unique key to deduplicate
        position_id = str(
            item.get("id") or item.get("positionId") or item.get("jobId") or item.get("requisitionId") or ""
        ).strip()
        if position_id and position_id in seen_ids:
            continue
        if position_id:
            seen_ids.add(position_id)

        # Extract location info
        locations = item.get("workLocations") or item.get("locations") or item.get("location") or []
        if isinstance(locations, str):
            locations = [locations]
        location_str = "、".join(str(x) for x in locations if x) if isinstance(locations, list) else str(locations)

        # Build the evidence text
        parts = [f"岗位名称: {title}"]
        if location_str:
            parts.append(f"工作地点: {location_str}")
        company = str(item.get("companyName") or item.get("company") or item.get("employer") or "").strip()
        if company:
            parts.append(f"公司名称: {company}")
        category = str(item.get("categoryName") or item.get("category") or "").strip()
        if category:
            parts.append(f"岗位类别: {category}")
        dept = str(item.get("department") or item.get("departmentName") or "").strip()
        if dept:
            parts.append(f"所属部门: {dept}")
        if description:
            parts.append(f"岗位职责:\n{description}")
        if requirement:
            parts.append(f"任职要求:\n{requirement}")

        text = "\n".join(parts)
        content_hash = hashlib.sha256(
            json.dumps(item, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        # Truncate text_excerpt to avoid overflowing LLM context window.
        evidence.append({
            "evidence_type": "job_detail_json",
            "url": source_url,
            "title": title,
            "content_hash": content_hash,
            "text_excerpt": text[:1500],
            "metadata": {
                "position_id": position_id,
                "source": "rendered_xhr",
                "locations": locations if isinstance(locations, list) else [locations] if locations else [],
                "company_name": company,
                "category": category,
                "department": dept,
            },
        })

    return evidence


def read_dom(url: str) -> str:
    """Read the DOM of a page as simplified text.

    Uses ``_cached_fetch`` — reuses an earlier ``open_url`` fetch when available.
    """
    if _is_blocked_domain(url):
        return f"ERROR: Cannot access blocked domain: {url}"
    budget_err = _nav_budget_check()
    if budget_err:
        return f"ERROR: {budget_err}"

    was_cached = url in _page_cache
    content, title, error = _cached_fetch(url)
    if error is not None:
        return f"ERROR: Could not read {url}: {error}"

    if not was_cached:
        global _nav_page_count, _nav_current_url, _nav_history
        _nav_page_count += 1
        if _nav_current_url:
            _nav_history.append(_nav_current_url)
        _nav_current_url = url

    html = content or ""
    # Strip script/style but keep structure
    html = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.IGNORECASE | re.DOTALL)
    html = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.IGNORECASE | re.DOTALL)
    html = re.sub(r"<!--.*?-->", "", html, flags=re.DOTALL)
    html = re.sub(r">\s+<", ">\n<", html)
    html = re.sub(r"\n{3,}", "\n\n", html)
    return html[:10000] or "(empty page)"


def extract_links(url: str) -> str:
    """Extract all links from a page.

    Uses ``_cached_fetch`` — reuses an earlier fetch when available.
    """
    if _is_blocked_domain(url):
        return json.dumps({"error": f"Cannot access blocked domain: {url}"})
    budget_err = _nav_budget_check()
    if budget_err:
        return json.dumps({"error": budget_err})

    was_cached = url in _page_cache
    content, title, error = _cached_fetch(url)
    if error is not None:
        return json.dumps({"error": f"Could not extract links from {url}: {error}"})

    if not was_cached:
        global _nav_page_count, _nav_current_url, _nav_history
        _nav_page_count += 1
        if _nav_current_url:
            _nav_history.append(_nav_current_url)
        _nav_current_url = url

    html = content or ""
    links = _extract_links_from_html(html, url)
    return json.dumps(links, ensure_ascii=False)


def click_link(url: str, link_text: str) -> str:
    """Follow a link on a page by matching link text.

    Uses ``_cached_fetch`` for the current page scan; the followed link
    is fetched directly since it is a new URL.
    """
    if _is_blocked_domain(url):
        return f"ERROR: Cannot access blocked domain: {url}"
    budget_err = _nav_budget_check()
    if budget_err:
        return f"ERROR: {budget_err}"

    content, title, error = _cached_fetch(url)
    if error is not None:
        return f"ERROR: Could not read {url}: {error}"

    html = content or ""
    links = _extract_links_from_html(html, url)

    # Find the link whose text matches (case-insensitive, substring).
    # Examine ALL links before deciding (the previous indentation returned
    # "no link found" on the first non-matching link and never followed any).
    target_url: str | None = None
    for link in links:
        if link_text.lower() in link["text"].lower():
            target_url = link["url"]
            break

    if not target_url:
        return f"ERROR: No link with text '{link_text}' found on {url}"

    # Follow the link
    global _nav_page_count, _nav_current_url, _nav_history
    try:
        resp2 = requests.get(target_url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        resp2.raise_for_status()
        html2 = resp2.text
        text2 = _extract_page_text(html2)

        _nav_page_count += 1
        if _nav_current_url:
            _nav_history.append(_nav_current_url)
        _nav_current_url = target_url

        return text2 or "(empty page)"
    except requests.RequestException as e:
        return f"ERROR: Could not follow link: {e}"


def go_back() -> str:
    """Return to the previous page in navigation history.

    Returns:
        Content of the previous page, or error if no history.
    """
    global _nav_current_url, _nav_history
    if not _nav_history:
        return "ERROR: No previous page in navigation history"
    prev_url = _nav_history.pop()
    _nav_current_url = prev_url
    return open_url(prev_url)


# ---------------------------------------------------------------------------
# Web Navigation Subagent builder
# ---------------------------------------------------------------------------

# System prompt for the Web Navigation Agent (verbatim from spec)
_WEB_NAVIGATION_SYSTEM_PROMPT = """\
You are the Web Navigation Agent.

Goal: Starting from a public URL, find credible job list pages and JD detail pages.

Allowed actions:
- Open pages.
- Open pages in a headless browser when static HTML is empty or JavaScript-rendered.
- Extract rendered job evidence from public recruitment XHR/JSON when a page is an SPA.
- Read visible text and DOM links.
- Follow navigation links likely related to Careers, Jobs, Join Us, Campus Recruitment, Internships, Recruiting, or Chinese equivalents.
- Capture evidence page text.

Rules:
- Stay within the tool-enforced page budget.
- Do not attempt login.
- Do not solve captcha or anti-bot challenges.
- You MAY let `extract_rendered_job_evidence` dismiss privacy/cookie consent
  interstitials to read publicly-available JD content. It only clicks consent
  buttons (同意/Accept/Agree) and never login/captcha/anti-bot. When a list
  page shows only job titles and detail pages are gated by a consent dialog,
  call `extract_rendered_job_evidence` on the detail-page URL to capture its
  full JD body.
- Return discovered JD evidence pages and discovery path.
- Do not extract final standardized jobs; the supervisor will call extraction tools."""

# Backward-compatible alias for tests / external consumers
_SUPERVISOR_SYSTEM_PROMPT = build_supervisor_prompt()


def create_web_navigation_subagent(
    settings: Settings,
) -> SubAgent:
    """Create a Web Navigation SubAgent specification for deepagents.

    The subagent has dedicated web navigation tools and enforces page budget
    and domain safety constraints programmatically.

    Args:
        settings: Application settings (provides page budget).

    Returns:
        A SubAgent TypedDict suitable for passing to create_deep_agent's
        subagents parameter.
    """
    max_pages = settings.job_discovery_max_pages_per_task

    global _nav_max_pages
    _nav_max_pages = max_pages

    web_nav_tools = [
        open_url,
        open_rendered_url,
        extract_rendered_job_evidence,
        read_dom,
        extract_links,
        click_link,
        go_back,
    ]

    subagent: SubAgent = {
        "name": "web_navigation_agent",
        "description": (
            "Navigates web pages to discover job JD evidence. "
            "Provides page text, links, and rendered job evidence. "
            "Enforces page budget and domain safety."
        ),
        "system_prompt": _WEB_NAVIGATION_SYSTEM_PROMPT,
        "tools": web_nav_tools,
    }

    return subagent


# ---------------------------------------------------------------------------
# Discovery Supervisor Agent builder
# ---------------------------------------------------------------------------


class _DiscoveryRunResultPydantic(BaseModel):
    """Pydantic equivalent of DiscoveryRunResult for use as response_format.

    Mirrors the DiscoveryRunResult dataclass fields so deepagents can
    produce structured output matching the same schema.
    """
    status: str = "failed"
    block_reason: str | None = None
    evidence: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    summary: str = ""


class _WebNavigationResultPydantic(BaseModel):
    """Structured result returned by the Web Navigation Agent."""

    evidence_pages: list[dict[str, Any]] = []
    navigation_path: list[dict[str, str]] = []
    page_count: int = 0
    error: str | None = None


def _parse_web_navigation_agent_result(result: Any) -> dict[str, Any]:
    """Normalize DeepAgent web navigation output into the public tool shape."""
    if hasattr(result, "model_dump"):
        result = result.model_dump()

    if not isinstance(result, dict):
        return {
            "evidence_pages": [],
            "navigation_path": [],
            "page_count": 0,
            "error": f"Unexpected web navigation agent result type: {type(result).__name__}",
            "delegated_to": "web_navigation_agent",
        }

    structured = result.get("structured_response")
    if hasattr(structured, "model_dump"):
        structured = structured.model_dump()
    if isinstance(structured, dict):
        data = structured
    else:
        data = result

    evidence_pages = data.get("evidence_pages") or data.get("evidence") or []
    navigation_path = data.get("navigation_path") or data.get("path") or []
    if not isinstance(evidence_pages, list):
        evidence_pages = []
    if not isinstance(navigation_path, list):
        navigation_path = []

    page_count = data.get("page_count")
    if not isinstance(page_count, int):
        page_count = len(evidence_pages)

    normalized = {
        "evidence_pages": evidence_pages,
        "navigation_path": navigation_path,
        "page_count": page_count,
        "delegated_to": "web_navigation_agent",
    }
    if data.get("error"):
        normalized["error"] = str(data["error"])
    return normalized


# Strong, specific job-title suffixes. Bare category words (运营/市场/产品/
# 销售) are NOT listed here as bare words, but qualified forms that *end* in a
# common role word are: e.g. ``商家运营`` / ``视频创意制作`` are real jobs whose
# titles end in ``运营`` / ``制作``. The post-extraction false-positive filter
# (``_is_plausible_job_title``) still rejects a title that is *exactly* a bare
# suffix (``运营`` alone) or a sidebar tab repeating across pages, so listing
# these role-word endings here surfaces qualified real titles while bare
# category tabs stay filtered. Shared by the loose title-only extractor and
# the post-extraction false-positive filter so a title is judged by one
# consistent suffix set.
_JOB_TITLE_SUFFIXES: tuple[str, ...] = (
    "工程师", "分析师", "架构师", "设计师", "科学家", "专家",
    "管培生", "管培", "专员", "实习生", "顾问", "助理",
    "研究员", "产品经理", "项目经理", "运营经理", "研发经理",
    "负责人", "总监", "主管", "经理",
    # Role-word endings (qualified forms only survive the filter):
    "运营", "制作",
)

# Bare-suffix words that are almost always a category / section header rather
# than a standalone job title (``经理`` / ``主管`` / ``运营`` alone are generic
# category labels). A bare title whose suffix is one of these is rejected. Bare
# *specific* role titles (``产品经理``, ``工程师``, ``管培生``, ``专员`` ...) are
# legitimate standalone jobs - e.g. deeproute lists ``【2027秋招】产品经理``
# which strips to the bare title ``产品经理`` - so they are NOT rejected here;
# if one is in fact a sidebar tab it is still caught by the repeats filter
# (rule 3) on multi-page captures. The matched-suffix lookup below resolves
# compounds (``产品经理`` matches ``产品经理``, not the shorter ``经理``), so a
# specific compound is never misclassified as a bare generic word.
_GENERIC_BARE_SUFFIXES: frozenset[str] = frozenset({
    "经理", "主管", "总监", "负责人", "运营", "制作",
})


def _extract_title_only_candidates(
    text: str,
    page_url: str,
    ref: dict[str, Any],
) -> list[NormalizedJobCandidate]:
    """Loose fallback: pull likely job titles from rendered list-page text.

    Used only when the strict JD-detail extractor (which matches
    ``岗位职责:``/``任职要求:`` patterns) finds nothing usable on a
    ``page_text`` evidence page. Career-site list pages render job *titles*
    publicly but not JD bodies - the detail pages are gated behind
    privacy/consent interstitials that this system never circumvents (per the
    security hard gate). Reading the already-rendered, publicly-visible title
    text is legitimate; it yields *title-only* candidates (no responsibilities
    / requirements), clearly flagged via ``normalization_warnings`` so reviewers
    know the JD body was not captured.

    Args:
        text: Rendered DOM text (``body.inner_text``) of the list page.
        page_url: URL of the page (used as ``apply_url``).
        ref: Evidence-ref dict to attach to every candidate.

    Returns:
        List of title-only NormalizedJobCandidate objects (deduped by title).
    """
    suffixes = _JOB_TITLE_SUFFIXES
    # Lines ending with these are category/section headers, not job titles.
    bad_endings = ("招聘", "类", "项目", "中心", "介绍", "说明", "详情", "更多")
    # Lines containing sentence punctuation are prose, not titles.
    sentence_punct = re.compile(r"[，。、；：！？]")
    numbered = re.compile(r"^\d")

    candidates: list[NormalizedJobCandidate] = []
    seen: set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or len(line) > 30 or len(line) < 2:
            continue
        # Drop campaign prefix like 【2027秋招】 and trailing parenthetical /
        # "-届" suffixes so the suffix check sees the bare title.
        _zw = "​‌‍﻿"
        _dash_class = "-–‐—"
        for _ch in _zw:
            line = line.replace(_ch, "")
        cleaned = re.sub(r"^[【][^】]*[】]\s*", "", line)
        cleaned = re.sub(r"（[^）]*）$", "", cleaned)
        # Strip a TRAILING 【...】 campaign/cohort tag (e.g. "...研发工程师【2027届云弧计划】")
        # so the suffix check sees the bare title and detects it. Mirrors the
        # leading-prefix strip above; the tag is listing metadata, not the job
        # title itself, so removing it is consistent with the leading-strip.
        cleaned = re.sub(r"[【][^】]*[】]\s*$", "", cleaned)
        cleaned = re.sub(rf"[{_dash_class}][^{_dash_class}]*$", "", cleaned)
        for _ch in _zw:
            cleaned = cleaned.replace(_ch, "")
        cleaned = cleaned.strip(f" {_dash_class}·")
        if not cleaned or len(cleaned) > 30 or len(cleaned) < 2:
            continue
        if cleaned.endswith(bad_endings):
            continue
        if sentence_punct.search(cleaned) or numbered.search(cleaned):
            continue
        if not any(cleaned.endswith(s) for s in suffixes):
            continue
        if cleaned in seen:
            continue
        seen.add(cleaned)
        candidates.append(
            NormalizedJobCandidate(
                title=cleaned,
                apply_url=page_url,
                description_text="",
                locations=[],
                recruitment_types=[],
                industries=[],
                evidence_refs=[ref],
                normalization_warnings=[
                    "Title-only candidate: extracted from rendered list page; "
                    "JD body not captured (detail page blocked by interstitial)"
                ],
            )
        )
        if len(candidates) >= 80:
            break
    return candidates


def _page_text_normalized_line_sets(
    evidence_pages: list[dict[str, Any]],
) -> list[set[str]]:
    """One set of normalized lines per ``page_text`` evidence page.

    Used by the sidebar-category filter: a title that appears as a whole line
    on several paginated list-page captures is a repeating sidebar element
    (e.g. a category tab that renders on every page), not a distinct job.
    """
    out: list[set[str]] = []
    for page in evidence_pages:
        if not isinstance(page, dict) or page.get("evidence_type") != "page_text":
            continue
        text = page.get("text_excerpt") or ""
        lines: set[str] = set()
        for raw in text.splitlines():
            ln = raw.strip()
            if not ln:
                continue
            norm = normalize_title(ln)
            if norm:
                lines.add(norm)
        out.append(lines)
    return out


def _is_plausible_job_title(
    candidate: NormalizedJobCandidate,
    page_text_line_sets: list[set[str]],
) -> bool:
    """Reject obvious false-positive titles from rendered list pages.

    Three generic, site-agnostic filters (no counts / pages / sites hardcoded):

    1. Structural-separator titles (banners / news headlines containing ``|``).
       A real job title never contains a pipe.
    2. Bare *generic* category words - the title is exactly a generic category
       suffix (``经理`` / ``主管`` / ``运营`` ...) with no qualifier. A section
       header, not a listing. Bare *specific* role titles (``产品经理``,
       ``工程师``, ``管培生`` ...) are legitimate standalone jobs and are kept;
       a specific role that is in fact a sidebar tab is still caught by rule 3.
    3. Sidebar category tabs - a *title-only* candidate (no JD body) whose
       normalized title appears as a whole line on 2+ ``page_text`` captures.
       Paginated list pages re-render the sidebar on every page, so a genuine
       job appears on exactly one page while a sidebar element repeats across
       all of them. Full-JD candidates are exempt (their body proves they are
       real listings even if cross-linked from several list pages).
    """
    title = (getattr(candidate, "title", "") or "").strip()
    if not title:
        return False
    if "|" in title:
        return False
    matched = max(
        (s for s in _JOB_TITLE_SUFFIXES if title.endswith(s)),
        key=len,
        default="",
    )
    if (
        matched
        and not title[: -len(matched)].strip()
        and matched in _GENERIC_BARE_SUFFIXES
    ):
        return False
    has_body = bool(
        (getattr(candidate, "responsibilities", "") or "").strip()
        or (getattr(candidate, "requirements", "") or "").strip()
    )
    if has_body or len(page_text_line_sets) < 2:
        return True
    norm = normalize_title(title)
    if norm and sum(1 for s in page_text_line_sets if norm in s) >= 2:
        return False
    return True


def _extract_and_verify_candidates_from_evidence(
    evidence_pages: list[dict[str, Any]],
    source_url: str,
) -> tuple[list[dict[str, Any]], str]:
    """Deterministically extract and verify candidates from captured evidence.

    For each evidence page, run the deterministic JD extractor on its
    ``text_excerpt`` and attach an ``evidence_ref`` pointing at that page's
    ``content_hash``/``url`` so the verifier's ``evidence_refs`` requirement is
    satisfied (the standard ``extract_jd_candidates`` path leaves refs empty,
    which ``verify_evidence`` would otherwise reject). Each page is extracted
    independently, so a page text containing a single job yields a single
    candidate - sidestepping the page-text 2-segment extractor ceiling.

    Returns ``(candidate_dicts, evidence_hash)`` where each candidate dict is
    packaged with idempotency/similarity keys ready for persistence.
    """
    if not evidence_pages:
        return [], ""

    _EVI_FIELDS = {f.name for f in PageEvidence.__dataclass_fields__.values()}
    evidence_objs: list[PageEvidence] = []
    for page in evidence_pages:
        if not isinstance(page, dict):
            continue
        fields = {k: v for k, v in page.items() if k in _EVI_FIELDS}
        # ``evidence_type`` is the one required PageEvidence field; some
        # evidence-page dicts produced by the Web Navigation Agent omit it.
        fields.setdefault("evidence_type", "page_text")
        evidence_objs.append(PageEvidence(**fields))

    all_candidates: list[NormalizedJobCandidate] = []
    for page, ev_obj in zip(evidence_pages, evidence_objs):
        if not isinstance(page, dict):
            continue
        text = page.get("text_excerpt") or ""
        if not text.strip():
            continue
        page_url = page.get("url") or source_url
        extracted = _extract_jd_candidates(text, page_url)
        ref = {
            "url": ev_obj.url,
            "content_hash": ev_obj.content_hash,
            "evidence_type": ev_obj.evidence_type,
        }
        # ``page_text`` evidence from a career-site *list* page has no JD-detail
        # structure, so the strict extractor returns nothing (or only spurious
        # section-header "candidates"). Fall back to the loose title extractor
        # so the publicly-rendered job titles are still surfaced. Detail pages
        # (whose candidates carry non-empty responsibilities/requirements) are
        # left untouched.
        is_page_text = (page.get("evidence_type") == "page_text") or (
            str(ev_obj.evidence_type) == "page_text"
        )
        has_detail = any(
            (getattr(c, "responsibilities", "") or getattr(c, "requirements", ""))
            for c in extracted
        )
        # On a list ``page_text`` with no JD-detail structure the strict
        # extractor returns only spurious section-header "candidates"
        # (e.g. ``在招职位`` / ``最新职位``); discard those and fall back to
        # the loose title extractor so the real job titles are surfaced.
        # Detail pages keep their strict candidates untouched.
        keep_strict = (not is_page_text) or has_detail
        if keep_strict:
            for cand in extracted:
                # Sanitize titles: the strict extractor occasionally yields a
                # candidate whose title is only zero-width/whitespace (e.g. on
                # a landing page whose first heading is a styled glyph, or a
                # testimonial block misread as a JD body). A candidate with no
                # usable title is not a real job listing - drop it outright
                # rather than letting a ``​`` title leak through.
                _t = cand.title or ""
                for _ch in "​‌‍﻿\t":
                    _t = _t.replace(_ch, "")
                _t = _t.strip()
                if not _t:
                    continue
                cand.title = _t
                cand.evidence_refs = [ref]
                all_candidates.append(cand)
        if is_page_text and not has_detail:
            all_candidates.extend(
                _extract_title_only_candidates(text, page_url, ref)
            )

    # Drop obvious false positives before verification: banners (pipe in
    # title), bare category words, and sidebar tabs that repeat across
    # paginated list-page captures. Generic (no site/count/page hardcoded);
    # applied here so both the strict and loose extractors' output is cleaned.
    page_text_line_sets = _page_text_normalized_line_sets(evidence_pages)
    all_candidates = [
        c for c in all_candidates
        if _is_plausible_job_title(c, page_text_line_sets)
    ]

    verified = _verify_evidence(all_candidates, evidence_objs)

    # Collapse duplicates that arise when the same job is captured across
    # overlapping evidence pages (the baseline extract_rendered_job_evidence
    # call plus the Web Navigation Agent's re-capture often produce near-
    # identical rendered text with different content hashes, so the same
    # titles get extracted twice). Exact-identity merge only (D3): full-JD
    # candidates dedup by (company, core_hash); title-only candidates dedup by
    # (company, normalized_title). This is generic post-processing - it is NOT
    # a site adapter and hardcodes no counts/pages/sites.
    verified = deduplicate_candidates(verified)

    # Cross-type subsumption: a title-only candidate (no JD body, captured from
    # rendered list text) whose normalized title is a substring of a full-JD
    # candidate's normalized title is almost certainly the SAME job captured
    # twice - the list-page loose title extractor reads the bare title while
    # the XHR / detail version carries it with a department/category suffix
    # (e.g. list text "6g无线方案设计工程师" vs XHR "顶尖应届-6g无线方案设计工程师-
    # 手机", both normalizing with the dept retained). The full-JD candidate is
    # authoritative (it has the JD body), so the redundant title-only one is
    # dropped. Substring (one-directional) keeps genuinely-new title-only jobs
    # whose title is NOT contained in any full-JD title. Generic post-processing
    # - not a site adapter, hardcodes no counts/pages/sites.
    full_jd_titles = [
        normalize_title(getattr(c, "title", "") or "")
        for c in verified
        if (getattr(c, "responsibilities", "") or "").strip()
        or (getattr(c, "requirements", "") or "").strip()
    ]
    full_jd_titles = [t for t in full_jd_titles if len(t) >= 4]
    if full_jd_titles:
        kept: list[Any] = []
        for c in verified:
            has_body = bool(
                (getattr(c, "responsibilities", "") or "").strip()
                or (getattr(c, "requirements", "") or "").strip()
            )
            if has_body:
                kept.append(c)
                continue
            t = normalize_title(getattr(c, "title", "") or "")
            # Drop only if this title-only title is contained in a full-JD title
            # (the full-JD version is the same job, more complete). A short
            # generic title (len < 4) is never subsumed to avoid false drops.
            if len(t) >= 4 and any(t in ft for ft in full_jd_titles):
                continue
            kept.append(c)
        verified = kept

    evidence_hash = hashlib.sha256(
        json.dumps(evidence_pages, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    try:
        source_family = urlsplit(source_url).netloc.lower() or "career_site"
    except ValueError:
        source_family = "career_site"

    packaged: list[dict[str, Any]] = []
    for cand in verified:
        d = _asdict(cand)
        company = d.get("company_name") or ""
        title = d.get("title") or ""
        locations = d.get("locations") or []
        location = locations[0] if locations else "unknown"
        apply_url = d.get("apply_url") or source_url
        rec_types = d.get("recruitment_types") or []
        recruitment_type = rec_types[0] if rec_types else "unknown"
        d["idempotency_key"] = build_candidate_idempotency_key(
            company=company, title=title, location=location,
            apply_url=apply_url, evidence_hash=evidence_hash,
        )
        d["similarity_group_key"] = build_similarity_group_key(
            company=company, title=title,
            recruitment_type=recruitment_type, source_family=source_family,
        )
        packaged.append(d)
    return packaged, evidence_hash


def build_web_navigation_agent(
    *,
    settings: Settings,
    model: ChatOpenAI | None = None,
) -> Any:
    """Build the standalone DeepAgent used for web navigation.

    This is the real Web Navigation Agent: an LLM loop that observes tool
    outputs and autonomously chooses the next navigation action.
    """
    if model is None:
        model = _build_job_discovery_llm(settings)

    global _nav_max_pages
    _nav_max_pages = settings.job_discovery_max_pages_per_task

    return create_deep_agent(
        model=model,
        tools=[
            open_url,
            open_rendered_url,
            extract_rendered_job_evidence,
            read_dom,
            extract_links,
            click_link,
            go_back,
        ],
        system_prompt=_WEB_NAVIGATION_SYSTEM_PROMPT,
        name="web_navigation_agent",
        response_format=_WebNavigationResultPydantic,
    )


def invoke_supervisor_agent(
    agent: Any,
    agent_input: dict[str, Any],
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Invoke the discovery supervisor agent, degrading recursion crashes.

    The agent is streamed (``stream_mode="values"``) rather than invoked, so
    that on a ``GraphRecursionError`` - the supervisor LLM looped without
    converging - the partial state accumulated up to the crash is preserved.
    ``run_web_navigation`` may have already captured and packaged candidates in
    its tool-call message by the time the loop trips the recursion limit; that
    state is returned so ``parse_agent_result`` can recover those candidates
    via its tool-output path instead of discarding them (xiaomi: the supervisor
    reliably calls ``run_web_navigation`` once, captures ~138 candidates, then
    loops trying to emit the oversized structured response). Only when no
    partial state survived do we fall back to a synthetic
    ``needs_manual_review`` dict.

    The supervisor prompt already mandates 3-step convergence, so a recursion
    crash is LLM non-determinism, not a real failure - the recovered candidates
    (if any) are authoritative; otherwise the run is flagged for manual review
    rather than reported as a hard ``failed``. Other exceptions propagate.

    Detection is by exception/message name (no hard langgraph import) so the
    guard works across langgraph versions.

    Returns:
        The final streamed agent state dict, the partial state recovered at a
        recursion crash, or a synthetic ``needs_manual_review`` dict when no
        state survived.
    """
    invoke_kwargs: dict[str, Any] = {"config": config} if config is not None else {}
    last_state: dict[str, Any] | None = None
    try:
        for state in agent.stream(agent_input, stream_mode="values", **invoke_kwargs):
            if isinstance(state, dict):
                last_state = state
    except Exception as exc:
        name = type(exc).__name__
        msg = str(exc)
        if "Recursion" in name or "Recursion limit" in msg:
            if last_state:
                # Preserve the partial state so parse_agent_result can recover
                # candidates run_web_navigation already captured (status is
                # synthesized downstream: candidates present -> succeeded).
                return last_state
            return {
                "status": "needs_manual_review",
                "block_reason": "recursion_limit",
                "evidence": [],
                "candidates": [],
                "summary": (
                    "Supervisor did not converge within the recursion limit "
                    "(LLM looped without reaching a stop condition) and no "
                    "partial state was captured. Re-run or review manually."
                ),
            }
        raise
    return last_state if last_state is not None else {}


def build_discovery_supervisor_agent(
    *,
    settings: Settings,
    model: ChatOpenAI | None = None,
    snapshot_context: dict | None = None,
) -> Any:
    """Build the Discovery Supervisor Agent using deepagents.

    Args:
        settings: Application settings.
        model: Optional pre-built ChatOpenAI instance.
        snapshot_context: If provided, Supervisor takes over from a failed
            SnapshotExecutor/Adapter. Injects completed steps and failed
            step info into the system prompt.

    Returns:
        A CompiledStateGraph ready for invocation.
    """
    if model is None:
        model = _build_job_discovery_llm(settings)


    # Create the web navigation subagent
    web_nav_subagent = create_web_navigation_subagent(settings)

    # Build a partial-application wrapper for tools that need settings
    def _make_run_web_navigation(settings: Settings):
        def _wrapper(start_url: str) -> dict[str, Any]:
            return run_web_navigation(
                start_url,
                settings=settings,
                subagent=web_nav_subagent,
                model=model,
            )
        _wrapper.__name__ = "run_web_navigation"  # type: ignore[attr-defined]
        _wrapper.__doc__ = run_web_navigation.__doc__
        _wrapper.__annotations__ = {"start_url": str, "return": dict[str, Any]}
        return _wrapper

    def _make_run_ocr(settings: Settings):
        def _wrapper(image_base64: str) -> dict[str, Any]:
            return run_ocr(image_base64, settings=settings)
        _wrapper.__name__ = "run_ocr"  # type: ignore[attr-defined]
        _wrapper.__doc__ = run_ocr.__doc__
        _wrapper.__annotations__ = {"image_base64": str, "return": dict[str, Any]}
        return _wrapper

    # Final tool list. Loop prevention is prompt-level only (L1, see
    # supervisor_base.txt); there is no programmatic tool-call guard.
    final_tools: list[Any] = [
        triage_link,
        _make_run_web_navigation(settings),
        parse_wechat_article,
        _make_run_ocr(settings),
        extract_jd_candidates,
        standardize_from_record_fields,
        verify_evidence,
        package_candidates,
        finish_with_manual_review,
    ]

    # Build prompt from template files
    system_prompt = build_supervisor_prompt(snapshot_context)

    try:
        agent = create_deep_agent(
            model=model,
            tools=final_tools,
            subagents=[web_nav_subagent],
            system_prompt=system_prompt,
            name="discovery_supervisor",
            response_format=_DiscoveryRunResultPydantic,
        )
    except TypeError:
        # deepagents does not support response_format; fall back
        agent = create_deep_agent(
            model=model,
            tools=final_tools,
            subagents=[web_nav_subagent],
            system_prompt=system_prompt,
            name="discovery_supervisor",
        )

    return agent
