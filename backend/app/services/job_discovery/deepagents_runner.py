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
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from deepagents import create_deep_agent
from deepagents.middleware.subagents import SubAgent

from backend.app.config import Settings
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
        "temperature": 0.2,
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

from pathlib import Path

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
    global _web_nav_page_count, _web_nav_current_url, _web_nav_history

    domain_err = _is_blocked_domain(url)
    if domain_err:
        return None, None, f"Cannot access blocked domain: {url}"

    budget_err = _check_page_budget()
    if budget_err:
        return None, None, budget_err

    # ── WeChat articles: try ReadGZH proxy first ──
    if _is_wechat_url(url):
        text, title, error = _fetch_wechat_via_readgzh(url)
        if error is None:
            _web_nav_page_count += 1
            if _web_nav_current_url:
                _web_nav_history.append(_web_nav_current_url)
            _web_nav_current_url = url
            return text, title, None
        # ReadGZH failed; fall through to regular fetch + browser fallback

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

        if _needs_browser_fallback(url, text):
            browser_text, browser_title, browser_error = _fetch_page_with_browser(url)
            if browser_error is None:
                text = browser_text or ""
                title = browser_title or title
            else:
                return None, None, f"WeChat verification wall: {browser_error}"

        _web_nav_page_count += 1
        if _web_nav_current_url:
            _web_nav_history.append(_web_nav_current_url)
        _web_nav_current_url = url

        return text, title, None
    except requests.RequestException as e:
        return None, None, f"HTTP error fetching {url}: {e}"


