"""Evidence-first public-page tool for the PEV ``job-discovery`` Skill."""

from __future__ import annotations

import base64
from collections.abc import Callable
from html.parser import HTMLParser
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import socket
import sys
import threading
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from pydantic import BaseModel, Field, field_validator
import requests

from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.job_discovery.tools.batch_progress import run_parallel_with_progress
from backend.app.services.job_discovery.tools.jd_extraction import extract_jd_candidates


_MAX_PUBLIC_REDIRECTS = 5
_PUBLIC_FETCH_HEADERS = {"User-Agent": "CareerAssistantPEV/1.0 (+public-job-fetch)"}

# SPA / JS-rendered career sites (bytedance, tencent, zhaopin, lagou, ...)
# return an empty shell to plain ``requests``. When the requests fast path
# fails or yields no visible text, the fetch falls back to a headless-Chromium
# render that re-applies the same public-URL safety checks per request. The
# fallback is off by default so unit suites never launch a browser; runtime
# assembly enables it from ``Settings.job_discovery_playwright_fallback_enabled``.
_PLAYWRIGHT_FALLBACK_CODES = frozenset(
    {"public_fetch_failed", "empty_public_page", "public_page_content_insufficient"}
)
# Visible text below this many chars is a shell (SPA boot stub, "Not Found",
# anti-bot placeholder) rather than usable job evidence; the renderer decides.
_MIN_USABLE_TEXT_CHARS = 160
# Upper bound for the visible-text evidence captured from one page. The Feishu
# campus portal renders a whole 100-job listing (with inline JD sections) in a
# single DOM pass (~26k chars for the 61 NIO agent roles), so the cap must
# comfortably exceed the largest single-portal render while staying far under
# the 48k per-run evidence budget kept full for recent artifacts.
_MAX_VISIBLE_TEXT_CHARS = 32_000
_PLAYWRIGHT_FALLBACK_ENABLED = False
# P2 (2026-08-09): a rendered page is a JS card-list when it exposes >= this
# many same-host job-shaped detail links while carrying no JD-section text;
# the batch fetch then deep-fetches up to this many detail pages so
# match-observed-jobs sees real JD body instead of an empty card shell.
_MIN_LIST_LINKS = 2
_MAX_LIST_EXPANSION = 5
# Render calls are serialized behind this lock and bounded by a hard
# watchdog: the C5 batch fetch runs up to 4 worker threads, and playwright's
# sync API is thread-affine (its event loop binds to the thread that started
# it), so two threads touching the shared browser at once wedge the driver
# forever. A wedged render is abandoned at the deadline -- the worker is a
# daemon and the runtime is orphaned, never torn down from a foreign thread.
_RENDER_LOCK = threading.Lock()
_RENDER_TIMEOUT_S = 60
_JD_SECTION_MARKERS = (
    "岗位职责",
    "岗位要求",
    "职位描述",
    "工作职责",
    "任职要求",
    "职责描述",
    "responsibilities",
)
# A3 (round1): the card-list gate only scans this many leading characters of
# the list body for JD-section markers. Detail pages put their markers near
# the top (char ~187/255); card shells carry them only in footer SEO text
# (often past char 5k) -- so a marker anywhere in the body (RC-C) wrongly
# blocked a shell with a job-looking footer from expanding.
_JD_MARKER_SCAN_HEAD_CHARS = 2_000
_PLAYWRIGHT_FETCH_IMPL: Callable[[str], tuple[str, str | None]] | None = None
_PLAYWRIGHT_RUNTIME: tuple[Any, Any] | None = None


def enable_playwright_fallback(enabled: bool) -> None:
    """Toggle the rendered-fetch fallback (called from runtime assembly)."""
    global _PLAYWRIGHT_FALLBACK_ENABLED
    _PLAYWRIGHT_FALLBACK_ENABLED = enabled


# A1 certified-adapter gate (mirror of the Playwright fallback toggle). The
# skill's adapters package (skill/job-discovery/scripts/adapters) is loaded
# in-process -- never via subprocess -- and only when runtime assembly opts in
# from ``Settings.use_public_api_adapters``. A covered URL that fails is a
# hard ``adapter:<code>`` blocked error; an unloaded package or an uncovered
# URL degrades to the normal requests/Playwright chain.
_PUBLIC_API_ADAPTERS_ENABLED = False
_ADAPTERS_PACKAGE: Any | None = None
_ADAPTERS_SCRIPTS_DIR = str(
    Path(__file__).resolve().parents[4] / "skill" / "job-discovery" / "scripts"
)


def enable_public_api_adapters(enabled: bool) -> None:
    """Toggle the A1 certified-adapter channel (called from runtime assembly)."""
    global _PUBLIC_API_ADAPTERS_ENABLED
    _PUBLIC_API_ADAPTERS_ENABLED = enabled


# The WeChat OCR channel owns ``mp.weixin.qq.com`` article pages the same way
# a certified adapter owns its hosts: their bodies are image content that the
# plain requests/render chain reads as an empty shell, so the fetch tool
# routes them to the OCR slice (``wechat.fetch_wechat_article``) before the
# generic chain. The gate mirrors ``Settings.job_discovery_ocr_enabled``,
# wired from runtime assembly like the adapter and Playwright toggles.
_WECHAT_ARTICLE_HOST = "mp.weixin.qq.com"


def _adapter_package() -> Any | None:
    """Load the skill adapters package once; None when it cannot be imported.

    The sys.path injection is idempotent and only adds the skill scripts
    directory (same shape as the deepagents ``browse_fetch`` loader).
    """
    global _ADAPTERS_PACKAGE
    if _ADAPTERS_PACKAGE is None:
        scripts_dir = _ADAPTERS_SCRIPTS_DIR
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        try:
            import adapters  # noqa: PLC0415 - lazy, guarded by callers
        except Exception:  # noqa: BLE001 - untrusted skill boundary.
            return None
        _ADAPTERS_PACKAGE = adapters
    return _ADAPTERS_PACKAGE


