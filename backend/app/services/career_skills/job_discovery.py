"""Evidence-first public-page tool for the PEV ``job-discovery`` Skill."""

from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Literal
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
import uuid

from pydantic import BaseModel, Field, field_validator, model_validator
import requests

from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.career_skills.target_evidence import resolve_target_evidence
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
# Soft-404 markers (W2): a page that carries one of these strings in its title
# or its first ``_JD_MARKER_SCAN_HEAD_CHARS`` body chars, with almost no JD
# body, is a dead link rather than valid evidence. Classified as ``dead_link``
# -- a neutral failure, NOT a blocked code -- so it feeds the
# search-authorization rule (search allowed only after EVERY candidate URL
# failed) without ever entering needs_manual_review.
_SOFT_404_MARKERS = ("页面不存在", "职位已下线", "职位不存在", "页面已经过期")
# At or above this many usable text chars a page is real content and the
# soft-404 markers are ignored (e.g. a listing page that mentions one offline
# role); below it, marker hits classify the page as a dead link.
_MIN_REAL_JD_TEXT_CHARS = 400
# Upper bound for the visible-text evidence captured from one page. The Feishu
# campus portal renders a whole 100-job listing (with inline JD sections) in a
# single DOM pass (~26k chars for the 61 NIO agent roles), so the cap must
# comfortably exceed the largest single-portal render while staying far under
# the 48k per-run evidence budget kept full for recent artifacts.
_MAX_VISIBLE_TEXT_CHARS = 32_000
_PLAYWRIGHT_FALLBACK_ENABLED = False
_PLAYWRIGHT_STORAGE_STATE_PATH: str | None = None
# P2 (2026-08-09): a rendered page is a JS card-list when it exposes >= this
# many same-host job-shaped detail links while carrying no JD-section text;
# the batch fetch then deep-fetches up to this many detail pages so
# match-observed-jobs sees real JD body instead of an empty card shell.
_MIN_LIST_LINKS = 2
_MAX_LIST_EXPANSION = 5
_CAMPUS_PORTAL_HOST = "career.hebut.edu.cn"
_MAX_CAMPUS_INDEX_PAGES = 2
_MAX_CAMPUS_DETAIL_PAGES = 20
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
_RENDER_METADATA = threading.local()


def _prioritize_detail_links(links: list[str]) -> list[str]:
    """Put likely detail routes before navigation links from the same page.

    Campus portals commonly expose navigation links (``/index.html``,
    ``/recruitment/index.html``) before the actual job links in their HTML.
    Expansion is intentionally bounded, so consuming the cap on navigation
    pages silently loses the public JD evidence that follows them.  The
    existing URL safety and same-host filters remain authoritative; this only
    changes the order of already-accepted links and preserves stable order for
    links with the same score.
    """
    detail_route = re.compile(
        r"/(?:content|detail|position|job|jobs|post)(?:/|$)", re.IGNORECASE
    )
    detail_query = re.compile(
        r"(?:^|_)(?:id|job[_-]?id|position[_-]?id|post[_-]?id)=",
        re.IGNORECASE,
    )

    def score(url: str) -> int:
        parsed = urlsplit(url)
        path = parsed.path.rstrip("/").lower()
        basename = path.rsplit("/", 1)[-1]
        value = 0
        if detail_route.search(path):
            value += 4
        if detail_query.search(parsed.query):
            value += 2
        if re.search(r"/(?:id|position|post)/[^/]+$", path):
            value += 1
        if basename in {"index", "index.html", "list", "search", "careers"}:
            value -= 3
        return value

    return [
        url
        for _, url in sorted(
            enumerate(links), key=lambda item: (-score(item[1]), item[0])
        )
    ]


def _prioritize_direct_search_results(
    results: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Rank direct detail results ahead of list/search shells, stably."""
    urls = [item["url"] for item in results if isinstance(item.get("url"), str)]
    ordered_urls = _prioritize_detail_links(urls)
    ranks = {url: index for index, url in enumerate(ordered_urls)}
    return [
        item
        for _index, item in sorted(
            enumerate(results),
            key=lambda pair: (
                ranks.get(str(pair[1].get("url")), len(ranks)),
                pair[0],
            ),
        )
    ]


def _render_metadata(url: str) -> tuple[str, int | None]:
    """Read the most recent render's final URL/status without changing tuple APIs."""
    metadata = getattr(_RENDER_METADATA, "value", None)
    if metadata is None:
        return url, 200
    return metadata


def _set_render_metadata(url: str, status_code: int | None) -> None:
    _RENDER_METADATA.value = (url, status_code)


def _playwright_worker_command(url: str, *, collect_links: bool) -> list[str]:
    """Build the isolated render-worker command without shell interpolation."""
    command = [
        sys.executable,
        "-m",
        "backend.app.services.career_skills.playwright_worker",
        "--url",
        url,
    ]
    if collect_links:
        command.append("--collect-links")
    if _PLAYWRIGHT_STORAGE_STATE_PATH:
        command.extend(["--storage-state", _PLAYWRIGHT_STORAGE_STATE_PATH])
    return command


def _terminate_process_tree(pid: int) -> None:
    """Terminate only the owned render worker and its descendants."""
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            check=False,
            capture_output=True,
        )
        return
    try:
        import os
        import signal

        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        try:
            import os

            os.kill(pid, 9)
        except (ProcessLookupError, OSError):
            pass


