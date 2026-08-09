"""Moka public-JSON adapter: app.mokahr.com /api/outer/ats-apply/website/jobs/v2.

Official unauthenticated listing endpoint (ats-scrapers MokaScraper).  POST
``orgId/siteId/limit/offset`` + ``needStat``; the response is an AES-CBC
envelope (``data`` b64 + ``necromancer`` UTF-8 key, fixed IV) wrapping the
usual ``{code, success, msg, data: {jobStats, jobs}}`` object.  Records carry
stable ``job_id`` = ``MK_<id>`` (C3 dedup key) and apply via the public
detail page ``https://<host>/<board>/<slug>/<site_id>/job/<id>``.

Tenant career sites come in two board shapes::

    https://app.mokahr.com/social-recruitment/{slug}/{site_id}
    https://app.mokahr.com/campus-recruitment/{slug}/{site_id}

``slug`` is the tenant key (also the API ``orgId``) and ``site_id`` selects
one of the tenant's sites (e.g. a specific location).  A ``hire-r1.mokahr.com``
host is Moka's second public board host.
"""
from __future__ import annotations

import base64
import html
import json
import re
from typing import Any
from urllib.parse import urlparse

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .base import ERROR_ADAPTER, ERROR_ADAPTER_INVALID, ERROR_MALFORMED_PAYLOAD
from .base import AdapterError, BaseAdapter

_LIST_PATH = "/api/outer/ats-apply/website/jobs/v2"
_PAGE_SIZE = 50
_AES_IV = b"de7c21ed8d6f50fe"
_BOARD_SITE = {"social-recruitment": "social", "campus-recruitment": "campus"}
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


class MokaAdapter(BaseAdapter):
    """One Moka tenant encoded as ``<board>/<slug>/<site_id>``."""

    company = "moka"
    hosts = ("app.mokahr.com", "hire-r1.mokahr.com")

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._host = ""
        self._board = "social-recruitment"
        self._slug = ""
        self._site_id = 0

    def execute(self, task: Any, strategy: Any = None, trajectory: Any = None) -> dict[str, Any]:
        """Parse the tenant URL first, then run the standard fetch loop."""
        url = self._url_from_task(task)
        self._parse_target(url)
        return super().execute(task, strategy, trajectory)

    def _parse_target(self, url: str) -> None:
        """Derive host/board/slug/site_id from a tenant career-site URL."""
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if host not in self.hosts:
            raise AdapterError(ERROR_ADAPTER_INVALID, f"moka host not managed: {host}")
        parts = [part for part in (parsed.path or "").split("/") if part]
        if len(parts) != 3 or parts[0] not in _BOARD_SITE:
            raise AdapterError(ERROR_ADAPTER_INVALID, f"moka URL shape: {url}")
        try:
            site_id = int(parts[2])
        except ValueError as exc:
            raise AdapterError(ERROR_ADAPTER_INVALID, f"moka site_id: {parts[2]!r}") from exc
        self._host = host
        self._board = parts[0]
        self._slug = parts[1]
        self._site_id = site_id

    def fetch_page(self, page: int) -> tuple[list[dict[str, Any]], bool]:
        if not self._host:
            raise AdapterError(ERROR_ADAPTER_INVALID, "no moka target url set")
        offset = (page - 1) * _PAGE_SIZE
        envelope = self._request_json(
            method="POST",
            url=f"https://{self._host}{_LIST_PATH}",
            payload={
                "orgId": self._slug,
                "siteId": self._site_id,
                "limit": _PAGE_SIZE,
                "offset": offset,
                "needStat": True,
                "site": _BOARD_SITE[self._board],
            },
            headers={"Referer": self._tenant_url()},
        )
        data = _unwrap(envelope, slug=self._slug)
        jobs = data.get("jobs")
        if not isinstance(jobs, list):
            return [], False
        records = [self.build_record(job) for job in jobs if isinstance(job, dict)]
        stats = data.get("jobStats")
        total = stats.get("total") if isinstance(stats, dict) else None
        has_more = bool(jobs) and offset + len(jobs) < int(total or 0)
        return records, has_more

    def build_record(self, raw: dict[str, Any]) -> dict[str, Any]:
        job_id = str(raw.get("id") or "")
        commitment = raw.get("commitment")
        return {
            "job_id": f"MK_{job_id}",
            "title": raw.get("title", ""),
            "company": self._slug,
            "location": _location_text(raw.get("locations")),
            "department": _department_name(raw.get("department")),
            "employment_type": commitment if isinstance(commitment, str) else "",
            "description": _strip_html(raw.get("jobDescription")),
            "apply_url": f"{self._tenant_url()}/job/{job_id}",
            "posted_at": _posted_at(raw),
        }

    def _tenant_url(self) -> str:
        return f"https://{self._host}/{self._board}/{self._slug}/{self._site_id}"


