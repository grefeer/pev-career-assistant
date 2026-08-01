"""Evidence-first public-page tool for the PEV ``job-discovery`` Skill."""

from __future__ import annotations

from html.parser import HTMLParser
import hashlib
import ipaddress
import socket
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator
import requests

from backend.app.services.agent_runtime.tool_context import ToolContext


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