_JOB_RESULT_URL_TOKENS = (
    "career",
    "job",
    "jobs",
    "talent",
    "recruit",
    "zhaopin",
    "position",
    "campus",
    "greenhouse",
    "lever.co",
    "workday",
)
_JOB_RESULT_TEXT_RE = re.compile(
    r"(?:招聘|职位|岗位|校招|社招|实习|工程师|开发|算法|researcher|"
    r"engineer|developer|intern|hiring|career|job)",
    re.IGNORECASE,
)
# P2 (B4): known recruiting hosts pass on the loose URL-token OR text-signal
# check; any other host must carry a job-shaped URL path. This rejects the
# tutorial/encyclopedia noise that merely mentions 招聘/岗位 in its title.
# Patterns ending in "." match the first label (careers.example,
# career.hebut.edu.cn, jobs.bytedance.com); plain domains match by suffix.
_JOB_SEARCH_ALLOWED_HOST_PATTERNS = (
    "careers.",
    "jobs.",
    "campus.",
    "talent.",
    "recruit.",
    "hr.",
    "job.",
    "liepin.com",
    "iguopin.com",
    "zhaopin.com",
    "shixiseng.com",
    "lagou.com",
    "mokahr.com",
    "feishu.cn",
    "ncss.cn",
    "fenbi.com",
    "juejin.cn",
)
# site: operators appended to the Bing query (skipped when the agent already
# steers with "site:") so the provider itself biases toward recruiting domains
# instead of returning 教程/百科/官网首页 noise.
_JOB_SEARCH_SITE_OPERATORS = (
    "site:liepin.com",
    "site:iguopin.com",
    "site:zhaopin.com",
    "site:shixiseng.com",
    "site:lagou.com",
    "site:job.ncss.cn",
    "site:talent.baidu.com",
    "site:jobs.bytedance.com",
    "site:careers.tencent.com",
    "site:campus.tencent.com",
    "site:juejin.cn",
)


class PublicJobFetchError(RuntimeError):
    """Stable, non-sensitive public-web fetch failure.

    ``message`` (optional) enriches the agent-facing error text while
    ``code`` stays the stable, machine-testable identity.  When omitted the
    error text falls back to the code itself.
    """

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code


