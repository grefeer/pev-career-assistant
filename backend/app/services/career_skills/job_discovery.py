"""Evidence-first public-page tool for the PEV ``job-discovery`` Skill."""

from __future__ import annotations

from html.parser import HTMLParser
import hashlib
import ipaddress
import re
import socket
from urllib.parse import urlsplit

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
    source_url: str
    title: str | None
    visible_text: str
    content_hash: str


class ExtractObservedJobDetailsInput(BaseModel):
    """The immutable evidence artifact selected by an autonomous Agent."""

    artifact_id: str = Field(min_length=1, max_length=64)


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
        source_url=payload.url,
        title=title,
        visible_text=visible_text,
        content_hash=hashlib.sha256(html.encode("utf-8", errors="replace")).hexdigest(),
    )


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
        responsibilities = _extract_jd_section(
            visible_text,
            labels=("岗位职责", "工作职责", "职位描述", "工作内容", "主要职责"),
        ) or candidate.responsibilities
        requirements = _extract_jd_section(
            visible_text,
            labels=("任职要求", "岗位要求", "职位要求", "资格要求", "招聘要求"),
        ) or candidate.requirements
        candidates.append(
            ExtractedJobDetails(
                title=candidate.title,
                company_name=candidate.company_name,
                locations=candidate.locations,
                responsibilities=responsibilities,
                requirements=requirements,
                recruitment_types=candidate.recruitment_types,
                apply_url=candidate.apply_url,
                deadline_text=candidate.deadline_text,
                confidence=round(candidate.confidence, 4),
                evidence_refs=[evidence_ref],
                normalization_warnings=candidate.normalization_warnings,
            )
        )
    return ExtractObservedJobDetailsOutput(
        source_artifact_id=payload.artifact_id,
        candidates=candidates,
    )


def _find_observed_evidence(
    context: ToolContext, artifact_id: str
) -> dict[str, object] | None:
    raw_evidence = context.metadata.get("observed_public_evidence")
    if not isinstance(raw_evidence, list):
        return None
    for item in raw_evidence:
        if isinstance(item, dict) and item.get("artifact_id") == artifact_id:
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