def _fetch_wechat_via_readgzh(url: str) -> tuple[str | None, str | None, str | None]:
    """Fetch a WeChat article via the ReadGZH proxy service.

    ReadGZH (https://readgzh.site) is a server-side proxy that bypasses
    WeChat's client fingerprinting to return clean, AI-readable article HTML.
    Articles are permanently cached — repeated reads incur zero credit cost.

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
            page = browser.new_page(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125 Safari/537.36"
                )
            )
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
    _reset_web_nav_state(max_pages=max_pages)

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
    _reset_nav_state(max_pages=max_pages)
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
        result = agent.invoke({"messages": [HumanMessage(content=prompt)]})
    except Exception as exc:  # noqa: BLE001 - agent/model providers raise heterogeneous errors
        return {
            "evidence_pages": [],
            "navigation_path": [],
            "page_count": _nav_page_count,
            "error": f"WebNavigationAgent failed: {exc}",
            "delegated_to": "web_navigation_agent",
        }
    return _parse_web_navigation_agent_result(result)


def _run_via_subagent_delegation(
    start_url: str,
    subagent: SubAgent,
    model: Any,
    max_pages: int,
) -> dict[str, Any]:
    """Attempt to delegate web navigation to the WebNavigationAgent subagent.

    Constructs a navigation prompt and invokes the subagent via
    deepagents' create_sub_agent. If the subagent's runnable does not
    support programmatic invocation, this raises TypeError or ValueError,
    telling the caller to fall back to direct navigation.

    Args:
        start_url: The URL to start navigation from.
        subagent: WebNavigationAgent subagent spec.
        model: Model instance for subagent creation.
        max_pages: Page budget.

    Returns:
        Dict with evidence_pages, navigation_path, and page_count.
    """
    from deepagents.middleware.subagents import create_sub_agent

    prompt = (
        f"Starting from {start_url}, find job listing pages "
        f"and JD detail pages. Return the evidence pages you find. "
        f"Page budget: {max_pages} pages."
    )

    # Inject model into subagent spec for create_sub_agent
    spec_with_model: SubAgent = {
        **subagent,  # type: ignore[arg-type]
        "model": model,
    }
    runnable = create_sub_agent(spec_with_model)
    result = runnable.invoke({"messages": [HumanMessage(content=prompt)]})

    # Parse structured output or messages-based result
    if isinstance(result, dict):
        result_evidence = (
            result.get("evidence_pages")
            or result.get("evidence")
            or []
        )
        result_path = (
            result.get("navigation_path")
            or result.get("path")
            or []
        )
        evidence_list = (
            list(result_evidence)
            if not isinstance(result_evidence, list)
            else result_evidence
        )
        path_list = (
            list(result_path)
            if not isinstance(result_path, list)
            else result_path
        )
        return {
            "evidence_pages": evidence_list,
            "navigation_path": path_list,
            "page_count": len(evidence_list),
            "delegated_to": "web_navigation_agent",
        }

    return {
        "evidence_pages": [],
        "navigation_path": [],
        "page_count": 0,
        "delegated_to": "web_navigation_agent",
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
    global _nav_page_count, _nav_current_url, _nav_history

    if _is_blocked_domain(url):
        return f"ERROR: Cannot access blocked domain: {url}"
    budget_err = _nav_budget_check()
    if budget_err:
        return f"ERROR: {budget_err}"

    # ── WeChat articles: try ReadGZH proxy first ──
    if _is_wechat_url(url):
        text, title, error = _fetch_wechat_via_readgzh(url)
        if error is None:
            _nav_page_count += 1
            if _nav_current_url:
                _nav_history.append(_nav_current_url)
            _nav_current_url = url
            return text or "(empty page)"
        # ReadGZH failed; fall through to regular fetch

    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        html = resp.text
        text = _extract_page_text(html)

        if _needs_browser_fallback(url, text):
            browser_text, browser_title, browser_error = _fetch_page_with_browser(url)
            if browser_error is None:
                text = browser_text or ""
            else:
                return f"ERROR: WeChat verification wall: {browser_error}"

        _nav_page_count += 1
        if _nav_current_url:
            _nav_history.append(_nav_current_url)
        _nav_current_url = url

        return text or "(empty page)"
    except requests.RequestException as e:
        return f"ERROR: Could not open {url}: {e}"


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
            page = browser.new_page(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125 Safari/537.36"
                )
            )
            page.on("response", capture_response)
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            try:
                page.wait_for_load_state("networkidle", timeout=15_000)
            except PlaywrightTimeoutError:
                page.wait_for_timeout(3_000)
            title = page.title()
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
            "metadata": {"captured_response_count": len(payloads), "response_urls": response_urls[:5]},
        },
        ensure_ascii=False,
    )


def _fetch_alibaba_search_api(url: str) -> dict[str, Any]:
    """Call the Alibaba campus position search JSON API directly.

    Extracts the batch ID and share code from the source SPA URL,
    then calls the ``/position/search`` API endpoint directly,
    bypassing the browser-based SPA rendering entirely.

    Args:
        url: The Alibaba campus SPA URL (e.g. ``https://campus-talent.alibaba.com/
             campus/position?batchId=...&campusShareCode=...``).

    Returns:
        The parsed JSON response from the position search API.

    Raises:
        requests.RequestException: If the API call fails.
        ValueError: If the response is not valid JSON.
    """
    from urllib.parse import urlencode, urlparse

    parsed = urlparse(url)
    # Build the search API URL from the same base host
    api_base = f"{parsed.scheme}://{parsed.netloc}/position/search"

    # Extract relevant query parameters from the original URL
    params: dict[str, str] = {}
    if parsed.query:
        for pair in parsed.query.split("&"):
            if "=" in pair:
                k, v = pair.split("=", 1)
                from urllib.parse import unquote_plus
                params[k] = unquote_plus(v)

    # Preserve batchId and campusShareCode — these are the key params
    # the Alibaba SPA sends to its search API.
    filtered: dict[str, str] = {}
    for key in ("batchId", "campusShareCode", "batchIds"):
        if key in params:
            filtered[key] = params[key]

    query_string = urlencode(filtered)
    api_url = f"{api_base}?{query_string}" if query_string else api_base

    resp = requests.get(
        api_url,
        timeout=30,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Referer": url,
        },
    )
    resp.raise_for_status()
    return resp.json()


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
- Open pages in a headless browser when static HTML is empty or JavaScript-rendered.
- Extract rendered job evidence from public recruitment XHR/JSON when a page is an SPA.
- Read visible text and DOM links.
- Follow navigation links likely related to Careers, Jobs, Join Us, Campus Recruitment, Internships, Recruiting, or Chinese equivalents.
- Capture evidence screenshots and page text.

Rules:
- Stay within the tool-enforced page budget.
- Do not attempt login.
- Do not solve captcha or anti-bot challenges.
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
            get_visible_text,
            screenshot,
            go_back,
        ],
        system_prompt=_WEB_NAVIGATION_SYSTEM_PROMPT,
        name="web_navigation_agent",
        response_format=_WebNavigationResultPydantic,
    )


def build_discovery_supervisor_agent(
    *,
    settings: Settings,
    model: ChatOpenAI | None = None,
    snapshot_context: dict | None = None,
) -> Any:
    """Build the Discovery Supervisor Agent using deepagents.

    Creates a compiled LangGraph agent with:
    - 8 supervisor tools wrapping Phase 4 deterministic functions
    - A WebNavigationAgent subagent for web navigation
    - Structured output via DiscoveryRunResult
    - Optional snapshot_context for breakpoint takeover

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

    # Final tool list with settings-bound closures
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
