"""Evidence-first public-page tool for the PEV ``job-discovery`` Skill."""

from __future__ import annotations

import base64
from html.parser import HTMLParser
import hashlib
import ipaddress
import re
import socket
from urllib.parse import parse_qs, urlencode, urlsplit

from pydantic import BaseModel, Field, field_validator
import requests

from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.job_discovery.tools.jd_extraction import extract_jd_candidates


class PublicJobFetchError(RuntimeError):
    """Stable, non-sensitive public-web fetch failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
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


class ExtractObservedJobDetailsOutput(BaseModel):
    """Structured JD candidates derived only from a selected captured page."""

    source_artifact_id: str
    source_url: str
    content_hash: str
    candidates: list[ExtractedJobDetails]


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
        address = ipaddress.ip_address(sockaddr[0])
        if not address.is_global:
            raise PublicJobFetchError("unsafe_public_url")


def fetch_public_job_page(
    context: ToolContext, payload: FetchPublicJobPageInput
) -> FetchPublicJobPageOutput:
    """Fetch public HTML and expose immutable visible-text evidence to Executor."""
    del context
    _assert_public_url(payload.url)
    try:
        response = requests.get(
            payload.url,
            timeout=20,
            allow_redirects=True,
            headers={"User-Agent": "CareerAssistantPEV/1.0 (+public-job-fetch)"},
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise PublicJobFetchError("public_fetch_failed") from exc
    if response.encoding is None or response.encoding.lower() in {"iso-8859-1", "latin-1"}:
        response.encoding = response.apparent_encoding or "utf-8"
    html = response.text
    parser = _VisibleTextParser()
    parser.feed(html)
    visible_text = "\n".join(parser.text_parts)[:12_000]
    if not visible_text:
        raise PublicJobFetchError("empty_public_page")
    title = " ".join(parser.title_parts) or None
    return FetchPublicJobPageOutput(
        artifact_id=f"observed:{hashlib.sha256(html.encode('utf-8', errors='replace')).hexdigest()}",
        source_url=payload.url,
        title=title,
        visible_text=visible_text,
        content_hash=hashlib.sha256(html.encode("utf-8", errors="replace")).hexdigest(),
    )


def search_public_job_pages(
    context: ToolContext, payload: SearchPublicJobPagesInput
) -> SearchPublicJobPagesOutput:
    """Search a fixed public provider and return only direct, safe career URLs."""
    del context
    source_url = f"https://www.bing.com/search?{urlencode({'q': payload.query})}"
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
    for raw_result in parser.results:
        result_url = _direct_bing_result_url(raw_result["url"])
        if result_url is None:
            continue
        parsed = urlsplit(result_url)
        if (
            result_url in seen_urls
            or parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.hostname.endswith("bing.com")
        ):
            continue
        try:
            _assert_public_url(result_url)
        except PublicJobFetchError:
            continue
        seen_urls.add(result_url)
        results.append(PublicJobSearchResult(
            title=raw_result["title"],
            url=result_url,
            snippet=raw_result.get("snippet"),
        ))
        if len(results) >= payload.max_results:
            break
    return SearchPublicJobPagesOutput(
        query=payload.query,
        source_url=source_url,
        content_hash=hashlib.sha256(html.encode("utf-8", errors="replace")).hexdigest(),
        results=results,
    )


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
    candidates = []
    for candidate in extract_jd_candidates(visible_text, source_url):
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
        responsibilities = _extract_jd_section(
            visible_text,
            labels=("岗位职责", "工作职责", "职位描述", "工作内容", "主要职责"),
        ) or candidate.responsibilities
        requirements = _extract_jd_section(
            visible_text,
            labels=(
                "任职要求",
                "职责要求",
                "岗位要求",
                "职位要求",
                "资格要求",
                "招聘要求",
            ),
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
            )
        )
    return ExtractObservedJobDetailsOutput(
        source_artifact_id=payload.artifact_id,
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
        if re.fullmatch(r"[\u4e00-\u9fff]{2,12}(?:市|省)?", line):
            return [line]
        if re.search(r"(?:岗位职责|工作职责|职位描述)", line):
            break
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