def _render_with_playwright_process(
    url: str, *, collect_links: bool
) -> tuple[str, str | None] | tuple[str, str | None, list[str]]:
    """Render in a killable child process so Chromium cannot become an orphan."""
    _assert_public_url(url)
    kwargs: dict[str, Any] = {
        "cwd": str(Path(__file__).resolve().parents[4]),
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "env": {**__import__("os").environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    process = subprocess.Popen(
        _playwright_worker_command(url, collect_links=collect_links), **kwargs
    )
    try:
        stdout, _stderr = process.communicate(timeout=_RENDER_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        _terminate_process_tree(process.pid)
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
        raise PublicJobFetchError("public_fetch_failed") from None
    if process.returncode != 0:
        raise PublicJobFetchError("public_fetch_failed") from None
    try:
        payload = json.loads((stdout or "").strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        raise PublicJobFetchError("public_fetch_failed") from None
    if payload.get("error"):
        raise PublicJobFetchError(
            str(payload["error"]),
            effective_url=payload.get("effective_url")
            if isinstance(payload.get("effective_url"), str)
            else url,
            status_code=payload.get("status_code")
            if isinstance(payload.get("status_code"), int)
            else None,
        )
    body = payload.get("body")
    title = payload.get("title")
    effective_url = payload.get("effective_url")
    status_code = payload.get("status_code")
    _set_render_metadata(
        effective_url if isinstance(effective_url, str) else url,
        status_code if isinstance(status_code, int) else 200,
    )
    if not isinstance(body, str):
        raise PublicJobFetchError("public_fetch_failed")
    if collect_links:
        links = payload.get("links")
        return body, title if isinstance(title, str) else None, (
            links if isinstance(links, list) else []
        )
    return body, title if isinstance(title, str) else None


def enable_playwright_fallback(enabled: bool) -> None:
    """Toggle the rendered-fetch fallback (called from runtime assembly)."""
    global _PLAYWRIGHT_FALLBACK_ENABLED
    _PLAYWRIGHT_FALLBACK_ENABLED = enabled


def configure_playwright_storage_state(path: str | None) -> None:
    """Configure an operator-provisioned, read-only browser storage state."""
    global _PLAYWRIGHT_STORAGE_STATE_PATH
    _PLAYWRIGHT_STORAGE_STATE_PATH = path.strip() if isinstance(path, str) and path.strip() else None


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
    "career.",
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
    "site:talent.baidu.com",
    "site:jobs.bytedance.com",
    "site:careers.tencent.com",
    "site:campus.tencent.com",
    "site:career.hebut.edu.cn",
    "site:fenbi.com",
    "site:juejin.cn",
    "site:liepin.com",
    "site:iguopin.com",
    "site:zhaopin.com",
    "site:shixiseng.com",
    "site:lagou.com",
    "site:job.ncss.cn",
)
_MAX_PUBLIC_SEARCH_ROUTES = 3
_JUEJIN_SEARCH_API_URL = "https://api.juejin.cn/search_api/v1/search"
_JUEJIN_RECENT_SEARCH_QUERIES = ("招聘", "内推", "校招")
_JUEJIN_MAX_SEARCH_PAGES_PER_QUERY = 8


class PublicJobFetchError(RuntimeError):
    """Stable, non-sensitive public-web fetch failure.

    ``message`` (optional) enriches the agent-facing error text while
    ``code`` stays the stable, machine-testable identity.  When omitted the
    error text falls back to the code itself.
    """

    def __init__(
        self,
        code: str,
        message: str | None = None,
        *,
        effective_url: str | None = None,
        redirect_chain: list[str] | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(code if not message else f"{code}: {message}")
        self.code = code
        self.effective_url = effective_url
        self.redirect_chain = list(redirect_chain or [])
        self.status_code = status_code


@dataclass(frozen=True)
class HttpFetchResult:
    """Validated HTTP response plus redirect provenance.

    ``__getattr__`` keeps the old internal response-shaped contract working
    for callers that only need ``status_code``, ``content`` or
    ``raise_for_status`` while making the provenance explicit to new code.
    """

    response: Any
    requested_url: str
    effective_url: str
    redirect_chain: list[str]

    def __getattr__(self, name: str) -> Any:
        return getattr(self.response, name)


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
    effective_url: str | None = None
    redirect_chain: list[str] = Field(default_factory=list)
    http_status: int | None = None
    # Evidence quality is intentionally explicit so downstream Skills can
    # distinguish a usable JD from a list shell without parsing prose.
    quality: Literal["jd_complete", "list_only", "js_shell", "empty"] | None = None
    quality_signal: str | None = None

    @model_validator(mode="after")
    def classify_quality(self) -> "FetchPublicJobPageOutput":
        if self.quality is not None:
            return self
        quality, signal = _classify_page_quality(self.visible_text)
        object.__setattr__(self, "quality", quality)
        object.__setattr__(self, "quality_signal", signal)
        return self


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
    effective_url: str | None = None
    redirect_chain: list[str] = Field(default_factory=list)
    http_status: int | None = None
    message: str | None = None


class FetchPublicJobPagesOutput(BaseModel):
    """All successfully captured pages plus explicit failures from one bounded batch."""

    pages: list[FetchPublicJobPageOutput]
    failures: list[PublicJobPageFetchFailure] = Field(default_factory=list)


_IGUOPIN_LIST_HOSTS = frozenset({"iguopin.com", "www.iguopin.com"})
_IGUOPIN_API_ORIGIN = "https://gp-api.iguopin.com"
_IGUOPIN_RECOMMEND_PATH = "/api/jobs/v1/recom-job"
_IGUOPIN_DETAIL_PATH = "/api/jobs/v3/info"
_TENCENT_CAREERS_HOST = "careers.tencent.com"
_TENCENT_QUERY_PATH = "/tencentcareer/api/post/query"


def _iguopin_list_detail_urls(url: str) -> list[str]:
    """Derive public detail-page routes from IGUOPIN's anonymous list IDs.

    Only the opaque record ID is consumed. Titles, dates, requirements, and
    every other API field are deliberately ignored: the resulting public web
    pages must still be fetched and hashed before they can become evidence.
    """
    parsed = urlsplit(url)
    if (
        (parsed.hostname or "").lower() not in _IGUOPIN_LIST_HOSTS
        or not parsed.path.lower().startswith("/job")
    ):
        return []
    keyword = parse_qs(parsed.query).get("keyword", [""])[0].strip()
    if not keyword:
        return []
    recommend_url = _IGUOPIN_API_ORIGIN + _IGUOPIN_RECOMMEND_PATH
    _assert_public_url(recommend_url)
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://www.iguopin.com",
        "Referer": "https://www.iguopin.com/",
        "User-Agent": _PUBLIC_FETCH_HEADERS["User-Agent"],
    }
    try:
        response = requests.post(
            recommend_url,
            json={
                "search": {"page": 1, "page_size": 20, "keyword": keyword},
                "recom": {
                    "update_time": True,
                    "company_nature": True,
                    "hot_job": True,
                },
            },
            timeout=20,
            allow_redirects=False,
            headers=headers,
        )
    except requests.RequestException:
        return []
    if response.status_code in {401, 403}:
        raise PublicJobFetchError(
            "iguopin_detail_api_denied", status_code=response.status_code
        )
    try:
        envelope = response.json()
    except ValueError:
        return []
    if isinstance(envelope, dict) and str(envelope.get("code")) in {"401", "403"}:
        raise PublicJobFetchError(
            "iguopin_detail_api_denied", status_code=int(envelope["code"])
        )
    records = envelope.get("data") if isinstance(envelope, dict) else None
    if isinstance(records, dict):
        records = records.get("list") or records.get("records") or records.get("rows")
    if not isinstance(records, list):
        return []
    urls: list[str] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        job_id = record.get("job_id") or record.get("id")
        if job_id is None:
            continue
        detail_url = "https://www.iguopin.com/job/detail?" + urlencode(
            {"id": str(job_id)}
        )
        _assert_public_url(detail_url)
        if detail_url not in urls:
            urls.append(detail_url)
    return urls


def _tencent_query_detail_urls(url: str, response_body: str) -> list[str]:
    """Derive same-origin Tencent detail routes from public query record IDs."""
    parsed = urlsplit(url)
    if (
        (parsed.hostname or "").lower() != _TENCENT_CAREERS_HOST
        or parsed.path.lower() != _TENCENT_QUERY_PATH
    ):
        return []
    try:
        envelope = json.loads(response_body)
    except (TypeError, json.JSONDecodeError):
        return []
    data = envelope.get("Data") if isinstance(envelope, dict) else None
    records = data.get("Posts") if isinstance(data, dict) else None
    if not isinstance(records, list):
        return []
    urls: list[str] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        post_id = str(record.get("PostId") or "")
        if not re.fullmatch(r"\d{6,32}", post_id):
            continue
        detail_url = (
            "https://careers.tencent.com/jobdesc.html?"
            + urlencode({"postId": post_id})
        )
        _assert_public_url(detail_url)
        if detail_url not in urls:
            urls.append(detail_url)
    return urls


def _probe_iguopin_detail_access(url: str, page: FetchPublicJobPageOutput) -> PublicJobFetchError | None:
    """Probe only the anonymous public detail boundary for an IGUOPIN list.

    IGUOPIN renders public job cards but its documented browser requests can
    return an authorization response for the detail endpoint.  We do not use
    the API payload as job evidence and never add credentials or retry a 401/
    403.  The probe exists solely to turn a card-only page plus a confirmed
    detail denial into an auditable external block instead of a duplicate-call
    stall.
    """
    if page.quality != "list_only":
        return None
    parsed = urlsplit(url)
    if (parsed.hostname or "").lower() not in _IGUOPIN_LIST_HOSTS:
        return None
    if not parsed.path.lower().startswith("/job"):
        return None
    keyword = parse_qs(parsed.query).get("keyword", [""])[0].strip()
    if not keyword:
        return None

    recommend_url = _IGUOPIN_API_ORIGIN + _IGUOPIN_RECOMMEND_PATH
    detail_url = _IGUOPIN_API_ORIGIN + _IGUOPIN_DETAIL_PATH
    _assert_public_url(recommend_url)
    _assert_public_url(detail_url)
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://www.iguopin.com",
        "Referer": "https://www.iguopin.com/",
        "User-Agent": _PUBLIC_FETCH_HEADERS["User-Agent"],
    }
    try:
        response = requests.post(
            recommend_url,
            json={
                "search": {"page": 1, "page_size": 20, "keyword": keyword},
                "recom": {
                    "update_time": True,
                    "company_nature": True,
                    "hot_job": True,
                },
            },
            timeout=20,
            allow_redirects=False,
            headers=headers,
        )
    except requests.RequestException:
        return None
    if response.status_code in {401, 403}:
        return PublicJobFetchError(
            "iguopin_detail_api_denied", status_code=response.status_code
        )
    try:
        envelope = response.json()
    except ValueError:
        return None
    if isinstance(envelope, dict) and str(envelope.get("code")) in {"401", "403"}:
        return PublicJobFetchError(
            "iguopin_detail_api_denied", status_code=int(envelope["code"])
        )
    records = envelope.get("data") if isinstance(envelope, dict) else None
    if isinstance(records, dict):
        records = records.get("list") or records.get("records") or records.get("rows")
    if not isinstance(records, list):
        return None
    job_id = next(
        (
            record.get("job_id") or record.get("id")
            for record in records
            if isinstance(record, dict)
            and (record.get("job_id") or record.get("id"))
        ),
        None,
    )
    if job_id is None:
        return None
    try:
        detail_response = requests.get(
            detail_url,
            params={"job_id": str(job_id)},
            timeout=20,
            allow_redirects=False,
            headers=headers,
        )
    except requests.RequestException:
        return None
    if detail_response.status_code in {401, 403}:
        return PublicJobFetchError(
            "iguopin_detail_api_denied", status_code=detail_response.status_code
        )
    try:
        detail_envelope = detail_response.json()
    except ValueError:
        return None
    if isinstance(detail_envelope, dict) and str(detail_envelope.get("code")) in {"401", "403"}:
        return PublicJobFetchError(
            "iguopin_detail_api_denied", status_code=int(detail_envelope["code"])
        )
    return None


def _persist_fetch_failure(
    failure_sink: list[PublicJobPageFetchFailure],
    failure_lock: threading.Lock | None,
    *,
    source_url: str,
    error: PublicJobFetchError,
) -> None:
    failure = PublicJobPageFetchFailure(
        source_url=source_url,
        error_code=error.code,
        effective_url=error.effective_url,
        redirect_chain=error.redirect_chain,
        http_status=error.status_code,
        message=str(error),
    )
    if failure_lock is None:
        failure_sink.append(failure)
    else:
        with failure_lock:
            failure_sink.append(failure)


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


class PublicCommunityScanRecord(PublicJobSearchResult):
    """One timestamped record inspected during an official community scan."""

    published_at: str


class SearchPublicJobPagesOutput(BaseModel):
    """Search evidence that lets the Executor choose a public page to inspect next."""

    query: str
    source_url: str
    content_hash: str
    results: list[PublicJobSearchResult]
    terminal_reason: Literal["candidates_found", "search_empty"] = "candidates_found"
    provider: Literal["public_web_search", "juejin_official_search"] = (
        "public_web_search"
    )
    source_scope: str | None = None
    time_window_days: int | None = Field(default=None, ge=1, le=365)
    coverage_complete: bool = False
    scanned_result_count: int = Field(default=0, ge=0)
    matched_result_count: int = Field(default=0, ge=0)
    scan_queries: list[str] = Field(default_factory=list)
    scan_evidence: list[PublicCommunityScanRecord] = Field(default_factory=list)


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
    published_at: str | None = None
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
    source_quality: Literal["jd_complete", "list_only", "js_shell", "empty"] | None = None
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


class _SogouMobileSearchResultParser(HTMLParser):
    """Read direct targets embedded in Sogou mobile result-wrapper URLs."""

    def __init__(self, source_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self._source_url = source_url
        self.results: list[dict[str, str]] = []
        self._url: str | None = None
        self._title_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag != "a" or self._url is not None:
            return
        href = dict(attrs).get("href")
        if not isinstance(href, str) or not href:
            return
        wrapper = urlsplit(urljoin(self._source_url, href))
        direct_url = parse_qs(wrapper.query).get("url", [None])[0]
        if isinstance(direct_url, str) and direct_url.startswith(("http://", "https://")):
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


_ACCESS_BLOCK_TEXT_MARKERS = (
    "安全验证",
    "验证码",
    "访问验证",
    "captcha",
    "verify you are human",
    "security center",
    "安全中心",
    "人机验证",
)


def _classify_page_quality(visible_text: str) -> tuple[
    Literal["jd_complete", "list_only", "js_shell", "empty"], str
]:
    """Classify captured text without treating a card shell as a JD.

    This is a routing signal, not a completion gate: the deterministic
    extraction and evidence contracts still decide whether a Skill succeeded.
    """
    normalized = re.sub(r"\s+", "", visible_text or "")
    if not normalized:
        return "empty", "no_visible_text"
    if len(normalized) < _MIN_USABLE_TEXT_CHARS:
        return "js_shell", f"visible_chars<{_MIN_USABLE_TEXT_CHARS}"
    head = normalized[:_JD_MARKER_SCAN_HEAD_CHARS].casefold()
    jd_markers = {
        *(_marker.replace(" ", "").casefold() for _marker in _JD_SECTION_MARKERS),
        "requirements",
        "qualifications",
        "jobresponsibilities",
        "whatyouwilldo",
    }
    if any(marker in head for marker in jd_markers):
        return "jd_complete", "jd_section_marker"
    # Some official career portals render the full JD bodies inline below a
    # long navigation header. The old head-only rule classified those pages as
    # list_only even though the visible evidence already contained repeated
    # responsibilities/requirements sections. Treat that bounded, explicit
    # inline-JD shape as complete; it remains source-backed and extraction
    # still decides which candidate rows are usable.
    inline_markers = (
        "职位描述",
        "工作职责",
        "岗位职责",
        "任职要求",
        "希望你是",
    )
    inline_marker_count = sum(normalized.count(marker) for marker in inline_markers)
    if len(normalized) >= _MIN_REAL_JD_TEXT_CHARS and inline_marker_count >= 2:
        return "jd_complete", "inline_jd_sections"
    if any(
        marker in head
        for marker in ("职位列表", "岗位列表", "招聘职位", "校招职位", "joblist", "jobcards")
    ):
        return "list_only", "list_marker_without_jd_section"
    return "list_only", "usable_text_without_jd_section"


def _detect_access_block(
    *,
    effective_url: str,
    title: str | None,
    visible_text: str,
    status_code: int | None,
) -> str | None:
    """Classify access gates before generic empty/short-page heuristics."""
    parsed = urlsplit(effective_url)
    host = (parsed.hostname or "").lower()
    path = parsed.path.lower()
    if host == "safe.liepin.com" and (
        "captcha" in path or "security" in path or "verify" in path
    ):
        return "anti_bot_challenge"
    if host == "wow.liepin.com" and "transit" in path:
        return "anti_bot_challenge"
    text = f"{title or ''}\n{visible_text[:2_000]}".lower()
    if any(marker.lower() in text for marker in _ACCESS_BLOCK_TEXT_MARKERS):
        return "anti_bot_challenge"
    if status_code == 429:
        return "rate_limited"
    if status_code in {401, 403}:
        return "access_denied"
    return None


def _domain_scope(url: str) -> str:
    """Return the run-level circuit key without merging unrelated registries."""
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    if host == "liepin.com" or host.endswith(".liepin.com"):
        return "liepin.com"
    return host


def _blocked_domains(context: ToolContext) -> set[str]:
    raw = context.metadata.get("blocked_public_domains", [])
    if not isinstance(raw, list):
        return set()
    return {item for item in raw if isinstance(item, str) and item}


def _ensure_domain_available(context: ToolContext, url: str) -> None:
    domain = _domain_scope(url)
    if domain in _blocked_domains(context):
        raise PublicJobFetchError(
            "domain_temporarily_blocked",
            message=f"当前运行已暂停访问 {domain}：该域名此前返回了反爬或访问阻断。",
            effective_url=url,
        )


def _remember_blocked_domain(
    context: ToolContext, url: str, error: PublicJobFetchError
) -> None:
    if error.code not in {"anti_bot_challenge", "access_denied"}:
        return
    domains = _blocked_domains(context)
    domains.add(_domain_scope(url))
    context.metadata["blocked_public_domains"] = sorted(domains)
    context.metadata.setdefault("blocked_public_domain_reasons", {})[
        _domain_scope(url)
    ] = error.code


def _fetch_validated(url: str) -> HttpFetchResult:
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
    redirect_chain = [url]
    last_status: int | None = None
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
                last_status = getattr(response, "status_code", None)
                if not response.is_redirect:
                    return HttpFetchResult(
                        response=response,
                        requested_url=url,
                        effective_url=current,
                        redirect_chain=list(redirect_chain),
                    )
                target = response.headers.get("Location")
                if not target:
                    return HttpFetchResult(
                        response=response,
                        requested_url=url,
                        effective_url=current,
                        redirect_chain=list(redirect_chain),
                    )
                target = urljoin(current, target)
                try:
                    _assert_public_url(target)
                except PublicJobFetchError as exc:
                    raise PublicJobFetchError(
                        exc.code,
                        str(exc),
                        effective_url=target,
                        redirect_chain=[*redirect_chain, target],
                        status_code=last_status,
                    ) from exc
                current = target
                redirect_chain.append(current)
            raise PublicJobFetchError(
                "unsafe_public_url",
                effective_url=current,
                redirect_chain=redirect_chain,
                status_code=last_status,
            )
        except PublicJobFetchError as exc:
            if not exc.effective_url:
                raise PublicJobFetchError(
                    exc.code,
                    str(exc),
                    effective_url=current,
                    redirect_chain=redirect_chain,
                    status_code=last_status,
                ) from exc
            raise
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
        _set_render_metadata(url, 200)
        body, title = rendered[:2]
        if collect_links:
            return body, title, list(rendered[2]) if len(rendered) >= 3 else []
        return body, title
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise PublicJobFetchError("public_fetch_failed") from None

    # Real Playwright runs in an isolated one-shot process. Unit tests can
    # still inject the sync API seam; injected fakes remain in-process so the
    # existing deterministic browser contract tests do not need a real browser.
    if getattr(sync_playwright, "__module__", "").startswith("playwright"):
        with _RENDER_LOCK:
            return _render_with_playwright_process(url, collect_links=collect_links)

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
    _ensure_domain_available(context, payload.url)
    wechat_page = _fetch_wechat_article_page(context, payload.url)
    if wechat_page is not None:
        return wechat_page
    adapter_page = _fetch_via_adapter(payload.url)
    if adapter_page is not None:
        return adapter_page
    try:
        page = _fetch_public_page_requests(payload.url)
        return page
    except PublicJobFetchError as error:
        _remember_blocked_domain(context, payload.url, error)
        if error.code not in _PLAYWRIGHT_FALLBACK_CODES or not _PLAYWRIGHT_FALLBACK_ENABLED:
            raise
    rendered_text, rendered_title = _render_with_playwright(payload.url)
    rendered_effective_url, rendered_status = _render_metadata(payload.url)
    try:
        page = _build_evidence_page(
            requested_url=payload.url,
            effective_url=rendered_effective_url,
            title=rendered_title,
            visible_text=rendered_text,
            status_code=rendered_status,
        )
        return page
    except PublicJobFetchError as error:
        _remember_blocked_domain(context, payload.url, error)
        raise


def _dead_link_code(text: str, title: str | None) -> str | None:
    """Return ``dead_link`` when the page is a soft-404, else None (W2).

    A page with at least ``_MIN_REAL_JD_TEXT_CHARS`` of usable text is real
    content even if a marker string appears somewhere: markers are only
    scanned in the title and the first ``_JD_MARKER_SCAN_HEAD_CHARS`` chars of
    the body, and a "404" in the title also classifies the page as dead.
    """
    if len(text) >= _MIN_REAL_JD_TEXT_CHARS:
        return None
    if any(marker in text[:_JD_MARKER_SCAN_HEAD_CHARS] for marker in _SOFT_404_MARKERS):
        return "dead_link"
    if title and ("404" in title or any(marker in title for marker in _SOFT_404_MARKERS)):
        return "dead_link"
    return None


def _normalize_visible_text(text: str) -> str:
    """Normalize evidence text so requests and rendered paths hash alike."""
    lines = (" ".join(line.split()) for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def _build_evidence_page(
    *,
    requested_url: str,
    effective_url: str,
    title: str | None,
    visible_text: str,
    status_code: int | None,
    redirect_chain: list[str] | None = None,
) -> FetchPublicJobPageOutput:
    """Apply one classification/normalization contract to every fetch path."""
    normalized_text = _normalize_visible_text(visible_text)[:_MAX_VISIBLE_TEXT_CHARS]
    blocked_code = _detect_access_block(
        effective_url=effective_url,
        title=title,
        visible_text=normalized_text,
        status_code=status_code,
    )
    diagnostics = {
        "effective_url": effective_url,
        "redirect_chain": list(redirect_chain or [requested_url]),
        "status_code": status_code,
    }
    if blocked_code is not None:
        raise PublicJobFetchError(
            blocked_code,
            message=(
                f"公开页面被站点访问控制阻断（effective_url={effective_url}, "
                f"status={status_code}）。不会继续无状态重试。"
            ),
            **diagnostics,
        )
    if not normalized_text:
        raise PublicJobFetchError("empty_public_page", **diagnostics)
    dead_code = _dead_link_code(normalized_text, title)
    if dead_code is not None:
        raise PublicJobFetchError(
            dead_code,
            message="页面已下线或不存在（死链），非有效岗位证据。",
            **diagnostics,
        )
    if len(normalized_text) < _MIN_USABLE_TEXT_CHARS:
        raise PublicJobFetchError("public_page_content_insufficient", **diagnostics)
    content_hash = hashlib.sha256(normalized_text.encode("utf-8")).hexdigest()
    return FetchPublicJobPageOutput(
        artifact_id=f"observed:{content_hash}",
        source_url=requested_url,
        effective_url=effective_url,
        redirect_chain=list(redirect_chain or [requested_url]),
        http_status=status_code,
        title=title,
        visible_text=normalized_text,
        content_hash=content_hash,
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
        fetched = _fetch_validated(url)
        response = fetched.response
        effective_url = fetched.effective_url
        redirect_chain = fetched.redirect_chain
        status_code = getattr(response, "status_code", None)
        if status_code not in {401, 403, 429}:
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
    title = " ".join(parser.title_parts) or None
    page = _build_evidence_page(
        requested_url=url,
        effective_url=effective_url,
        title=title,
        visible_text="\n".join(parser.text_parts),
        status_code=status_code,
        redirect_chain=redirect_chain,
    )
    return page, html


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
        raw = page.eval_on_selector_all(
            "a[href], [data-href], [data-url], [data-link], [data-detail-url]",
            """els => els.flatMap(e => [
                e.href,
                e.getAttribute('data-href'),
                e.getAttribute('data-url'),
                e.getAttribute('data-link'),
                e.getAttribute('data-detail-url')
            ]).filter(Boolean)""",
        )
    except Exception:
        return []
    origin_host = urlsplit(origin_url).hostname
    seen: set[str] = set()
    links: list[str] = []
    for href in raw:
        if not isinstance(href, str):
            continue
        href = urljoin(origin_url, href)
        if not href.startswith(("http://", "https://")):
            continue
        if not _is_public_url(href):
            continue
        if not _same_host_or_linkedin_public_detail(
            origin_url, href, origin_host=origin_host
        ):
            continue
        path = urlsplit(href).path.lower()
        if not any(token in path for token in _JOB_RESULT_URL_TOKENS):
            continue
        if href in seen:
            continue
        seen.add(href)
        links.append(href)
    return links


def _same_host_or_linkedin_public_detail(
    origin_url: str,
    target_url: str,
    *,
    origin_host: str | None = None,
) -> bool:
    """Allow one audited cross-subdomain list route used by LinkedIn.

    LinkedIn's anonymous job-search endpoint lives on ``www.linkedin.com``
    while its public details are localized to hosts such as
    ``cn.linkedin.com``.  This exception is deliberately narrower than an
    eTLD+1 match: only the guest search API may emit it, and only
    ``/jobs/view/`` details on a real LinkedIn subdomain are accepted.
    """
    origin = urlsplit(origin_url)
    target = urlsplit(target_url)
    effective_origin_host = origin_host or origin.hostname
    if target.hostname == effective_origin_host:
        return True
    origin_hostname = (effective_origin_host or "").lower().rstrip(".")
    target_hostname = (target.hostname or "").lower().rstrip(".")
    return (
        origin_hostname == "www.linkedin.com"
        and origin.path.startswith(
            "/jobs-guest/jobs/api/seeMoreJobPostings/search"
        )
        and (
            target_hostname == "linkedin.com"
            or target_hostname.endswith(".linkedin.com")
        )
        and target.path.startswith("/jobs/view/")
    )


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
        attrs_map = dict(attrs)
        href = attrs_map.get("href")
        data_candidates = [
            attrs_map.get(key)
            for key in ("data-href", "data-url", "data-link", "data-detail-url")
        ]
        if not isinstance(href, str) or not href:
            href = next((value for value in data_candidates if isinstance(value, str) and value), None)
        if not isinstance(href, str) or not href:
            return
        candidates = [href]
        for value in data_candidates:
            if isinstance(value, str) and value:
                candidates.append(value)
        for candidate in candidates:
            self._add_candidate(candidate)

    def _add_candidate(self, href: str) -> None:
        resolved = urljoin(self._origin_url, href)
        if not resolved.startswith(("http://", "https://")):
            return
        if not _is_public_url(resolved):
            return
        if not _same_host_or_linkedin_public_detail(
            self._origin_url, resolved, origin_host=self._origin_host
        ):
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
    url: str,
    links: list[str],
    list_body: str,
    *,
    context: ToolContext | None = None,
    failure_sink: list[PublicJobPageFetchFailure] | None = None,
    failure_lock: threading.Lock | None = None,
    max_links: int | None = None,
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
    valid evidence. When a failure sink is provided, detail failures are
    returned to the caller instead of being silently discarded.
    """
    if len(links) < _MIN_LIST_LINKS:
        return []
    if any(
        marker.lower() in list_body[:_JD_MARKER_SCAN_HEAD_CHARS].lower()
        for marker in _JD_SECTION_MARKERS
    ):
        return []
    pages: list[FetchPublicJobPageOutput] = []

    def record_failure(link: str, error: PublicJobFetchError) -> None:
        if failure_sink is None:
            return
        _persist_fetch_failure(
            failure_sink,
            failure_lock,
            source_url=link,
            error=error,
        )

    expansion_limit = max_links if max_links is not None else _MAX_LIST_EXPANSION
    for link in _prioritize_detail_links(links)[:expansion_limit]:
        if context is not None:
            try:
                _ensure_domain_available(context, link)
            except PublicJobFetchError as exc:
                record_failure(link, exc)
                break
        try:
            pages.append(_fetch_public_page_requests(link))
            continue
        except PublicJobFetchError as exc:
            if context is not None:
                _remember_blocked_domain(context, link, exc)
            if exc.code not in _PLAYWRIGHT_FALLBACK_CODES or not _PLAYWRIGHT_FALLBACK_ENABLED:
                record_failure(link, exc)
                if exc.code in {"anti_bot_challenge", "access_denied"}:
                    break
                continue
        try:
            body_text, title = _render_with_playwright(link)  # no collect_links -> no recursion
        except PublicJobFetchError as exc:
            if context is not None:
                _remember_blocked_domain(context, link, exc)
            record_failure(link, exc)
            if exc.code in {"anti_bot_challenge", "access_denied"}:
                break
            continue
        try:
            pages.append(
                _build_evidence_page(
                    requested_url=link,
                    effective_url=link,
                    title=title,
                    visible_text=body_text,
                    status_code=200,
                )
            )
        except PublicJobFetchError as exc:
            if context is not None:
                _remember_blocked_domain(context, link, exc)
            record_failure(link, exc)
            if exc.code in {"anti_bot_challenge", "access_denied"}:
                break
            continue
    return pages


def _expand_official_campus_detail(
    url: str,
    page: FetchPublicJobPageOutput,
    *,
    context: ToolContext,
    failure_sink: list[PublicJobPageFetchFailure] | None = None,
    failure_lock: threading.Lock | None = None,
) -> list[FetchPublicJobPageOutput]:
    """Boundedly inspect the official campus portal indexes behind one JD.

    Some university portals expose a complete JD at the supplied URL but do
    not link sibling postings from that page.  For the reviewed Hebut portal,
    two public, paginated index pages are the smallest deterministic route to
    recent postings; their same-host detail links are then expanded by the
    normal bounded list handler.  This is public-page fetching only: every
    derived URL is still validated, and failures remain explicit.
    """
    parsed = urlsplit(url)
    if (
        page.quality != "jd_complete"
        or (parsed.hostname or "").lower() != _CAMPUS_PORTAL_HOST
        or not parsed.path.lower().startswith("/correcruit/content/")
    ):
        return []
    expanded: list[FetchPublicJobPageOutput] = []
    seen: set[str] = {url}
    for page_number in range(1, _MAX_CAMPUS_INDEX_PAGES + 1):
        index_url = (
            f"https://{_CAMPUS_PORTAL_HOST}/correcruit/index.html?p={page_number}"
        )
        try:
            index_page, raw_html = _fetch_public_page_requests_with_html(index_url)
        except PublicJobFetchError as error:
            if context is not None:
                _remember_blocked_domain(context, index_url, error)
            if failure_sink is not None:
                _persist_fetch_failure(
                    failure_sink,
                    failure_lock,
                    source_url=index_url,
                    error=error,
                )
            continue
        if index_page.source_url not in seen:
            expanded.append(index_page)
            seen.add(index_page.source_url)
        collector = _HtmlLinkCollector(index_url)
        collector.feed(raw_html)
        details = _expand_from_list_links(
            index_url,
            collector.links,
            index_page.visible_text,
            context=context,
            failure_sink=failure_sink,
            failure_lock=failure_lock,
            max_links=_MAX_CAMPUS_DETAIL_PAGES,
        )
        for detail in details:
            if detail.source_url not in seen:
                expanded.append(detail)
                seen.add(detail.source_url)
    return expanded


def _fetch_one_with_expansion(
    context: ToolContext,
    url: str,
    *,
    failure_sink: list[PublicJobPageFetchFailure] | None = None,
    failure_lock: threading.Lock | None = None,
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
    _assert_public_url(url)
    _ensure_domain_available(context, url)
    wechat_page = _fetch_wechat_article_page(context, url)
    if wechat_page is not None:
        return [wechat_page]
    adapter_page = _fetch_via_adapter(url)
    if adapter_page is not None:
        return [adapter_page]
    try:
        page, raw_html = _fetch_public_page_requests_with_html(url)
    except PublicJobFetchError as error:
        _remember_blocked_domain(context, url, error)
        if error.code not in _PLAYWRIGHT_FALLBACK_CODES or not _PLAYWRIGHT_FALLBACK_ENABLED:
            raise
    else:
        collector = _HtmlLinkCollector(url)
        collector.feed(raw_html)
        tencent_links = _tencent_query_detail_urls(url, raw_html)
        if tencent_links:
            return [
                page,
                *_expand_from_list_links(
                    url,
                    tencent_links,
                    page.visible_text,
                    context=context,
                    failure_sink=failure_sink,
                    failure_lock=failure_lock,
                ),
            ]
        try:
            iguopin_links = _iguopin_list_detail_urls(url)
        except PublicJobFetchError as probe_error:
            if failure_sink is not None:
                _persist_fetch_failure(
                    failure_sink,
                    failure_lock,
                    source_url=url,
                    error=probe_error,
                )
            return [page]
        if iguopin_links:
            return [
                page,
                *_expand_from_list_links(
                    url,
                    iguopin_links,
                    page.visible_text,
                    context=context,
                    failure_sink=failure_sink,
                    failure_lock=failure_lock,
                ),
            ]
        campus_pages = _expand_official_campus_detail(
            url,
            page,
            context=context,
            failure_sink=failure_sink,
            failure_lock=failure_lock,
        )
        if campus_pages:
            return [page, *campus_pages]
        return [
            page,
            *_expand_from_list_links(
                url,
                collector.links,
                page.visible_text,
                context=context,
                failure_sink=failure_sink,
                failure_lock=failure_lock,
            ),
        ]
    rendered_text, rendered_title, links = _render_with_playwright(
        url, collect_links=True
    )
    rendered_effective_url, rendered_status = _render_metadata(url)
    try:
        list_page = _build_evidence_page(
            requested_url=url,
            effective_url=rendered_effective_url,
            title=rendered_title,
            visible_text=rendered_text,
            status_code=rendered_status,
        )
    except PublicJobFetchError as error:
        _remember_blocked_domain(context, url, error)
        raise
    try:
        iguopin_links = _iguopin_list_detail_urls(url)
    except PublicJobFetchError as probe_error:
        if failure_sink is not None:
            _persist_fetch_failure(
                failure_sink,
                failure_lock,
                source_url=url,
                error=probe_error,
            )
        return [list_page]
    if iguopin_links:
        return [
            list_page,
            *_expand_from_list_links(
                url,
                iguopin_links,
                list_page.visible_text,
                context=context,
                failure_sink=failure_sink,
                failure_lock=failure_lock,
            ),
        ]
    campus_pages = _expand_official_campus_detail(
        url,
        list_page,
        context=context,
        failure_sink=failure_sink,
        failure_lock=failure_lock,
    )
    if campus_pages:
        return [list_page, *campus_pages]
    return [
        list_page,
        *_expand_from_list_links(
            url,
            links,
            list_page.visible_text,
            context=context,
            failure_sink=failure_sink,
            failure_lock=failure_lock,
        ),
    ]


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
    failure_lock = threading.Lock()
    def work(url: str) -> list[FetchPublicJobPageOutput]:
        try:
            return _fetch_one_with_expansion(
                context,
                url,
                failure_sink=failures,
                failure_lock=failure_lock,
            )
        except TypeError as exc:
            # Keep older test seams and third-party wrappers that still expose
            # the original two-argument helper contract working.
            if "unexpected keyword argument" not in str(exc):
                raise
            return _fetch_one_with_expansion(context, url)

    batch = run_parallel_with_progress(
        payload.urls,
        work,
        label="url",
        key=lambda url: url,
    )
    for result in batch:
        if result.error is not None:
            error = result.error
            code = error.code if isinstance(error, PublicJobFetchError) else "public_fetch_failed"
            failures.append(
                PublicJobPageFetchFailure(
                    source_url=result.item,
                    error_code=code,
                    effective_url=(
                        error.effective_url if isinstance(error, PublicJobFetchError) else None
                    ),
                    redirect_chain=(
                        error.redirect_chain if isinstance(error, PublicJobFetchError) else []
                    ),
                    http_status=(
                        error.status_code if isinstance(error, PublicJobFetchError) else None
                    ),
                    message=str(error),
                )
            )
        elif result.value is not None:
            pages.extend(result.value)
    return FetchPublicJobPagesOutput(pages=pages, failures=failures)


def _juejin_recent_days(context: ToolContext) -> int | None:
    """Return the explicit recent window for a named Juejin source request.

    The official adapter is deliberately narrow: a generic web query must not
    silently become an exhaustive-source claim. Juejin must be named in the
    original task goal and the goal must state a window supported by its
    public search period filter (at most seven days).
    """
    task_goal = context.metadata.get("task_goal")
    if not isinstance(task_goal, str) or not any(
        marker in task_goal.lower() for marker in ("稀土掘金", "juejin")
    ):
        return None
    match = re.search(r"(?:最近|近|过去|过去的)\s*(\d+)\s*(?:天|日)", task_goal)
    if match is None:
        return None
    recent_days = int(match.group(1))
    return recent_days if 1 <= recent_days <= 7 else None


def _juejin_article_projection(item: object) -> dict[str, object] | None:
    """Project one official search result onto bounded, auditable fields."""
    if not isinstance(item, dict) or item.get("result_type") != 2:
        return None
    result_model = item.get("result_model")
    article_info = (
        result_model.get("article_info") if isinstance(result_model, dict) else None
    )
    if not isinstance(article_info, dict):
        return None
    article_id = str(article_info.get("article_id") or "").strip()
    title = article_info.get("title")
    ctime = article_info.get("ctime")
    if (
        not re.fullmatch(r"\d{8,32}", article_id)
        or not isinstance(title, str)
        or not title.strip()
        or not str(ctime).isdigit()
    ):
        return None
    snippet = article_info.get("brief_content")
    return {
        "article_id": article_id,
        "title": " ".join(title.split())[:240],
        "snippet": (
            " ".join(snippet.split())[:500]
            if isinstance(snippet, str) and snippet.strip()
            else None
        ),
        "published_timestamp": int(str(ctime)),
    }


_JUEJIN_RECRUITMENT_POST_RE = re.compile(
    r"(?:校园招聘|校招|实习生招聘|招聘(?:正式)?启动|招聘岗位|"
    r"内推码|投递简历|欢迎.{0,12}投递|岗位职责|职位要求)",
    re.IGNORECASE,
)


def _juejin_record_matches_goal(record: dict[str, object], task_goal: str) -> bool:
    """Apply only explicit role/cohort hard constraints to one recent post."""
    searchable = " ".join(
        value
        for value in (record.get("title"), record.get("snippet"))
        if isinstance(value, str)
    )
    lowered = searchable.lower()
    goal_lowered = task_goal.lower()
    if _JUEJIN_RECRUITMENT_POST_RE.search(searchable) is None:
        return False
    if "产品经理" in task_goal and "产品经理" not in searchable:
        return False
    if "aigc" in goal_lowered and not any(
        marker in lowered for marker in ("aigc", "生成式", "大模型", "ai 产品")
    ):
        return False
    if any(marker in task_goal for marker in ("应届", "校招", "毕业生")) and not any(
        marker in searchable
        for marker in ("应届", "校招", "校园招聘", "毕业生", "届")
    ):
        return False
    return True


def _search_juejin_recent_posts(
    context: ToolContext,
    payload: SearchPublicJobPagesInput,
    *,
    recent_days: int,
) -> SearchPublicJobPagesOutput:
    """Exhaust Juejin's official recent search and filter exact task bounds."""
    _assert_public_url(_JUEJIN_SEARCH_API_URL)
    task_goal = str(context.metadata.get("task_goal") or "")
    records_by_id: dict[str, dict[str, object]] = {}
    coverage_complete = True
    for keyword in _JUEJIN_RECENT_SEARCH_QUERIES:
        cursor = "0"
        exhausted = False
        for _page_index in range(_JUEJIN_MAX_SEARCH_PAGES_PER_QUERY):
            request_url = _JUEJIN_SEARCH_API_URL + "?" + urlencode(
                {
                    "query": keyword,
                    "id_type": 0,
                    "cursor": cursor,
                    "limit": 20,
                    # Juejin's public UI maps period=2 to 最近一周. The
                    # requested <=7-day bound is applied again below using
                    # each article's official ctime.
                    "search_type": 2,
                    "sort_type": 0,
                    "version": 1,
                    "uuid": str(uuid.uuid4()),
                }
            )
            try:
                fetched = _fetch_validated(request_url)
                response = fetched.response
                response.raise_for_status()
                envelope = response.json()
            except (requests.RequestException, ValueError) as exc:
                raise PublicJobFetchError("public_search_failed") from exc
            if not isinstance(envelope, dict) or envelope.get("err_no") not in {0, "0"}:
                raise PublicJobFetchError("public_search_failed")
            raw_records = envelope.get("data")
            if not isinstance(raw_records, list):
                raise PublicJobFetchError("public_search_failed")
            for raw_record in raw_records:
                record = _juejin_article_projection(raw_record)
                if record is not None:
                    records_by_id[str(record["article_id"])] = record
            has_more = envelope.get("has_more") is True
            next_cursor = str(envelope.get("cursor") or "")
            if not has_more:
                exhausted = True
                break
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
        if not exhausted:
            coverage_complete = False

    now_timestamp = int(time.time())
    cutoff_timestamp = now_timestamp - recent_days * 86_400
    window_records = [
        record
        for record in records_by_id.values()
        if cutoff_timestamp <= int(record["published_timestamp"]) <= now_timestamp
    ]
    window_records.sort(
        key=lambda record: (-int(record["published_timestamp"]), str(record["article_id"]))
    )
    matched_records = [
        record
        for record in window_records
        if _juejin_record_matches_goal(record, task_goal)
    ]

    def to_scan_record(record: dict[str, object]) -> PublicCommunityScanRecord:
        timestamp = int(record["published_timestamp"])
        return PublicCommunityScanRecord(
            title=str(record["title"]),
            url=f"https://juejin.cn/post/{record['article_id']}",
            snippet=(
                str(record["snippet"])
                if isinstance(record.get("snippet"), str)
                else None
            ),
            published_at=datetime.fromtimestamp(
                timestamp, tz=timezone.utc
            ).isoformat(),
        )

    scan_evidence = [to_scan_record(record) for record in window_records]
    results = [
        PublicJobSearchResult(
            title=item.title,
            url=item.url,
            snippet=item.snippet,
        )
        for item in scan_evidence
        if item.url
        in {
            f"https://juejin.cn/post/{record['article_id']}"
            for record in matched_records[: payload.max_results]
        }
    ]
    hash_payload = {
        "source": _JUEJIN_SEARCH_API_URL,
        "queries": list(_JUEJIN_RECENT_SEARCH_QUERIES),
        "recent_days": recent_days,
        "coverage_complete": coverage_complete,
        "records": [
            {
                "article_id": record["article_id"],
                "title": record["title"],
                "snippet": record["snippet"],
                "published_timestamp": record["published_timestamp"],
            }
            for record in window_records
        ],
    }
    content_hash = hashlib.sha256(
        json.dumps(
            hash_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return SearchPublicJobPagesOutput(
        query=payload.query,
        source_url=_JUEJIN_SEARCH_API_URL,
        content_hash=content_hash,
        results=results,
        terminal_reason="candidates_found" if results else "search_empty",
        provider="juejin_official_search",
        source_scope="juejin.cn",
        time_window_days=recent_days,
        coverage_complete=coverage_complete,
        scanned_result_count=len(window_records),
        matched_result_count=len(matched_records),
        scan_queries=list(_JUEJIN_RECENT_SEARCH_QUERIES),
        scan_evidence=scan_evidence,
    )


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
    query = payload.query
    query_hash = hashlib.sha256(query.encode("utf-8")).hexdigest()
    attempted = context.metadata.setdefault("public_search_query_hashes", [])
    if not isinstance(attempted, list):
        attempted = []
        context.metadata["public_search_query_hashes"] = attempted
    route_limit = (
        _MAX_PUBLIC_SEARCH_ROUTES
        if context.metadata.get("runtime_auto_search") is True
        else _MAX_PUBLIC_SEARCH_ROUTES - 1
    )
    if query_hash in attempted or len(attempted) >= route_limit:
        raise PublicJobFetchError(
            "route_already_consumed",
            message="本次运行的公开搜索路由已使用完毕，请转入人工确认或使用已有候选页面。",
        )
    attempted.append(query_hash)
    juejin_recent_days = _juejin_recent_days(context)
    if juejin_recent_days is not None:
        return _search_juejin_recent_posts(
            context, payload, recent_days=juejin_recent_days
        )
    if (
        "site:" not in query
        and context.metadata.get("runtime_auto_search") is not True
    ):
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
    blocked_domains = _blocked_domains(context)
    def add_result(raw_result: dict[str, str], result_url: str | None) -> None:
        if result_url is None:
            return
        parsed = urlsplit(result_url)
        if (
            result_url in seen_urls
            or parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.hostname.endswith("bing.com")
            or _domain_scope(result_url) in blocked_domains
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
    if not results:
        # Sogou's mobile HTML exposes the direct public target in the wrapper's
        # ``url`` query parameter and gives materially better Chinese job-query
        # recall than the two desktop providers. We decode only that declared
        # target; ``add_result`` still applies host/path quality filtering and
        # the full public-URL/blocked-domain security checks before returning it.
        mobile_source_url = "https://m.sogou.com/web/searchList.jsp?" + urlencode(
            {"keyword": payload.query}
        )
        try:
            mobile_response = requests.get(
                mobile_source_url,
                timeout=20,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Linux; Android 12; Pixel 5) "
                        "AppleWebKit/537.36 Chrome/120 Mobile Safari/537.36"
                    )
                },
            )
            mobile_response.raise_for_status()
        except requests.RequestException:
            pass
        else:
            html = mobile_response.text
            source_url = mobile_source_url
            mobile_parser = _SogouMobileSearchResultParser(mobile_source_url)
            mobile_parser.feed(html)
            for raw_result in _prioritize_direct_search_results(mobile_parser.results):
                add_result(raw_result, raw_result["url"])
                if len(results) >= payload.max_results:
                    break
    return SearchPublicJobPagesOutput(
        query=payload.query,
        source_url=source_url,
        content_hash=hashlib.sha256(html.encode("utf-8", errors="replace")).hexdigest(),
        results=results,
        terminal_reason="candidates_found" if results else "search_empty",
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
    parsed = urlsplit(result_url)
    allowed_host = _is_allowed_job_host(hostname)
    normalized_path = parsed.path.rstrip("/").lower()
    generic_page = normalized_path.rsplit("/", 1)[-1] in {
        "home",
        "home.html",
        "index",
        "index.html",
    }
    if allowed_host and (normalized_path == "" or generic_page):
        # A recruiting homepage is a source index, not a direct job result.
        # The Executor can still use an explicitly supplied homepage when the
        # user asks for it, but search must not spend fetch budget on it.
        return False
    path_and_query = f"{parsed.path}?{parsed.query}".lower()
    url_token_match = any(
        token in path_and_query
        for token in _JOB_RESULT_URL_TOKENS
        if "." not in token
    )
    if allowed_host:
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
    source_quality = evidence.get("quality")
    if source_quality not in {"jd_complete", "list_only", "js_shell", "empty"}:
        source_quality = None
    official_records = _parse_known_official_career_records(visible_text, source_url)
    if official_records is not None:
        return _adapter_details_output(
            payload.artifact_id,
            source_url,
            content_hash,
            official_records,
            evidence_ref,
            source_quality=source_quality,
        )
    adapter_records = _parse_adapter_evidence(visible_text)
    if adapter_records is not None:
        return _adapter_details_output(
            payload.artifact_id,
            source_url,
            content_hash,
            adapter_records,
            evidence_ref,
            source_quality=source_quality,
        )
    # Several public campus portals put the real title/company in a detail
    # header but place navigation chrome before the JD body.  Add only
    # deterministic labels recovered from the captured page; never add model
    # text or fetch a second source.  This prevents ``招聘日历``/login-footer
    # navigation from winning over an otherwise valid public JD.
    extraction_text = _prepare_portal_extraction_text(visible_text, source_url)
    extracted = extract_jd_candidates(extraction_text, source_url)
    if source_quality == "jd_complete" and "/job/" in urlsplit(source_url).path:
        # A full aggregator detail page can append a "猜你喜欢" card list to
        # the same visible text. Keep only the candidate whose title is the
        # page's own official heading; downstream matching must never rank a
        # recommendation card as the requested JD.
        official_title = _infer_official_page_title(visible_text)
        if official_title:
            official_matches = [
                candidate
                for candidate in extracted
                if isinstance(candidate.title, str)
                and candidate.title.strip().lower() in official_title.lower()
            ]
            if official_matches:
                extracted = official_matches[:1]
            elif extracted:
                extracted = extracted[:1]
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
            or (
                inferred_title is not None
                and not _is_plausible_job_title(candidate.title)
            )
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
                extraction_text,
                labels=("岗位职责", "工作职责", "职位描述", "工作内容", "主要职责", "岗位定位", "你将负责"),
            )
            if single_jd_page
            else candidate.responsibilities
        ) or candidate.responsibilities
        portal_role_text = _extract_portal_role_text(visible_text, source_url)
        if portal_role_text and portal_role_text not in responsibilities:
            responsibilities = " ".join(
                part for part in (responsibilities, portal_role_text) if part
            )
        requirements = (
            _extract_jd_section(
                extraction_text,
                labels=(
                    "任职要求",
                    "职责要求",
                    "岗位要求",
                    "职位要求",
                    "资格要求",
                    "招聘要求",
                    "任职资格",
                    "专业要求",
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
                published_at=candidate.published_at,
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
        source_quality=source_quality,
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


def _parse_known_official_career_records(
    text: str, source_url: str
) -> list[dict[str, Any]] | None:
    """Split stable official multi-role career pages into per-JD records.

    The Baiont careers page renders every opening in one HTML document using
    repeated ``title -> 岗位职责 -> 要求`` blocks.  Treating that whole page as
    one JD lets a late requirement line become the title and merges unrelated
    roles.  This parser only segments captured text from Baiont's verified
    official hosts; it does not add facts or fetch a secondary source.
    """
    host = (urlsplit(source_url).hostname or "").lower().rstrip(".")
    if host not in {
        "baiontcapital.com",
        "www.baiontcapital.com",
        "baiont.ai",
        "www.baiont.ai",
    }:
        return None
    pattern = re.compile(
        r"^(?P<title>[^\r\n]{2,80})\r?\n"
        r"岗位职责\s*[:：]\s*\r?\n"
        r"(?P<responsibilities>.*?)\r?\n"
        r"要求\s*[:：]\s*\r?\n"
        r"(?P<requirements>.*?)"
        r"(?=\r?\n[^\r\n]{2,80}\r?\n岗位职责\s*[:：]"
        r"|\r?\n您可将简历投递至\s*[:：]?|\Z)",
        flags=re.DOTALL | re.MULTILINE,
    )
    records: list[dict[str, Any]] = []
    for match in pattern.finditer(text):
        title = " ".join(match.group("title").split()).strip()
        responsibilities = " ".join(
            match.group("responsibilities").split()
        ).strip()
        requirements = " ".join(match.group("requirements").split()).strip()
        if not title or not responsibilities:
            continue
        records.append(
            {
                "title": title,
                "company": "倍漾量化",
                "description": (
                    f"岗位职责：{responsibilities}\n任职要求：{requirements}"
                ),
                "apply_url": source_url,
            }
        )
    return records or None


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
    *,
    source_quality: str | None = None,
) -> ExtractObservedJobDetailsOutput:
    """Normalize adapter-record evidence into one candidate per record."""
    candidates = [_record_to_job_details(record, source_url, evidence_ref) for record in records]
    return ExtractObservedJobDetailsOutput(
        source_artifact_id=artifact_id,
        source_url=source_url,
        content_hash=content_hash,
        source_quality=source_quality,
        candidates=candidates,
    )


def _find_observed_evidence(
    context: ToolContext, artifact_id: str
) -> dict[str, object] | None:
    raw_evidence = context.metadata.get("observed_public_evidence")
    structured_candidates = context.metadata.get("structured_job_candidates", [])
    if isinstance(raw_evidence, list):
        for item in raw_evidence:
            if not isinstance(item, dict):
                continue
            if (
                item.get("artifact_id") == artifact_id
                and item.get("artifact_type") == "job_search_results"
            ):
                raise PublicJobFetchError("search_artifact_requires_fetch")
    resolved = resolve_target_evidence(raw_evidence, structured_candidates, artifact_id)
    if resolved is None:
        return None
    # Extract requires page-backed text.  A candidate-only projection without
    # a raw page is not enough to manufacture public evidence.
    source_artifact_id = resolved.get("source_artifact_id")
    if source_artifact_id and isinstance(raw_evidence, list):
        for item in raw_evidence:
            if isinstance(item, dict) and (
                item.get("artifact_id") == source_artifact_id
                or f"observed:{item.get('content_hash')}" == source_artifact_id
            ):
                merged = dict(item)
                merged.update(resolved)
                merged["artifact_id"] = item.get("artifact_id") or resolved.get("artifact_id")
                return merged
    return resolved


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
        "公司介绍",
        "公司简介",
        "招募对象",
        "职位类型",
        "招聘流程",
        "专业要求",
        "投递简历",
        "任职资格",
    )
    )
    matches = re.finditer(
        rf"(?:{label_pattern})\s*[:：]?\s*(.*?)"
        rf"(?=(?:{stop_pattern})\s*[:：]?|$)",
        text,
        flags=re.DOTALL,
    )
    for match in matches:
        value = " ".join(match.group(1).split()).strip()
        if value:
            return value
    return ""


def _prepare_portal_extraction_text(text: str, source_url: str) -> str:
    """Prefix stable labels for known public campus-detail layouts.

    The source page remains the sole evidence.  These prefixes only expose
    fields already present in that page's visible text to the deterministic
    JD parser, so footer/navigation strings cannot be mistaken for the job
    title or employer.
    """
    parsed = urlsplit(source_url)
    host = (parsed.hostname or "").lower()
    path = parsed.path.lower()
    prefixes: list[str] = []
    body = text
    if host == _CAMPUS_PORTAL_HOST and "/correcruit/content/" in path:
        match = re.search(
            r"(?:^|\n)>\s*>\s*招聘信息\s*\n"
            r"(?P<title>[^\n]+)\n[^\n]*\n【(?P<company>[^】\n]+)】",
            text,
        )
        if match:
            prefixes.extend(
                [
                    f"职位名称：{match.group('title').strip()}",
                    f"公司名称：{match.group('company').strip()}",
                ]
            )
            header = text[match.end() :]
            body = header
            location = re.search(r"工作地域：([^\n]*?)(?:\s+职位类别：|$)", header)
            if location:
                prefixes.append(f"工作地点：{location.group(1).strip()}")
            degree = re.search(r"学历要求：([^\s]+)", header)
            if degree:
                prefixes.append(f"学历要求：{degree.group(1).strip()}")
    elif host == "job.xiaohongshu.com" and "/campus/position/" in path:
        match = re.search(
            r"(?:^|\n)职位列表\s*\n(?P<title>[^\n]+)\n",
            text,
        )
        if match:
            prefixes.extend(
                [
                    f"职位名称：{match.group('title').strip()}",
                    "公司名称：行吟信息科技（上海）有限公司",
                ]
            )
            body = text[match.end() :]
        location = re.search(r"工作地点：([^\n]+)", text)
        if location:
            prefixes.append(f"工作地点：{location.group(1).strip()}")
    elif (
        (host == "linkedin.com" or host.endswith(".linkedin.com"))
        and path.startswith("/jobs/view/")
    ):
        # LinkedIn's anonymous detail renderer puts login controls before the
        # JD and appends a large "similar jobs" feed after it.  Its first line
        # is nevertheless a stable, captured header and named-source mirrors
        # carry an explicit attribution marker in the JD body.  Re-label only
        # those observed fields and bound the body at the first page-chrome
        # marker so recommendation titles cannot become the selected role.
        header = re.match(
            r"^(?P<company>[^\r\n]+?)正在招聘(?P<title>[^\r\n]+?)\s*"
            r"\((?P<location>[^)\r\n]+)\)\s*\|\s*领英(?:\r?\n|$)",
            text,
        )
        if header:
            prefixes.extend(
                [
                    f"职位名称：{header.group('title').strip()}",
                    f"公司名称：{header.group('company').strip()}",
                    f"工作地点：{header.group('location').strip()}",
                ]
            )
        source_marker_index = text.find("该职位来源于猎聘")
        if source_marker_index >= 0:
            observed_jd = text[source_marker_index:]
            observed_jd = re.split(
                r"(?:\r?\n)(?:Show more|Show less|职位级别|相似职位)(?:\r?\n|$)",
                observed_jd,
                maxsplit=1,
            )[0]
            sections = re.split(r"\s+任职要求\s+", observed_jd, maxsplit=1)
            responsibilities = " ".join(sections[0].split()).strip()
            requirements = (
                " ".join(sections[1].split()).strip() if len(sections) == 2 else ""
            )
            scoped = [*prefixes]
            if responsibilities:
                scoped.append(f"岗位职责：{responsibilities}")
            if requirements:
                scoped.append(f"任职要求：{requirements}")
            if scoped:
                return "\n".join(scoped)
    return "\n".join(prefixes + [body]) if prefixes else text


def _extract_portal_role_text(text: str, source_url: str) -> str:
    """Keep official portal role-family lines that identify specific roles."""
    parsed = urlsplit(source_url)
    if (
        (parsed.hostname or "").lower() != _CAMPUS_PORTAL_HOST
        or "/correcruit/content/" not in parsed.path.lower()
    ):
        return ""
    match = re.search(
        r"职位类型：\s*(.*?)\s*(?=招聘流程：|工作地点：|投递简历|$)",
        text,
        flags=re.DOTALL,
    )
    return " ".join(match.group(1).split()).strip() if match else ""


def _infer_official_page_title(text: str) -> str | None:
    """Infer a title from the header area of official pages lacking a title label."""
    header = re.split(r"(?:岗位职责|工作职责|职位描述)\s*[:：]?", text, maxsplit=1)[0]
    for line in reversed(header.splitlines()):
        candidate = line.strip()
        if (
            3 <= len(candidate) <= 100
            and re.search(
                r"(?:工程师|开发|算法|研究员|实习生|架构师|科学家|产品经理|项目经理)",
                candidate,
            )
            and "申请" not in candidate
        ):
            return candidate
    return None


def _is_plausible_job_title(value: object) -> bool:
    """Reject page chrome or numbered safety notes as a job title."""
    if not isinstance(value, str):
        return False
    candidate = " ".join(value.split()).strip()
    if not 2 <= len(candidate) <= 80:
        return False
    if re.match(r"^\d+[.、)]", candidate):
        return False
    return not any(
        marker in candidate
        for marker in (
            "如您应聘",
            "温馨提示",
            "平台内招聘方",
            "安全防范",
            "举报",
            "查看全部",
        )
    )


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
        if (
            re.fullmatch(
                r"[\u4e00-\u9fff]{2,12}(?:市|省|区|县|新区)?"
                r"(?:-[\u4e00-\u9fff]{1,12}(?:区|县|镇|街道)?)?",
                line,
            )
            and line not in {"本科", "硕士", "博士", "大专", "学历不限"}
        ):
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
