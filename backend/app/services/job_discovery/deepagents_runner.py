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
from dataclasses import asdict, is_dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

import requests
from langchain_openai import ChatOpenAI

from deepagents import create_deep_agent
from deepagents.middleware.subagents import SubAgent

from backend.app.config import Settings
from backend.app.services.job_discovery.schemas import (
    DiscoveryRunResult,
    NormalizedJobCandidate,
    OcrResult,
    PageEvidence,
    TriageResult,
    WechatArticleResult,
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
        "temperature": 0.2,
    }
    api_key = get_api_key()
    if api_key:
        kwargs["api_key"] = api_key
    base_url = get_base_url()
    kwargs["base_url"] = base_url
    return ChatOpenAI(**kwargs)


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


# ---------------------------------------------------------------------------
# Web navigation state (per-run, module-level)
# ---------------------------------------------------------------------------

_web_nav_page_count: int = 0
_web_nav_max_pages: int = 20
_web_nav_history: list[str] = []
_web_nav_current_url: str | None = None


def _reset_web_nav_state(max_pages: int = 20) -> None:
    """Reset the web navigation state for a new run."""
    global _web_nav_page_count, _web_nav_max_pages, _web_nav_history, _web_nav_current_url
    _web_nav_page_count = 0
    _web_nav_max_pages = max_pages
    _web_nav_history = []
    _web_nav_current_url = None


def _check_page_budget() -> str | None:
    """Check if page budget is exhausted. Returns error message or None."""
    if _web_nav_page_count >= _web_nav_max_pages:
        return (
            f"Page budget exhausted ({_web_nav_page_count}/{_web_nav_max_pages} pages used). "
            "Cannot open more pages."
        )
    return None


def _fetch_page(url: str) -> tuple[str, str, str] | tuple[None, None, str]:
    """Fetch a URL and return (text_content, title, error_or_none).

    Returns (text, title, None) on success, (None, None, error_message) on failure.
    """
    domain_err = _is_blocked_domain(url)
    if domain_err:
        return None, None, f"Cannot access blocked domain: {url}"

    budget_err = _check_page_budget()
    if budget_err:
        return None, None, budget_err

    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "")
        if "text/html" not in content_type and "text/plain" not in content_type:
            return None, None, f"Non-text content type: {content_type}"

        html = resp.text
        # Extract title and text
        title = _extract_page_title(html)
        text = _extract_page_text(html)

        global _web_nav_page_count, _web_nav_current_url, _web_nav_history
        _web_nav_page_count += 1
        if _web_nav_current_url:
            _web_nav_history.append(_web_nav_current_url)
        _web_nav_current_url = url

        return text, title, None
    except requests.RequestException as e:
        return None, None, f"HTTP error fetching {url}: {e}"


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