def _unwrap(envelope: dict[str, Any], *, slug: str) -> dict[str, Any]:
    """Decrypt + unwrap a Moka envelope down to the jobs object.

    Handles the encrypted form (``data`` b64 + ``necromancer`` key, recursed
    after decryption) and the plaintext form (``code``/``success`` at the
    top, or the jobs object directly).
    """
    if envelope.get("necromancer") and isinstance(envelope.get("data"), str):
        return _unwrap(_decrypt(envelope["data"], envelope["necromancer"]), slug=slug)
    if envelope.get("success") is False or envelope.get("code") not in {None, 0}:
        code = envelope.get("code")
        message = envelope.get("msg") or "unknown"
        raise AdapterError(ERROR_ADAPTER, f"moka API error code={code} msg={message}")
    data = envelope.get("data")
    if not isinstance(data, dict):
        raise AdapterError(ERROR_MALFORMED_PAYLOAD, "moka envelope missing data object")
    return data


def _decrypt(data_b64: str, necromancer: str) -> dict[str, Any]:
    """AES-CBC decrypt (IV + PKCS7) a Moka payload to a JSON object."""
    key = necromancer.encode("utf-8")
    if len(key) not in {16, 24, 32}:
        raise AdapterError(ERROR_MALFORMED_PAYLOAD, f"moka necromancer key length {len(key)}")
    try:
        ciphertext = base64.b64decode(data_b64, validate=True)
    except (ValueError, TypeError) as exc:
        raise AdapterError(ERROR_MALFORMED_PAYLOAD, f"moka data not valid base64: {exc}") from exc
    if not ciphertext or len(ciphertext) % 16:
        raise AdapterError(ERROR_MALFORMED_PAYLOAD, "moka ciphertext length not a multiple of 16")
    decryptor = Cipher(algorithms.AES(key), modes.CBC(_AES_IV)).decryptor()
    plaintext = decryptor.update(ciphertext) + decryptor.finalize()
    pad = plaintext[-1]
    if pad < 1 or pad > 16 or plaintext[-pad:] != bytes([pad]) * pad:
        raise AdapterError(ERROR_MALFORMED_PAYLOAD, "moka PKCS7 unpad failed")
    try:
        decoded = json.loads(plaintext[:-pad])
    except ValueError as exc:
        raise AdapterError(ERROR_MALFORMED_PAYLOAD, f"moka decrypted body not JSON: {exc}") from exc
    if not isinstance(decoded, dict):
        raise AdapterError(ERROR_MALFORMED_PAYLOAD, "moka decrypted body is not an object")
    return decoded


def _location_text(locations: Any) -> str:
    """Join Moka location dicts into a deduplicated display string."""
    if not isinstance(locations, list):
        return ""
    pieces: list[str] = []
    seen: set[str] = set()
    for location in locations:
        if not isinstance(location, dict):
            continue
        display = ", ".join(
            value.strip()
            for value in (
                location.get("cityName"),
                location.get("provinceName"),
                location.get("country"),
            )
            if isinstance(value, str) and value.strip()
        )
        if display and display not in seen:
            seen.add(display)
            pieces.append(display)
    return "; ".join(pieces)


def _department_name(value: Any) -> str:
    """Moka department is a ``{"id", "name"}`` object (or a plain string)."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        name = value.get("name")
        return name.strip() if isinstance(name, str) else ""
    return ""


def _strip_html(value: Any) -> str:
    """Strip Moka JD HTML to plain text (25k cap, whitespace collapsed)."""
    if not isinstance(value, str) or not value.strip():
        return ""
    cleaned = _HTML_TAG_RE.sub(" ", value)
    cleaned = html.unescape(cleaned)
    return _WHITESPACE_RE.sub(" ", cleaned).strip()[:25_000]


def _posted_at(raw: dict[str, Any]) -> str:
    """First present publish/opened/created timestamp (raw ISO string)."""
    for key in ("publishedAt", "openedAt", "createdAt"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""
