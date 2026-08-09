"""Beisen public-JSON adapter: {tenant}.zhiye.com two-phase portal.

Official unauthenticated channel (ats-scrapers BeisenScraper).  The tenant
SPA embeds a ``BSGlobal`` config (``PortalId``) in
``/portal/registerSystemInfo``; the listing endpoint is
``POST /api/Jobad/GetJobAdPageList`` (``Code`` in {200, "200"}, ``Data``
list, ``Count`` total).  Records carry stable ``job_id`` = ``BS_<JobAdId>``
(C3 dedup key) and apply via ``https://<tenant>.zhiye.com/portal/jobs/<id>``.

A tenant page without a ``BSGlobal`` object is a legacy portal; this adapter
deliberately does NOT implement the legacy channel and reports a blocked
``adapter_error`` instead.
"""
from __future__ import annotations

import html
import json
import re
from typing import Any
from urllib.parse import urlparse

from .base import ERROR_ADAPTER, ERROR_ADAPTER_INVALID, ERROR_MALFORMED_PAYLOAD
from .base import AdapterError, BaseAdapter

_REGISTER_PATH = "/portal/registerSystemInfo"
_SEARCH_PATH = "/api/Jobad/GetJobAdPageList"
_PAGE_SIZE = 300
_BSGLOBAL_RE = re.compile(r"var\s+BSGlobal\s*=\s*(\{)", re.DOTALL)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_END_RE = re.compile(r"</(?:div|li|p|tr|h[1-6])\s*>", re.IGNORECASE)
_BREAK_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_HORIZONTAL_SPACE_RE = re.compile(r"[^\S\r\n]+")
_EXCESS_NEWLINES_RE = re.compile(r"\n{3,}")
_RECRUIT_SUFFIXES = (
    "校园招聘",
    "社会招聘",
    "人才招聘",
    "招贤纳士",
    "招聘门户",
    "招聘官网",
    "招聘网站",
    "招聘中心",
    "招聘",
)


class BeisenAdapter(BaseAdapter):
    """One current-generation Beisen tenant by zhiye.com subdomain."""

    company = "beisen"
    hosts = ("*.zhiye.com",)

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._base_url = ""
        self._tenant = ""
        self._tenant_name: str | None = None
        self._portal_id: str | None = None

    def execute(self, task: Any, strategy: Any = None, trajectory: Any = None) -> dict[str, Any]:
        """Parse the tenant URL first, then run the standard fetch loop."""
        url = self._url_from_task(task)
        self._parse_target(url)
        return super().execute(task, strategy, trajectory)

    def _parse_target(self, url: str) -> None:
        """Derive ``base_url`` + tenant slug from a tenant career-site URL."""
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if not host.endswith(".zhiye.com"):
            raise AdapterError(ERROR_ADAPTER_INVALID, f"beisen host not managed: {host}")
        self._base_url = f"https://{host}"
        self._tenant = host[: -len(".zhiye.com")]

    def fetch_page(self, page: int) -> tuple[list[dict[str, Any]], bool]:
        if not self._base_url:
            raise AdapterError(ERROR_ADAPTER_INVALID, "no beisen target url set")
        portal_id = self._portal_id or self._resolve_tenant()
        payload = {
            "PageIndex": page,
            "PageSize": _PAGE_SIZE,
            "KeyWords": "",
            "SpecialType": 0,
            "PortalId": portal_id,
            "DisplayFields": ["Category", "Description", "LocId", "Org", "Salary"],
        }
        data = self._request_json(
            method="POST",
            url=f"{self._base_url}{_SEARCH_PATH}",
            payload=payload,
            headers={"Referer": f"{self._base_url}/social/jobs"},
        )
        if data.get("Code") not in {200, "200"}:
            raise AdapterError(ERROR_ADAPTER, f"beisen API failure: {data.get('Code')!r}")
        items = data.get("Data")
        if not isinstance(items, list):
            return [], False
        records = [self.build_record(item) for item in items if isinstance(item, dict)]
        count = _coerce_int(data.get("Count"))
        has_more = bool(items) and page * len(items) < int(count or 0)
        return records, has_more

    def _resolve_tenant(self) -> str:
        """Fetch the tenant SPA config; returns the PortalId (cached)."""
        text = self._request_text(
            method="GET",
            url=f"{self._base_url}{_REGISTER_PATH}",
            headers={"Accept": "text/html,application/xhtml+xml,*/*"},
        )
        match = _BSGLOBAL_RE.search(text)
        if not match:
            raise AdapterError(ERROR_ADAPTER, "beisen tenant has no BSGlobal (legacy portal?)")
        try:
            config, _ = json.JSONDecoder().raw_decode(text, match.start(1))
        except ValueError as exc:
            raise AdapterError(ERROR_MALFORMED_PAYLOAD, f"beisen BSGlobal not valid JSON: {exc}") from exc
        portal_id = _clean_text(config.get("PortalId"))
        if not portal_id:
            raise AdapterError(ERROR_ADAPTER, "beisen BSGlobal missing PortalId")
        filing = config.get("BeiAnInfo")
        site_name = filing.get("SiteName") if isinstance(filing, dict) else None
        self._tenant_name = _clean_company_name(site_name) or self._tenant
        self._portal_id = portal_id
        return portal_id

    def build_record(self, raw: dict[str, Any]) -> dict[str, Any]:
        job_id = str(raw.get("JobAdId") or "")
        duty = _clean_html(raw.get("Duty"))
        require = _clean_html(raw.get("Require"))
        description = "\n\n".join(part for part in (duty, require) if part)
        return {
            "job_id": f"BS_{job_id}",
            "title": _clean_text(raw.get("JobAdName")) or "",
            "company": self._tenant_name or self._tenant,
            "location": _format_location(raw.get("LocNames"))
            or _clean_text(raw.get("DetailAddress"))
            or "",
            "category": _clean_text(raw.get("Category")) or "",
            "job_type": _clean_text(raw.get("Station")) or "",
            "department": _clean_text(raw.get("Org")) or _clean_text(raw.get("Category")) or "",
            "salary": _clean_text(raw.get("Salary")) or "",
            "description": description,
            "apply_url": f"{self._base_url}/portal/jobs/{job_id}",
            "posted_at": _clean_text(raw.get("ChangeDate")) or _clean_text(raw.get("PostDate")) or "",
        }


def _clean_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _clean_company_name(value: object) -> str | None:
    """Site name without 招聘 suffixes (e.g. "奇瑞招聘" -> "奇瑞")."""
    name = _clean_text(value)
    if name:
        for suffix in _RECRUIT_SUFFIXES:
            if name.endswith(suffix):
                return name[: -len(suffix)].strip()
    return name


def _clean_html(value: object) -> str | None:
    """Block-aware HTML -> text: <br>/</div>/</li> become line breaks."""
    text = _clean_text(value)
    if text is None:
        return None
    text = _BREAK_RE.sub("\n", text)
    text = _BLOCK_END_RE.sub("\n", text)
    text = _HTML_TAG_RE.sub("", text)
    text = html.unescape(text).replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(_HORIZONTAL_SPACE_RE.sub(" ", line).strip() for line in text.split("\n"))
    text = _EXCESS_NEWLINES_RE.sub("\n\n", text).strip()
    return text or None


def _format_location(value: object) -> str | None:
    """LocNames is a list of strings like ["安徽省·芜湖市"]."""
    if not isinstance(value, list):
        return None
    parts = [str(part).strip() for part in value if str(part).strip()]
    return ", ".join(parts) if parts else None


def _coerce_int(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