class FetchPublicJobPageInput(BaseModel):
    """One public HTTP(S) URL selected autonomously by Executor."""

    url: str = Field(min_length=1, max_length=2_048)

    @field_validator("url")
    @classmethod
    def normalize_url(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("url must not be empty")
        return cleaned


class FetchPublicJobPageOutput(BaseModel):
    artifact_id: str
    source_url: str
    title: str | None
    visible_text: str
    content_hash: str


class FetchPublicJobPagesInput(BaseModel):
    """A finite Agent-selected set of official public pages to capture at once."""

    urls: list[str] = Field(min_length=1, max_length=10)

    @field_validator("urls")
    @classmethod
    def normalize_urls(cls, values: list[str]) -> list[str]:
        cleaned = [FetchPublicJobPageInput.normalize_url(value) for value in values]
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("urls must not contain duplicates")
        return cleaned


class PublicJobPageFetchFailure(BaseModel):
    """One transparent per-page failure; successful evidence remains usable."""

    source_url: str
    error_code: str


class FetchPublicJobPagesOutput(BaseModel):
    """All successfully captured pages plus explicit failures from one bounded batch."""

    pages: list[FetchPublicJobPageOutput]
    failures: list[PublicJobPageFetchFailure] = Field(default_factory=list)


class SearchPublicJobPagesInput(BaseModel):
    """A bounded public-web query selected by the Executor from the user's goal."""

    query: str = Field(min_length=2, max_length=400)
    max_results: int = Field(default=5, ge=1, le=10)

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if not cleaned:
            raise ValueError("query must not be empty")
        return cleaned


class PublicJobSearchResult(BaseModel):
    """One direct public career link observed in a fixed search-provider response."""

    title: str
    url: str
    snippet: str | None = None


class SearchPublicJobPagesOutput(BaseModel):
    """Search evidence that lets the Executor choose a public page to inspect next."""

    query: str
    source_url: str
    content_hash: str
    results: list[PublicJobSearchResult]


class ExtractObservedJobDetailsInput(BaseModel):
    """The immutable evidence artifact selected by an autonomous Agent."""

    artifact_id: str = Field(min_length=1, max_length=80)


class ExtractedJobDetails(BaseModel):
    """A normalized job DTO whose values remain traceable to one page artifact."""

    title: str | None
    company_name: str | None
    locations: list[str]
    responsibilities: str
    requirements: str
    recruitment_types: list[str]
    apply_url: str | None
    deadline_text: str | None
    confidence: float
    evidence_refs: list[dict[str, str]]
    normalization_warnings: list[str]
    # FindJobs-derived structured features (optional v1 fields; see
    # docs/findjobs-optimization-plan.zh-CN.md §6 - no MySQL migration).
    skills: list[str] = Field(default_factory=list)  # A2: closed-set tags
    min_degree: str | None = None                    # B3: degree whitelist value
    priority: str = "unknown"                        # B3: must/preferred/unknown
    # B1: strength dict {score, tier, base_score, evidence[]}; optional.
    strength: dict[str, Any] | None = None
    # B2: taxonomy [level1, level2]; empty list when unclassified.
    taxonomy: list[str] = Field(default_factory=list)


class ExtractObservedJobDetailsOutput(BaseModel):
    """Structured JD candidates derived only from a selected captured page."""

    source_artifact_id: str
    source_url: str
    content_hash: str
    candidates: list[ExtractedJobDetails]


class ExtractObservedJobDetailsBatchInput(BaseModel):
    """A finite set of previously observed evidence artifacts to normalize."""

    artifact_ids: list[str] = Field(min_length=1, max_length=10)

    @field_validator("artifact_ids")
    @classmethod
    def validate_artifact_ids(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values]
        if any(not value for value in cleaned) or len(set(cleaned)) != len(cleaned):
            raise ValueError("artifact_ids must be non-empty and unique")
        return cleaned


class ExtractObservedJobDetailsBatchOutput(BaseModel):
    """One structured result per requested immutable public-page artifact."""

    details: list[ExtractObservedJobDetailsOutput]


class _VisibleTextParser(HTMLParser):
    _IGNORED = {"script", "style", "noscript", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ignored_depth = 0
        self._in_title = False
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag in self._IGNORED:
            self._ignored_depth += 1
        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag in self._IGNORED and self._ignored_depth:
            self._ignored_depth -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        normalized = " ".join(data.split())
        if not normalized or self._ignored_depth:
            return
        if self._in_title:
            self.title_parts.append(normalized)
        self.text_parts.append(normalized)


class _BingSearchResultParser(HTMLParser):
    """Small HTML parser for direct links in Bing's public result cards."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._current: dict[str, list[str] | str] | None = None
        self._in_heading = False
        self._in_snippet = False

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        attributes = dict(attrs)
        if tag == "li" and self._current is None and "b_algo" in attributes.get("class", ""):
            self._current = {"title": [], "snippet": []}
            return
        if self._current is None:
            return
        if tag == "h2":
            self._in_heading = True
        elif tag == "p":
            self._in_snippet = True
        elif tag == "a" and self._in_heading:
            href = attributes.get("href")
            if href:
                self._current["url"] = href

    def handle_endtag(self, tag: str) -> None:
        if self._current is None:
            return
        if tag == "h2":
            self._in_heading = False
        elif tag == "p":
            self._in_snippet = False
        elif tag == "li":
            title = " ".join(self._current.get("title", []))
            url = self._current.get("url")
            snippet = " ".join(self._current.get("snippet", []))
            if isinstance(url, str) and title:
                item = {"title": title, "url": url}
                if snippet:
                    item["snippet"] = snippet
                self.results.append(item)
            self._current = None
            self._in_heading = False
            self._in_snippet = False

    def handle_data(self, data: str) -> None:
        if self._current is None:
            return
        normalized = " ".join(data.split())
        if not normalized:
            return
        if self._in_heading:
            title_parts = self._current["title"]
            assert isinstance(title_parts, list)
            title_parts.append(normalized)
        elif self._in_snippet:
            snippet_parts = self._current["snippet"]
            assert isinstance(snippet_parts, list)
            snippet_parts.append(normalized)


class _SoSearchResultParser(HTMLParser):
    """Read 360's public result anchors that expose their direct target URL."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._url: str | None = None
        self._title_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag != "a" or self._url is not None:
            return
        direct_url = dict(attrs).get("data-mdurl")
        if isinstance(direct_url, str) and direct_url:
            self._url = direct_url
            self._title_parts = []

    def handle_data(self, data: str) -> None:
        if self._url is None:
            return
        normalized = " ".join(data.split())
        if normalized:
            self._title_parts.append(normalized)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or self._url is None:
            return
        title = " ".join(self._title_parts)
        if title:
            self.results.append({"title": title, "url": self._url})
        self._url = None
        self._title_parts = []


def _is_public_url(url: str) -> bool:
    """True only for http(s), userinfo-free hosts resolving to a global IP.

    Returns False for non-http(s) schemes, embedded credentials, unresolvable
    hosts, and non-global (loopback / RFC1918 / link-local / cloud-metadata)
    addresses -- a permissive check used by the Playwright route guard, which
    must fail closed on any ambiguous destination.
    """
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        return False
    try:
        addresses = socket.getaddrinfo(parsed.hostname, None, type=socket.SOCK_STREAM)
    except OSError:
        return False
    return all(
        ipaddress.ip_address(sockaddr[0]).is_global
        for _family, _kind, _proto, _canon, sockaddr in addresses
    )


def _assert_public_url(url: str) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise PublicJobFetchError("unsafe_public_url")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, None, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise PublicJobFetchError("public_host_unresolvable") from exc
    for _family, _kind, _proto, _canon, sockaddr in addresses:
        if not ipaddress.ip_address(sockaddr[0]).is_global:
            raise PublicJobFetchError("unsafe_public_url")


def _fetch_validated(url: str) -> requests.Response:
    """GET ``url`` following redirects manually, re-checking every hop is public.

    ``requests`` follows 3xx redirects automatically, but it never re-runs
    ``_assert_public_url`` against the ``Location`` target -- so a public URL
    that redirects to an internal address (loopback, link-local, RFC1918, or a
    cloud metadata endpoint) would let an Agent read private network state. We
    disable auto-redirect and walk each hop ourselves, re-validating the scheme,
    absence of userinfo, and global-IP rule on every resolved target.

    One transport-level retry is performed before a timeout/connection failure
    is handed back: a single transient failure (flaky CDN edge, connection
    reset) should not waste a whole Executor turn on a URL that succeeds a
    moment later. Only ``requests.RequestException`` (timeout / connection /
    HTTP transport errors) is retried; a ``PublicJobFetchError`` from the
    redirect-walk (``unsafe_public_url``) or any blocked/4xx outcome stays
    final -- retrying a security rejection would be both useless and risky.
    """
    current = url
    attempt = 0
    while True:
        try:
            for _ in range(_MAX_PUBLIC_REDIRECTS + 1):
                response = requests.get(
                    current,
                    timeout=20,
                    allow_redirects=False,
                    headers=_PUBLIC_FETCH_HEADERS,
                )
                if not response.is_redirect:
                    return response
                target = response.headers.get("Location")
                if not target:
                    return response
                target = urljoin(current, target)
                _assert_public_url(target)
                current = target
            raise PublicJobFetchError("unsafe_public_url")
        except requests.RequestException:
            if attempt >= 1:
                raise
            attempt += 1
            # restart the whole redirect walk from the original URL
            current = url


def _render_with_playwright(
    url: str, *, collect_links: bool = False
) -> tuple[str, str | None] | tuple[str, str | None, list[str]]:
    """Render ``url`` in headless Chromium; return (body_text, title[, links]).

    Uses the seam ``_PLAYWRIGHT_FETCH_IMPL`` when injected (unit tests); the
    real path lazily imports playwright and reuses one browser per process.
    A per-request route guard aborts any request whose destination is not a
    global public address, mirroring ``_assert_public_url`` inside the
    rendered page (SPA redirects and fetch() subresources included).
    With ``collect_links=True`` the rendered DOM's same-host job-shaped
    ``<a href>`` targets are also returned (list-page expansion, P2).

    The real path relaunches the shared browser exactly once (RC-A): a
    generic crash / OOM / CDP-disconnect mid-render tears the dead runtime
    down and retries, so one dead browser can never fail every later render
    in the process.  A raised ``PublicJobFetchError`` is never retried --
    security/validation rejections stay final.
    """
    if _PLAYWRIGHT_FETCH_IMPL is not None:
        rendered = _PLAYWRIGHT_FETCH_IMPL(url)
        body, title = rendered[:2]
        if collect_links:
            return body, title, list(rendered[2]) if len(rendered) >= 3 else []
        return body, title
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise PublicJobFetchError("public_fetch_failed") from None

    def _render_once(browser: Any, target_url: str) -> tuple[Any, ...]:
        """One render pass against ``browser``; the caller owns retry policy."""
        page = browser.new_page()

        def _abort_non_public(route: Any, request: Any) -> None:
            try:
                if _is_public_url(request.url):
                    route.continue_()
                else:
                    route.abort()
            except Exception:
                route.abort()

        page.route("**/*", _abort_non_public)
        try:
            response = page.goto(
                target_url, wait_until="domcontentloaded", timeout=20_000
            )
            if response is None:
                raise PublicJobFetchError("public_fetch_failed")
            # SPA career portals frequently finish rendering long after
            # domcontentloaded (deferred data fetch + re-render timers; the
            # Feishu campus portal paints its job list ~10s late). Poll the
            # body text until it stops growing above the usable-text threshold,
            # capped at ~15s, so a late-rendering page is not returned as an
            # empty shell. A below-threshold shell must NOT break early: it is
            # exactly the pre-render state we are waiting out.
            page.wait_for_timeout(1_500)
            body_text = page.inner_text("body") or ""
            stable_samples = 0
            for _ in range(30):
                previous_len = len(body_text.strip())
                page.wait_for_timeout(500)
                body_text = page.inner_text("body") or ""
                current_len = len(body_text.strip())
                if (
                    current_len >= _MIN_USABLE_TEXT_CHARS
                    and current_len == previous_len
                ):
                    stable_samples += 1
                    if stable_samples >= 2:
                        break
                else:
                    stable_samples = 0
            title = page.title() or None
            if not collect_links:
                return body_text, title
            return body_text, title, _collect_page_links(page, target_url)
        finally:
            page.close()

    def _run_render() -> tuple[Any, ...]:
        """Launch-or-reuse the shared browser and render; relaunch once."""
        global _PLAYWRIGHT_RUNTIME
        attempt = 0
        while True:
            pw, browser = _PLAYWRIGHT_RUNTIME or (None, None)
            if browser is None:
                try:
                    pw = sync_playwright().start()
                    browser = pw.chromium.launch(headless=True)
                    _PLAYWRIGHT_RUNTIME = (pw, browser)
                except Exception:
                    if pw is not None:
                        try:
                            pw.stop()
                        except Exception:
                            pass
                    raise PublicJobFetchError("public_fetch_failed") from None
            try:
                return _render_once(browser, url)
            except PublicJobFetchError:
                raise
            except Exception:
                # The shared browser died mid-render (crash / OOM / CDP
                # disconnect). Every later attempt against the dead runtime
                # would fail in ~0.0s, so tear it down and relaunch exactly
                # once (RC-A). A PublicJobFetchError is never retried:
                # security/validation rejections and the deliberate
                # blocked-page path stay final.
                if attempt >= 1:
                    raise PublicJobFetchError("public_fetch_failed") from None
                attempt += 1
                try:
                    browser.close()
                except Exception:
                    pass
                try:
                    pw.stop()
                except Exception:
                    pass
                _PLAYWRIGHT_RUNTIME = None

    # Serialize renders (playwright sync is thread-affine; the C5 batch fetch
    # runs 4 threads) and bound each call with a hard watchdog. A wedged
    # driver must not hang the whole eval process: the worker is a daemon,
    # the runtime is orphaned for the next call to replace, and teardown is
    # never attempted from a foreign thread (that call is itself the hang).
    global _PLAYWRIGHT_RUNTIME
    with _RENDER_LOCK:
        boxed: list[tuple[Any, ...]] = []
        errors: list[BaseException] = []

        def _watchdog_target() -> None:
            try:
                boxed.append(_run_render())
            except BaseException as exc:  # noqa: BLE001 - re-raised on caller
                errors.append(exc)

        worker = threading.Thread(target=_watchdog_target, daemon=True)
        worker.start()
        worker.join(timeout=_RENDER_TIMEOUT_S)
        if worker.is_alive():
            _PLAYWRIGHT_RUNTIME = None
            raise PublicJobFetchError("public_fetch_failed")
        if errors:
            raise errors[0]
        return boxed[0]


def _adapter_company_for_url(url: str) -> str | None:
    """Adapter company covering ``url``, or None (uncovered / unavailable).

    Packages exposing the certified ``_ADAPTERS`` registry (company ->
    class with class-level ``hosts``) are matched from that registry with
    no adapter instantiation -- each adapter ``__init__`` builds an httpx
    client (~1s on Windows), so instantiating per lookup is ~5s.  The
    registry is authoritative: a miss means the URL is uncovered.  A
    package without the registry shape falls back to its own
    ``company_for_url`` so the untrusted-boundary semantics stay intact.
    """
    package = _adapter_package()
    if package is None:
        return None
    registry = getattr(package, "_ADAPTERS", None)
    if isinstance(registry, dict):
        try:
            host = (urlsplit(url).hostname or "").lower()
        except ValueError:
            host = ""
        if host:
            try:
                matched = _host_to_company(registry, host)
            except Exception:  # noqa: BLE001 - untrusted adapter boundary.
                matched = None
            if matched is not None:
                return matched
        return None
    try:
        company = package.company_for_url(url)
    except Exception:  # noqa: BLE001 - untrusted adapter boundary.
        return None
    return company if isinstance(company, str) and company else None


def _host_to_company(registry: dict[str, Any], host: str) -> str | None:
    """Host -> company from certified adapter classes, no instantiation.

    Mirrors the adapters package's own matching (exact host or suffix
    under a ``*.`` wildcard pattern); None when no class claims the host.
    """
    for company, adapter_cls in registry.items():
        for pattern in getattr(adapter_cls, "hosts", ()):
            if host == pattern or host.endswith("." + pattern.lstrip("*.")):
                return company if isinstance(company, str) and company else None
    return None


def _run_company_adapter(package: Any, url: str, company: str) -> list[dict[str, Any]]:
    """Execute one certified adapter; any failure is an ``adapter:<code>`` block."""
    try:
        adapter = package.load_company_adapter(company)
        result = adapter.execute(url, None, None)
    except Exception as exc:  # noqa: BLE001 - untrusted adapter boundary.
        code = getattr(exc, "code", "unexpected")
        raise PublicJobFetchError(f"adapter:{code}") from exc
    records = result.get("records") if isinstance(result, dict) else None
    if not isinstance(records, list) or not records:
        raise PublicJobFetchError("adapter:empty_result")
    return records


def _fetch_via_adapter(url: str) -> FetchPublicJobPageOutput | None:
    """Adapter-first fetch for a certified company URL; None when uncovered.

    Adapter evidence is the same memory-bound shape as browsed evidence: a
    JSON document of normalized records whose sha256 is the content hash, so
    ``_with_observed_page`` and the extract side treat it like any page.
    """
    if not _PUBLIC_API_ADAPTERS_ENABLED:
        return None
    company = _adapter_company_for_url(url)
    if company is None:
        return None
    package = _adapter_package()
    records = _run_company_adapter(package, url, company)
    body = json.dumps(records, ensure_ascii=False, indent=2)
    content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    first_title = records[0].get("title") if isinstance(records[0], dict) else None
    return FetchPublicJobPageOutput(
        artifact_id=f"observed:{content_hash}",
        source_url=url,
        title=first_title if isinstance(first_title, str) else None,
        visible_text=body,
        content_hash=content_hash,
    )


def _fetch_wechat_article_page(
    context: ToolContext, url: str
) -> FetchPublicJobPageOutput | None:
    """OCR-route a WeChat article URL; None when the host is not WeChat.

    Mirrors the adapter-first contract for the WeChat channel: the OCR slice
    is authoritative for ``mp.weixin.qq.com`` pages, so a gated-off channel
    is a hard ``wechat_ocr_disabled`` error, never a silent fallthrough to
    the empty-page path. A slice run that produced no text is
    ``wechat_ocr_failed``; usable text becomes the same memory-bound page
    evidence shape as browsed pages (sha256 content hash, ``observed:`` id).
    """
    host = (urlsplit(url).hostname or "").lower()
    if host != _WECHAT_ARTICLE_HOST:
        return None
    from backend.app.services.career_skills import wechat as wechat_skill

    result = wechat_skill.fetch_wechat_article(
        context, wechat_skill.FetchWechatArticleInput(url=url)
    )
    if result.status == "needs_manual_review" and result.reason == "ocr_disabled":
        raise PublicJobFetchError("wechat_ocr_disabled")
    if not result.content_hash or not result.visible_text:
        raise PublicJobFetchError(
            "wechat_ocr_failed",
            message="该微信链接抓取失败（镜像验证墙/付费墙或无正文）仅代表此 URL 本身不可用，不代表同批其他微信链接也会失败；同批其余 URL 仍应继续逐一尝试。",
        )
    return FetchPublicJobPageOutput(
        artifact_id=result.artifact_id,
        source_url=url,
        title=None,
        visible_text=result.visible_text,
        content_hash=result.content_hash,
    )


def fetch_public_job_page(
    context: ToolContext, payload: FetchPublicJobPageInput
) -> FetchPublicJobPageOutput:
    """Fetch public evidence and expose immutable visible-text evidence to Executor.

    A WeChat article URL (``mp.weixin.qq.com``) is OCR-routed first: its body
    is image content the generic chain reads as an empty shell. A URL covered
    by a certified A1 adapter (moka/beisen/didi/netease/baidu) is fetched
    adapter-first when the channel is enabled: the adapter is the
    authoritative channel for its hosts, so a covered URL that fails is a
    hard ``adapter:<code>`` blocked error, never a silent fallthrough.
    Uncovered URLs take the plain ``requests`` fast path; when that fails or
    returns a shell with no usable text (SPA / login wall), the fetch falls
    back to a headless-Chromium render of the same URL -- still under the
    original public-URL validation. The fallbacks are gated by runtime flags
    so unit suites stay deterministic.
    """
    _assert_public_url(payload.url)
    wechat_page = _fetch_wechat_article_page(context, payload.url)
    if wechat_page is not None:
        return wechat_page
    adapter_page = _fetch_via_adapter(payload.url)
    if adapter_page is not None:
        return adapter_page
    try:
        return _fetch_public_page_requests(payload.url)
    except PublicJobFetchError as error:
        if error.code not in _PLAYWRIGHT_FALLBACK_CODES or not _PLAYWRIGHT_FALLBACK_ENABLED:
            raise
    rendered_text, rendered_title = _render_with_playwright(payload.url)
    visible_text = rendered_text.strip()[:_MAX_VISIBLE_TEXT_CHARS]
    if not visible_text:
        raise PublicJobFetchError("empty_public_page")
    if len(visible_text) < _MIN_USABLE_TEXT_CHARS:
        raise PublicJobFetchError("public_page_content_insufficient")
    rendered_bytes = visible_text.encode("utf-8", errors="replace")
    return FetchPublicJobPageOutput(
        artifact_id=f"observed:{hashlib.sha256(rendered_bytes).hexdigest()}",
        source_url=payload.url,
        title=rendered_title,
        visible_text=visible_text,
        content_hash=hashlib.sha256(rendered_bytes).hexdigest(),
    )


def _fetch_public_page_requests_with_html(
    url: str,
) -> tuple[FetchPublicJobPageOutput, str]:
    """The non-rendered evidence path: requests + visible-text normalization.

    Returns ``(page_evidence, raw_html)``; the raw HTML lets the requests
    fast path run the same card-list expansion as the render path (A2, RC-B)
    without a second fetch. Backward-compatible single-value callers keep
    using ``_fetch_public_page_requests``.
    """
    try:
        response = _fetch_validated(url)
        response.raise_for_status()
    except PublicJobFetchError:
        raise
    except requests.RequestException as exc:
        raise PublicJobFetchError("public_fetch_failed") from exc
    if response.encoding is None or response.encoding.lower() in {"iso-8859-1", "latin-1"}:
        response.encoding = response.apparent_encoding or "utf-8"
    html = response.text
    parser = _VisibleTextParser()
    parser.feed(html)
    visible_text = "\n".join(parser.text_parts)[:_MAX_VISIBLE_TEXT_CHARS]
    if not visible_text:
        raise PublicJobFetchError("empty_public_page")
    title = " ".join(parser.title_parts) or None
    if len(visible_text) < _MIN_USABLE_TEXT_CHARS:
        raise PublicJobFetchError("public_page_content_insufficient")
    return FetchPublicJobPageOutput(
        artifact_id=f"observed:{hashlib.sha256(html.encode('utf-8', errors='replace')).hexdigest()}",
        source_url=url,
        title=title,
        visible_text=visible_text,
        content_hash=hashlib.sha256(html.encode("utf-8", errors="replace")).hexdigest(),
    ), html


def _fetch_public_page_requests(url: str) -> FetchPublicJobPageOutput:
    """Backward-compatible wrapper: the requests fast path, evidence only."""
    page, _html = _fetch_public_page_requests_with_html(url)
    return page


def _collect_page_links(page: Any, origin_url: str) -> list[str]:
    """Same-host job-shaped ``<a href>`` targets from a rendered DOM.

    Only http(s) targets that pass the public-URL checks and share the
    origin's hostname are kept, so list-page expansion never follows a
    cross-host redirect ladder or a private/cloud-metadata address.  The
    path filter reuses the search-result URL tokens (career/job/position/
    campus/...), which match the detail-route shapes of the campus-portal
    SPA family the expansion targets.
    """
    try:
        raw = page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
    except Exception:
        return []
    origin_host = urlsplit(origin_url).hostname
    seen: set[str] = set()
    links: list[str] = []
    for href in raw:
        if not isinstance(href, str) or not href.startswith(("http://", "https://")):
            continue
        if not _is_public_url(href):
            continue
        if urlsplit(href).hostname != origin_host:
            continue
        path = urlsplit(href).path.lower()
        if not any(token in path for token in _JOB_RESULT_URL_TOKENS):
            continue
        if href in seen:
            continue
        seen.add(href)
        links.append(href)
    return links


class _HtmlLinkCollector(HTMLParser):
    """Same-host job-shaped ``<a href>`` targets from raw HTML.

    Mirrors the filters of ``_collect_page_links`` (rendered DOM) on the
    requests fast path: only http(s) targets that pass the public-URL checks
    and share the origin's hostname are kept, the path filter reuses the
    search-result URL tokens (career/job/position/campus/...), relative hrefs
    are resolved against the page URL with ``urljoin``, and duplicates are
    dropped. Anchors inside ``script``/``style``/``noscript`` blocks are
    skipped (mirroring ``_VisibleTextParser``), so inline JSON never leaks
    random URLs into the card-list candidate set.
    """

    _IGNORED = {"script", "style", "noscript"}

    def __init__(self, origin_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self._origin_url = origin_url
        self._origin_host = urlsplit(origin_url).hostname
        self._ignored_depth = 0
        self._seen: set[str] = set()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag in self._IGNORED:
            self._ignored_depth += 1
        if tag != "a" or self._ignored_depth:
            return
        href = dict(attrs).get("href")
        if not isinstance(href, str) or not href:
            return
        resolved = urljoin(self._origin_url, href)
        if not resolved.startswith(("http://", "https://")):
            return
        if not _is_public_url(resolved):
            return
        if urlsplit(resolved).hostname != self._origin_host:
            return
        path = urlsplit(resolved).path.lower()
        if not any(token in path for token in _JOB_RESULT_URL_TOKENS):
            return
        if resolved in self._seen:
            return
        self._seen.add(resolved)
        self.links.append(resolved)

    def handle_endtag(self, tag: str) -> None:
        if tag in self._IGNORED and self._ignored_depth:
            self._ignored_depth -= 1


def _expand_from_list_links(
    url: str, links: list[str], list_body: str
) -> list[FetchPublicJobPageOutput]:
    """Deep-fetch detail pages behind a JS card-list, one evidence page each.

    A page is treated as a card-list only when it exposes enough same-host
    job-shaped links while carrying no JD-section text of its own (the
    campus-portal SPA family).  The JD-marker scan is head-positioned (A3):
    only the first ``_JD_MARKER_SCAN_HEAD_CHARS`` characters of the body are
    checked, because detail pages place their markers near the top while
    card shells carry them only in footer SEO text.  Detail fetches reuse
    the requests fast path with the render fallback, never recurse into
    expansion again, and fail silently per-link: the list page itself stays
    valid evidence.
    """
    if len(links) < _MIN_LIST_LINKS:
        return []
    if any(
        marker.lower() in list_body[:_JD_MARKER_SCAN_HEAD_CHARS].lower()
        for marker in _JD_SECTION_MARKERS
    ):
        return []
    pages: list[FetchPublicJobPageOutput] = []
    for link in links[:_MAX_LIST_EXPANSION]:
        try:
            pages.append(_fetch_public_page_requests(link))
            continue
        except PublicJobFetchError as exc:
            if exc.code not in _PLAYWRIGHT_FALLBACK_CODES or not _PLAYWRIGHT_FALLBACK_ENABLED:
                continue
        try:
            body_text, title = _render_with_playwright(link)  # no collect_links -> no recursion
        except PublicJobFetchError:
            continue
        visible_text = body_text.strip()[:_MAX_VISIBLE_TEXT_CHARS]
        if not visible_text or len(visible_text) < _MIN_USABLE_TEXT_CHARS:
            continue
        rendered_bytes = visible_text.encode("utf-8", errors="replace")
        pages.append(
            FetchPublicJobPageOutput(
                artifact_id=f"observed:{hashlib.sha256(rendered_bytes).hexdigest()}",
                source_url=link,
                title=title,
                visible_text=visible_text,
                content_hash=hashlib.sha256(rendered_bytes).hexdigest(),
            )
        )
    return pages


def _fetch_one_with_expansion(
    context: ToolContext, url: str
) -> list[FetchPublicJobPageOutput]:
    """Fetch one URL with P2 list-page expansion, returning 1..N evidence pages.

    WeChat and adapter routes keep their single-page semantics (their bodies
    are already the terminal evidence).  Both the ``requests`` fast path and
    the render fallback can expand: the page is checked for card-list shape,
    and when it qualifies the detail pages behind its same-host job-shaped
    links are deep-fetched and appended after the list page itself.  On the
    requests path the raw HTML is scanned once for anchors (A2, RC-B), so
    server-rendered card lists (e.g. liepin) expand deterministically without
    a browser; the render path collects links from the rendered DOM.
    """
    wechat_page = _fetch_wechat_article_page(context, url)
    if wechat_page is not None:
        return [wechat_page]
    adapter_page = _fetch_via_adapter(url)
    if adapter_page is not None:
        return [adapter_page]
    try:
        page, raw_html = _fetch_public_page_requests_with_html(url)
    except PublicJobFetchError as error:
        if error.code not in _PLAYWRIGHT_FALLBACK_CODES or not _PLAYWRIGHT_FALLBACK_ENABLED:
            raise
    else:
        collector = _HtmlLinkCollector(url)
        collector.feed(raw_html)
        return [page, *_expand_from_list_links(url, collector.links, page.visible_text)]
    rendered_text, rendered_title, links = _render_with_playwright(
        url, collect_links=True
    )
    visible_text = rendered_text.strip()[:_MAX_VISIBLE_TEXT_CHARS]
    if not visible_text:
        raise PublicJobFetchError("empty_public_page")
    if len(visible_text) < _MIN_USABLE_TEXT_CHARS:
        raise PublicJobFetchError("public_page_content_insufficient")
    rendered_bytes = visible_text.encode("utf-8", errors="replace")
    list_page = FetchPublicJobPageOutput(
        artifact_id=f"observed:{hashlib.sha256(rendered_bytes).hexdigest()}",
        source_url=url,
        title=rendered_title,
        visible_text=visible_text,
        content_hash=hashlib.sha256(rendered_bytes).hexdigest(),
    )
    return [list_page, *_expand_from_list_links(url, links, visible_text)]


def fetch_public_job_pages(
    context: ToolContext, payload: FetchPublicJobPagesInput
) -> FetchPublicJobPagesOutput:
    """Capture a bounded candidate set without hiding individual public-page errors.

    Fetches run with bounded concurrency (C5): deterministic input-index
    ordering, i/n progress lines, and per-item error isolation identical to
    the sequential loop this replaced.  A rendered card-list page (JS SPA,
    no JD body, job-shaped detail links) is expanded in place (P2): the list
    page stays first in ``pages`` and up to ``_MAX_LIST_EXPANSION`` detail
    pages follow it, so later extract/match tools see real JD body.
    """
    pages: list[FetchPublicJobPageOutput] = []
    failures: list[PublicJobPageFetchFailure] = []
    batch = run_parallel_with_progress(
        payload.urls,
        lambda url: _fetch_one_with_expansion(context, url),
        label="url",
        key=lambda url: url,
    )
    for result in batch:
        if result.error is not None:
            error = result.error
            code = error.code if isinstance(error, PublicJobFetchError) else "public_fetch_failed"
            failures.append(PublicJobPageFetchFailure(source_url=result.item, error_code=code))
        elif result.value is not None:
            pages.extend(result.value)
    return FetchPublicJobPagesOutput(pages=pages, failures=failures)


def search_public_job_pages(
    context: ToolContext, payload: SearchPublicJobPagesInput
) -> SearchPublicJobPagesOutput:
    """Search a fixed public provider and return only direct, safe career URLs.

    The query is qualified with recruiting-domain ``site:`` operators unless it
    already steers with one, and results are filtered by the recruiting-host
    whitelist (unknown hosts need a job-shaped URL path, not just JD wording in
    the title), so tutorial/encyclopedia/official-homepage noise (P2/B4) never
    becomes discovery evidence. The 360 fallback runs the raw query: it stays
    the unconstrained escape hatch, with the result filter still applied.
    """
    del context
    query = payload.query
    if "site:" not in query:
        query = query + " " + " OR ".join(_JOB_SEARCH_SITE_OPERATORS)
    search_parameters = {
        "q": query,
        "mkt": "zh-CN",
        "setlang": "zh-hans",
        "cc": "CN",
    }
    source_url = "https://www.bing.com/search?" + urlencode(search_parameters)
    try:
        response = requests.get(
            source_url,
            timeout=20,
            headers={"User-Agent": "CareerAssistantPEV/1.0 (+public-job-search)"},
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise PublicJobFetchError("public_search_failed") from exc
    html = response.text
    parser = _BingSearchResultParser()
    parser.feed(html)
    results: list[PublicJobSearchResult] = []
    seen_urls: set[str] = set()
    def add_result(raw_result: dict[str, str], result_url: str | None) -> None:
        if result_url is None:
            return
        parsed = urlsplit(result_url)
        if (
            result_url in seen_urls
            or parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.hostname.endswith("bing.com")
            or not _is_plausible_public_job_result(raw_result, result_url, parsed.hostname)
        ):
            return
        try:
            _assert_public_url(result_url)
        except PublicJobFetchError:
            return
        seen_urls.add(result_url)
        results.append(PublicJobSearchResult(
            title=raw_result["title"],
            url=result_url,
            snippet=raw_result.get("snippet"),
        ))
    for raw_result in parser.results:
        add_result(raw_result, _direct_bing_result_url(raw_result["url"]))
        if len(results) >= payload.max_results:
            break
    if not results:
        fallback_source_url = "https://www.so.com/s?" + urlencode({"q": payload.query})
        try:
            fallback_response = requests.get(
                fallback_source_url,
                timeout=20,
                headers={"User-Agent": "CareerAssistantPEV/1.0 (+public-job-search)"},
            )
            fallback_response.raise_for_status()
        except requests.RequestException as exc:
            raise PublicJobFetchError("public_search_failed") from exc
        html = fallback_response.text
        source_url = fallback_source_url
        fallback_parser = _SoSearchResultParser()
        fallback_parser.feed(html)
        for raw_result in fallback_parser.results:
            add_result(raw_result, raw_result["url"])
            if len(results) >= payload.max_results:
                break
    return SearchPublicJobPagesOutput(
        query=payload.query,
        source_url=source_url,
        content_hash=hashlib.sha256(html.encode("utf-8", errors="replace")).hexdigest(),
        results=results,
    )


def _is_allowed_job_host(hostname: str) -> bool:
    """True when ``hostname`` is a known recruiting domain (P2/B4 whitelist).

    This is a result-quality filter only -- ``_assert_public_url`` remains the
    security gate. Patterns ending in "." match the first label (careers.example,
    career.hebut.edu.cn); plain domains match by suffix (www.liepin.com,
    job.ncss.cn).
    """
    lowered = hostname.lower()
    for pattern in _JOB_SEARCH_ALLOWED_HOST_PATTERNS:
        if pattern.endswith("."):
            if lowered.startswith(pattern):
                return True
        elif lowered == pattern or lowered.endswith("." + pattern):
            return True
    return False


def _is_plausible_public_job_result(
    result: dict[str, str], result_url: str, hostname: str
) -> bool:
    """Keep search evidence useful for job discovery without trusting generic pages.

    Whitelisted recruiting hosts pass on the loose URL-token OR text-signal
    check (their pages are already job-shaped). Any other host must carry a
    job token in its URL path: a tutorial or encyclopedia page rarely does,
    even though its title often mentions 招聘/岗位 -- exactly the noise this
    rejects.
    """
    searchable_text = " ".join(
        value for value in (result.get("title"), result.get("snippet"))
        if isinstance(value, str)
    )
    lowered_url = result_url.lower()
    url_token_match = any(token in lowered_url for token in _JOB_RESULT_URL_TOKENS)
    if _is_allowed_job_host(hostname):
        return url_token_match or _JOB_RESULT_TEXT_RE.search(searchable_text) is not None
    return url_token_match


def _direct_bing_result_url(url: str) -> str | None:
    """Decode Bing's documented URL-safe ``u`` redirect value before safety checks."""
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").lower()
    if not hostname.endswith("bing.com"):
        return url
    if not parsed.path.startswith("/ck/"):
        return None
    encoded = parse_qs(parsed.query).get("u", [None])[0]
    if not isinstance(encoded, str) or not encoded.startswith("a1"):
        return None
    try:
        payload = encoded[2:]
        padding = "=" * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload + padding).decode("utf-8")
    except (UnicodeDecodeError, ValueError):
        return None
    decoded_url = urlsplit(decoded)
    if decoded_url.scheme not in {"http", "https"} or not decoded_url.hostname:
        return None
    return decoded


def extract_observed_job_details(
    context: ToolContext, payload: ExtractObservedJobDetailsInput
) -> ExtractObservedJobDetailsOutput:
    """Normalize one existing public-page artifact without accepting model text."""
    evidence = _find_observed_evidence(context, payload.artifact_id)
    if evidence is None:
        raise PublicJobFetchError("observed_evidence_not_found")
    source_url = evidence.get("source_url")
    content_hash = evidence.get("content_hash")
    visible_text = evidence.get("visible_text")
    if not all(isinstance(value, str) and value for value in (source_url, content_hash, visible_text)):
        raise PublicJobFetchError("observed_evidence_incomplete")
    evidence_ref = {
        "artifact_id": payload.artifact_id,
        "source_url": source_url,
        "content_hash": content_hash,
    }
    adapter_records = _parse_adapter_evidence(visible_text)
    if adapter_records is not None:
        return _adapter_details_output(
            payload.artifact_id, source_url, content_hash, adapter_records, evidence_ref
        )
    extracted = extract_jd_candidates(visible_text, source_url)
    # A single-JD page is enriched from its own full text; a multi-candidate
    # page (e.g. a Feishu card listing) must NOT have the page's first
    # responsibilities/requirements section copied onto every candidate.
    single_jd_page = len(extracted) <= 1
    candidates = []
    for candidate in extracted:
        inferred_title = _infer_official_page_title(visible_text)
        title = (
            inferred_title
            if candidate.title in {None, "申请职位"}
            else candidate.title
        )
        locations = candidate.locations or _infer_official_page_locations(
            visible_text, title
        )
        recruitment_types = _infer_recruitment_types(
            source_url, candidate.recruitment_types
        )
        warnings = [
            warning
            for warning in candidate.normalization_warnings
            if not (locations and warning == "No location information found")
        ]
        responsibilities = (
            _extract_jd_section(
                visible_text,
                labels=("岗位职责", "工作职责", "职位描述", "工作内容", "主要职责", "岗位定位", "你将负责"),
            )
            if single_jd_page
            else candidate.responsibilities
        ) or candidate.responsibilities
        requirements = (
            _extract_jd_section(
                visible_text,
                labels=(
                    "任职要求",
                    "职责要求",
                    "岗位要求",
                    "职位要求",
                    "资格要求",
                    "招聘要求",
                ),
            )
            if single_jd_page
            else candidate.requirements
        ) or candidate.requirements
        candidates.append(
            ExtractedJobDetails(
                title=title,
                company_name=candidate.company_name,
                locations=locations,
                responsibilities=responsibilities,
                requirements=requirements,
                recruitment_types=recruitment_types,
                apply_url=candidate.apply_url,
                deadline_text=candidate.deadline_text,
                confidence=round(candidate.confidence, 4),
                evidence_refs=[evidence_ref],
                normalization_warnings=warnings,
                skills=candidate.skills,
                min_degree=candidate.min_degree,
                priority=candidate.priority,
                strength=candidate.strength,
                taxonomy=candidate.taxonomy,
            )
        )
    return ExtractObservedJobDetailsOutput(
        source_artifact_id=payload.artifact_id,
        source_url=source_url,
        content_hash=content_hash,
        candidates=candidates,
    )


def extract_observed_job_details_batch(
    context: ToolContext, payload: ExtractObservedJobDetailsBatchInput
) -> ExtractObservedJobDetailsBatchOutput:
    """Normalize a bounded observed set without letting the model supply JD text."""
    return ExtractObservedJobDetailsBatchOutput(
        details=[
            extract_observed_job_details(
                context, ExtractObservedJobDetailsInput(artifact_id=artifact_id)
            )
            for artifact_id in payload.artifact_ids
        ]
    )


_ADAPTER_RECORD_KEYS = frozenset({"title", "description", "apply_url"})


def _parse_adapter_evidence(text: str) -> list[dict[str, Any]] | None:
    """Parse adapter-record JSON evidence; None when the text is not records.

    Adapter evidence is a JSON list of normalized records. Everything else --
    including a page that merely begins with "[" -- falls back to the normal
    JD-text extraction path, so a false marker can never lose evidence.
    """
    if not text.lstrip().startswith("["):
        return None
    try:
        records = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(records, list) or not records:
        return None
    if not all(
        isinstance(record, dict) and _ADAPTER_RECORD_KEYS.issubset(record)
        for record in records
    ):
        return None
    return records


def _record_to_job_details(
    record: dict[str, Any], source_url: str, evidence_ref: dict[str, str]
) -> ExtractedJobDetails:
    """Map one normalized adapter record onto the ExtractedJobDetails shape.

    The record is structured data (title / location / description /
    apply_url), so the deterministic text heuristics are only used to split
    its description into responsibilities vs requirements; the page-level
    title/location inference never runs on JSON text.
    """
    description = record.get("description")
    description = description if isinstance(description, str) else ""
    location = record.get("location")
    locations = [location] if isinstance(location, str) and location else []
    responsibilities = (
        _extract_jd_section(
            description,
            labels=("岗位职责", "工作职责", "职位描述", "工作内容", "主要职责", "岗位定位", "你将负责"),
        )
        or description
    )
    requirements = _extract_jd_section(
        description,
        labels=("任职要求", "职责要求", "岗位要求", "职位要求", "资格要求", "招聘要求"),
    )
    title = record.get("title")
    company = record.get("company")
    apply_url = record.get("apply_url")
    deadline = record.get("deadline")
    return ExtractedJobDetails(
        title=title if isinstance(title, str) and title else None,
        company_name=company if isinstance(company, str) and company else None,
        locations=locations,
        responsibilities=responsibilities,
        requirements=requirements,
        recruitment_types=_infer_recruitment_types(source_url, []),
        apply_url=apply_url if isinstance(apply_url, str) and apply_url else None,
        deadline_text=deadline if isinstance(deadline, str) and deadline else None,
        confidence=1.0,
        evidence_refs=[evidence_ref],
        normalization_warnings=[],
    )


def _adapter_details_output(
    artifact_id: str,
    source_url: str,
    content_hash: str,
    records: list[dict[str, Any]],
    evidence_ref: dict[str, str],
) -> ExtractObservedJobDetailsOutput:
    """Normalize adapter-record evidence into one candidate per record."""
    candidates = [_record_to_job_details(record, source_url, evidence_ref) for record in records]
    return ExtractObservedJobDetailsOutput(
        source_artifact_id=artifact_id,
        source_url=source_url,
        content_hash=content_hash,
        candidates=candidates,
    )


def _find_observed_evidence(
    context: ToolContext, artifact_id: str
) -> dict[str, object] | None:
    raw_evidence = context.metadata.get("observed_public_evidence")
    if not isinstance(raw_evidence, list):
        return None
    for item in raw_evidence:
        if isinstance(item, dict) and (
            item.get("artifact_id") == artifact_id
            or f"observed:{item.get('content_hash')}" == artifact_id
        ):
            return item
    return None


def _extract_jd_section(text: str, *, labels: tuple[str, ...]) -> str:
    """Extract one Chinese JD section even when a page collapses line breaks."""
    label_pattern = "|".join(re.escape(label) for label in labels)
    stop_pattern = "|".join(
        re.escape(label)
        for label in (
            "岗位职责",
            "工作职责",
            "职位描述",
            "工作内容",
            "主要职责",
            "任职要求",
            "职责要求",
            "岗位要求",
            "职位要求",
            "资格要求",
            "招聘要求",
            "工作地点",
            "工作地址",
            "投递方式",
            "申请方式",
            "截止日期",
            "截止时间",
            "申请职位",
        )
    )
    match = re.search(
        rf"(?:{label_pattern})\s*[:：]?\s*(.*?)"
        rf"(?=(?:{stop_pattern})\s*[:：]?|$)",
        text,
        flags=re.DOTALL,
    )
    if match is None:
        return ""
    return " ".join(match.group(1).split()).strip()


def _infer_official_page_title(text: str) -> str | None:
    """Infer a title from the header area of official pages lacking a title label."""
    header = re.split(r"(?:岗位职责|工作职责|职位描述)\s*[:：]?", text, maxsplit=1)[0]
    for line in reversed(header.splitlines()):
        candidate = line.strip()
        if (
            3 <= len(candidate) <= 100
            and re.search(r"(?:工程师|开发|算法|研究员|实习生|架构师|科学家)", candidate)
            and "申请" not in candidate
        ):
            return candidate
    return None


def _infer_official_page_locations(text: str, title: str | None) -> list[str]:
    """Read a city-shaped line following a title in an official page header."""
    if not title:
        return []
    lines = [line.strip() for line in text.splitlines()]
    try:
        title_index = lines.index(title)
    except ValueError:
        return []
    for line in lines[title_index + 1 :]:
        if re.search(r"(?:岗位职责|工作职责|职位描述)", line):
            break
        if re.fullmatch(r"[\u4e00-\u9fff]{2,12}(?:市|省)?", line):
            return [line]
    return []


def _infer_recruitment_types(source_url: str, fallback: list[str]) -> list[str]:
    """Prefer an official career URL's explicit recruitment segment when present."""
    upper_url = source_url.upper()
    if "/SOCIAL/" in upper_url:
        return ["social"]
    if "/GRADUATE/" in upper_url:
        return ["campus"]
    if "/INTERN/" in upper_url:
        return ["internship"]
    return fallback