def run_web_navigation(start_url: str, settings: Settings | None = None) -> dict[str, Any]:
    """Navigate from a start URL to discover job JD evidence pages.

    Fetches the start page, extracts links, follows career-related ones,
    and collects evidence pages up to the configured page budget.

    Args:
        start_url: The URL to start navigation from.
        settings: Optional Settings object (defaults to 20-page budget).

    Returns:
        Dict with evidence_pages (list of PageEvidence-like dicts) and
        navigation_path (list of visited URLs).
    """
    max_pages = (settings.job_discovery_max_pages_per_task
                 if settings else 20)
    _reset_web_nav_state(max_pages=max_pages)

    evidence_pages: list[dict[str, Any]] = []
    navigation_path: list[dict[str, str]] = []
    visited: set[str] = set()

    # Fetch the start page
    text, title, error = _fetch_page(start_url)
    if error:
        return {"evidence_pages": [], "navigation_path": [], "error": error}

    evidence_pages.append({
        "evidence_type": "page_text",
        "url": start_url,
        "title": title,
        "content_hash": hashlib.sha256((text or "").encode("utf-8")).hexdigest(),
        "text_excerpt": (text or "")[:2000],
        "metadata": {"page_num": _web_nav_page_count},
    })
    navigation_path.append({"url": start_url, "title": title or ""})
    visited.add(start_url)

    # Check if there are career-related links to follow
    # (We only do one level of following for the initial implementation)
    try:
        resp = requests.get(start_url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code == 200 and "text/html" in resp.headers.get("Content-Type", ""):
            links = _extract_links_from_html(resp.text, start_url)
            # Find career-related links
            career_keywords = re.compile(
                r"(career|job|jobs|recruit|position|openings|join\s*us|campus|"
                r"招聘|职位|校园|加入我们| careers?|求职)",
                re.IGNORECASE,
            )
            career_links = [l for l in links if career_keywords.search(l["url"]) or career_keywords.search(l["text"])]

            for link in career_links[:3]:  # Follow up to 3 career links
                if link["url"] in visited or _web_nav_page_count >= max_pages:
                    continue
                text2, title2, error2 = _fetch_page(link["url"])
                if error2:
                    continue
                evidence_pages.append({
                    "evidence_type": "page_text",
                    "url": link["url"],
                    "title": title2,
                    "content_hash": hashlib.sha256((text2 or "").encode("utf-8")).hexdigest(),
                    "text_excerpt": (text2 or "")[:2000],
                    "metadata": {"page_num": _web_nav_page_count},
                })
                navigation_path.append({"url": link["url"], "title": title2 or ""})
                visited.add(link["url"])
    except requests.RequestException:
        pass

    return {
        "evidence_pages": evidence_pages,
        "navigation_path": navigation_path,
        "page_count": _web_nav_page_count,
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

    # Reconstruct dataclass instances
    candidates = [NormalizedJobCandidate(**c) for c in candidates_data]
    evidence = [PageEvidence(**e) for e in evidence_data]

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


def _reset_nav_state(max_pages: int = 20) -> None:
    """Reset subagent navigation state."""
    global _nav_page_count, _nav_max_pages, _nav_history, _nav_current_url
    _nav_page_count = 0
    _nav_max_pages = max_pages
    _nav_history = []
    _nav_current_url = None


def _nav_budget_check() -> str | None:
    """Check page budget for subagent navigation."""
    if _nav_page_count >= _nav_max_pages:
        return f"Page budget exhausted ({_nav_page_count}/{_nav_max_pages})"
    return None


def open_url(url: str) -> str:
    """Open a URL and return its visible text content.

    Args:
        url: The URL to open.

    Returns:
        Page text content, or error message if the page cannot be accessed.
    """
    if _is_blocked_domain(url):
        return f"ERROR: Cannot access blocked domain: {url}"
    budget_err = _nav_budget_check()
    if budget_err:
        return f"ERROR: {budget_err}"

    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        html = resp.text
        text = _extract_page_text(html)

        global _nav_page_count, _nav_current_url, _nav_history
        _nav_page_count += 1
        if _nav_current_url:
            _nav_history.append(_nav_current_url)
        _nav_current_url = url

        return text or "(empty page)"
    except requests.RequestException as e:
        return f"ERROR: Could not open {url}: {e}"


def read_dom(url: str) -> str:
    """Read the DOM of a page as simplified text.

    Args:
        url: The URL to read.

    Returns:
        Simplified DOM text, or error message.
    """
    if _is_blocked_domain(url):
        return f"ERROR: Cannot access blocked domain: {url}"
    budget_err = _nav_budget_check()
    if budget_err:
        return f"ERROR: {budget_err}"

    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        html = resp.text
        # Return a simplified DOM: strip only script/style but keep structure
        html = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.IGNORECASE | re.DOTALL)
        html = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.IGNORECASE | re.DOTALL)
        html = re.sub(r"<!--.*?-->", "", html, flags=re.DOTALL)
        # Condense whitespace
        html = re.sub(r">\s+<", ">\n<", html)
        html = re.sub(r"\n{3,}", "\n\n", html)

        global _nav_page_count, _nav_current_url, _nav_history
        _nav_page_count += 1
        if _nav_current_url:
            _nav_history.append(_nav_current_url)
        _nav_current_url = url

        return html[:10000] or "(empty page)"
    except requests.RequestException as e:
        return f"ERROR: Could not read {url}: {e}"


def extract_links(url: str) -> str:
    """Extract all links from a page.

    Args:
        url: The URL to extract links from.

    Returns:
        JSON string of link objects with url and text fields.
    """
    if _is_blocked_domain(url):
        return json.dumps({"error": f"Cannot access blocked domain: {url}"})
    budget_err = _nav_budget_check()
    if budget_err:
        return json.dumps({"error": budget_err})

    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        html = resp.text
        links = _extract_links_from_html(html, url)

        global _nav_page_count, _nav_current_url, _nav_history
        _nav_page_count += 1
        if _nav_current_url:
            _nav_history.append(_nav_current_url)
        _nav_current_url = url

        return json.dumps(links, ensure_ascii=False)
    except requests.RequestException as e:
        return json.dumps({"error": f"Could not extract links from {url}: {e}"})


def click_link(url: str, link_text: str) -> str:
    """Follow a link on a page by matching link text.

    Args:
        url: The current page URL to scan for links.
        link_text: The text of the link to follow.

    Returns:
        Content of the followed page, or error.
    """
    if _is_blocked_domain(url):
        return f"ERROR: Cannot access blocked domain: {url}"
    budget_err = _nav_budget_check()
    if budget_err:
        return f"ERROR: {budget_err}"

    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        html = resp.text
        links = _extract_links_from_html(html, url)

        # Find the link whose text matches (case-insensitive, substring)
        target_url: str | None = None
        for link in links:
            if link_text.lower() in link["text"].lower():
                target_url = link["url"]
                break

        if not target_url:
            return f"ERROR: No link with text '{link_text}' found on {url}"

        # Follow the link
        resp2 = requests.get(target_url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        resp2.raise_for_status()
        html2 = resp2.text
        text2 = _extract_page_text(html2)

        global _nav_page_count, _nav_current_url, _nav_history
        _nav_page_count += 1
        if _nav_current_url:
            _nav_history.append(_nav_current_url)
        _nav_current_url = target_url

        return text2 or "(empty page)"
    except requests.RequestException as e:
        return f"ERROR: Could not follow link: {e}"


def get_visible_text(url: str) -> str:
    """Get visible text from a page (no markup).

    Args:
        url: The URL to get text from.

    Returns:
        Visible text content.
    """
    return open_url(url)


def screenshot(url: str) -> str:
    """Take a screenshot of a page (stub — returns placeholder).

    Args:
        url: The URL to screenshot.

    Returns:
        Base64 data URI placeholder (screenshot not available without browser).
    """
    return (
        "data:image/png;base64,"
        "SCREENSHOT_NOT_AVAILABLE: Real screenshot requires a browser "
        "(Playwright/Selenium) which is not integrated in this version."
    )


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
- Read visible text and DOM links.
- Follow navigation links likely related to Careers, Jobs, Join Us, Campus Recruitment, Internships, Recruiting, or Chinese equivalents.
- Capture evidence screenshots and page text.

Rules:
- Stay within the tool-enforced page budget.
- Do not attempt login.
- Do not solve captcha or anti-bot challenges.
- Return discovered JD evidence pages and discovery path.
- Do not extract final standardized jobs; the supervisor will call extraction tools."""

# System prompt for the Discovery Supervisor Agent (verbatim from spec)
_SUPERVISOR_SYSTEM_PROMPT = """\
You are the Discovery Supervisor Agent for a campus career assistant.

Goal: Given a Tencent smart sheet raw record and one source URL, discover job JD evidence, extract standard job candidates, verify evidence, and return a structured result.

Rules:
- Use tools in a loop. Decide the next action from observations.
- Do not bypass login, captcha, anti-bot, permission, or paywall barriers.
- If blocked by login, captcha, anti-bot, unavailable WeChat content, or permission limits, finish as needs_manual_review with a precise reason.
- Do not write to the database. Return structured evidence and candidates only.
- Respect all budgets enforced by tools.
- Prefer evidence from official company, official career site, public WeChat article, or direct recruitment page.
- Email application instructions are valid application channels. Extract email, subject hint, materials, and original instruction.
- If information is insufficient, ask tools for more evidence or finish as needs_manual_review.
- Never invent company, title, location, deadline, or apply method."""


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
        read_dom,
        extract_links,
        click_link,
        get_visible_text,
        screenshot,
        go_back,
    ]

    subagent: SubAgent = {
        "name": "web_navigation_agent",
        "description": (
            "Navigates web pages to discover job JD evidence. "
            "Provides page text, links, and screenshots. "
            "Enforces page budget and domain safety."
        ),
        "system_prompt": _WEB_NAVIGATION_SYSTEM_PROMPT,
        "tools": web_nav_tools,
    }

    return subagent


# ---------------------------------------------------------------------------
# Discovery Supervisor Agent builder
# ---------------------------------------------------------------------------


def build_discovery_supervisor_agent(
    *,
    settings: Settings,
    model: ChatOpenAI | None = None,
) -> Any:
    """Build the Discovery Supervisor Agent using deepagents.

    Creates a compiled LangGraph agent with:
    - 8 supervisor tools wrapping Phase 4 deterministic functions
    - A WebNavigationAgent subagent for web navigation
    - Structured output via DiscoveryRunResult (if supported) or tool-based

    Args:
        settings: Application settings (model name, page budget, etc.).
        model: Optional pre-built ChatOpenAI instance. If None, one is
               created from settings.

    Returns:
        A CompiledStateGraph (deep agent) ready for invocation.
    """
    if model is None:
        model = _build_job_discovery_llm(settings)

    # Create the web navigation subagent
    web_nav_subagent = create_web_navigation_subagent(settings)

    # Define the 8 supervisor tools
    supervisor_tools = [
        triage_link,
        run_web_navigation,
        parse_wechat_article,
        run_ocr,
        extract_jd_candidates,
        verify_evidence,
        package_candidates,
        finish_with_manual_review,
    ]

    # Build a partial-application wrapper for tools that need settings
    # Since deepagents tools are plain functions, we create closures that
    # capture the settings.

    def _make_run_web_navigation(settings: Settings):
        def _wrapper(start_url: str) -> dict[str, Any]:
            return run_web_navigation(start_url, settings=settings)
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

    # Final tool list with settings-bound closures
    final_tools: list[Any] = [
        triage_link,
        _make_run_web_navigation(settings),
        parse_wechat_article,
        _make_run_ocr(settings),
        extract_jd_candidates,
        verify_evidence,
        package_candidates,
        finish_with_manual_review,
    ]

    # Create the deep agent
    agent = create_deep_agent(
        model=model,
        tools=final_tools,
        subagents=[web_nav_subagent],
        system_prompt=_SUPERVISOR_SYSTEM_PROMPT,
        name="discovery_supervisor",
    )

    return agent
