"""Shared plumbing for certified public-JSON adapters (A1).

A certified adapter is a fetch-only channel onto official, unauthenticated,
public JSON endpoints (docs/findjobs-optimization-plan.zh-CN.md §4.1).  It is
NOT a bypass: login/captcha/anti-bot sites are never adapted, TLS stays on,
requests are politely paced, and a hard per-company item cap bounds scale.
Every failure maps to a stable ``blocked`` code (SKILL.md) — never a silent
empty result and never a partial result presented as success.

The channel is double-gated: the backend flag ``use_public_api_adapters``
(off by default) AND ``endpoint_allowlist.json`` carrying
``review_status: "reviewed"`` (human-reviewed).  Until both hold, the whole
package refuses to run (``url_not_allowlisted``).
"""
from __future__ import annotations

import ipaddress
import json
import random
import socket
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import httpx

from browse import is_safe_public_url

#: Stable blocked codes (SKILL.md adapter contract).  ``http_error:*`` carries
#: the status code as its suffix (e.g. ``http_error:403``).
ERROR_URL_NOT_ALLOWLISTED = "url_not_allowlisted"
ERROR_EMPTY_RESULT = "empty_result"
ERROR_MALFORMED_PAYLOAD = "malformed_payload"
ERROR_ADAPTER = "adapter_error"
ERROR_ADAPTER_UNKNOWN = "adapter_unknown"
ERROR_ADAPTER_INVALID = "adapter_invalid"
ERROR_ALLOWLIST = "allowlist_missing"
ERROR_TIMEOUT = "timeout"
ERROR_DNS = "dns_error"
ERROR_TRANSPORT = "transport_error"


class AdapterError(RuntimeError):
    """Stable blocked code + human message; raised, never swallowed."""

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code


def load_allowlist(path: str | Path) -> dict[str, Any]:
    """Load and shape-check the endpoint allowlist; missing/invalid -> blocked.

    The allowlist is the human-review gate: adapters only run when
    ``review_status == "reviewed"`` and a reviewer is recorded.  Anything else
    is a hard ``allowlist_*`` blocked code, so a broken review state can never
    degrade into an un-reviewed fetch.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AdapterError(ERROR_ALLOWLIST, f"allowlist unreadable: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("endpoints"), list):
        raise AdapterError(ERROR_ALLOWLIST, "allowlist has no endpoints list")
    return data


def allowlist_reviewed(allowlist: dict[str, Any]) -> bool:
    """True only when a human review is recorded (reviewed_by + date)."""
    status = allowlist.get("review_status")
    return status == "reviewed" and bool(allowlist.get("reviewed_by"))


def host_matches(value: str, host: str) -> bool:
    """Exact host match; ``*.host`` entries also cover subdomains."""
    if value.startswith("*."):
        return host.endswith("." + value[2:]) or host == value[2:]
    return host == value


def _classify_http_error(exc: BaseException) -> AdapterError:
    """Map an httpx transport failure to a stable blocked code."""
    cause = exc.__cause__ if hasattr(exc, "__cause__") else None
    if isinstance(cause, socket.gaierror):
        return AdapterError(ERROR_DNS, f"dns resolution failed: {cause}")
    return AdapterError(ERROR_TRANSPORT, f"transport error: {type(exc).__name__}: {exc}")


def _public_ip_safe(hostname: str | None) -> bool:
    """Hostname-level fallback: all resolved IPs must be public."""
    if not hostname:
        return False
    try:
        addresses = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False
    resolved = {entry[4][0] for entry in addresses if entry[4]}
    if not resolved:
        return False
    try:
        return all(ipaddress.ip_address(addr).is_global for addr in resolved)
    except ValueError:
        return False


class BaseAdapter:
    """Common adapter behaviour; subclasses define endpoint + record mapping.

    Subclass contract:
      - ``company`` (str, registry key) and ``hosts`` (tuple of allowlist host
        patterns);
      - ``fetch_page(page) -> (list[dict], has_more: bool)`` — one page of
        records and whether to continue;
      - ``build_record(raw: dict) -> dict`` — raw endpoint item -> normalized
        record (title / location / description / apply_url / job_id / ...).
    """

    company: str = ""
    hosts: tuple[str, ...] = ()

    def __init__(
        self,
        *,
        allowlist_path: str | Path | None = None,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self._allowlist_path = str(allowlist_path or self._default_allowlist_path())
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(20.0),
            follow_redirects=False,
            headers={"User-Agent": "Mozilla/5.0 (compatible; CareerAssistant/1.0)"},
        )
        self._sleep = sleep
        self._rng = rng or random.Random()

    # -- allowlist / URL policy --------------------------------------------

    def _default_allowlist_path(self) -> Path:
        return Path(__file__).resolve().parent / "endpoint_allowlist.json"

    def _endpoint(self) -> dict[str, Any]:
        """Allowlisted endpoint for this company, or an allowlist blocked."""
        allowlist = load_allowlist(self._allowlist_path)
        if not allowlist_reviewed(allowlist):
            raise AdapterError(ERROR_ALLOWLIST, "allowlist not human-reviewed")
        for entry in allowlist["endpoints"]:
            if entry.get("company") == self.company:
                return entry
        raise AdapterError(ERROR_ALLOWLIST, f"no endpoint entry for {self.company}")

    def validate(self, url: str) -> bool:
        """True when this adapter may fetch ``url`` under the current gates."""
        if not is_safe_public_url(url):
            return False
        try:
            entry = self._endpoint()
        except AdapterError:
            return False
        try:
            parsed = urlparse(url)
        except ValueError:
            return False
        if not host_matches(entry.get("host", ""), parsed.hostname or ""):
            return False
        path = parsed.path or "/"
        return any(path.startswith(prefix) for prefix in entry.get("path_prefixes", []))

    # -- request plumbing ---------------------------------------------------

    def _pace(self, min_delay: float, max_delay: float) -> None:
        """Polite randomized pacing between requests (0.2-0.5s default)."""
        self._sleep(min_delay + self._rng.random() * (max_delay - min_delay))

    def _request(
        self,
        *,
        method: str,
        url: str,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """One endpoint call with stable error classification.

        Retries transient failures up to 3 times (1s/2s/4s backoff + jitter);
        HTTP failures become explicit blocked codes.  Callers parse the
        response body (JSON or text) themselves.
        """
        if not is_safe_public_url(url):
            raise AdapterError(ERROR_URL_NOT_ALLOWLISTED, f"unsafe endpoint URL: {url}")
        last: BaseException | None = None
        for attempt in range(3):
            try:
                response = self._client.request(method, url, params=params, json=payload, headers=headers)
                if response.status_code >= 400:
                    raise AdapterError(f"http_error:{response.status_code}", f"HTTP {response.status_code} from {url}")
                return response
            except AdapterError:
                raise
            except httpx.TimeoutException as exc:
                raise AdapterError(ERROR_TIMEOUT, f"request timed out: {exc}") from exc
            except httpx.HTTPError as exc:
                last = exc
                self._sleep(2**attempt + self._rng.random() * 0.5)
            except socket.gaierror as exc:
                raise AdapterError(ERROR_DNS, f"dns failure: {exc}") from exc
        raise _classify_http_error(last) if last else AdapterError(ERROR_TRANSPORT, "unreachable")

    def _request_json(
        self,
        *,
        method: str,
        url: str,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        max_items: int = 300,
    ) -> dict[str, Any]:
        """One JSON endpoint call with stable error classification.

        Retries transient failures up to 3 times (1s/2s/4s backoff + jitter);
        non-JSON or HTTP failures become explicit blocked codes.
        """
        response = self._request(method=method, url=url, params=params, payload=payload, headers=headers)
        try:
            data = response.json()
        except ValueError as exc:
            raise AdapterError(ERROR_MALFORMED_PAYLOAD, "endpoint returned non-JSON") from exc
        if not isinstance(data, dict):
            raise AdapterError(ERROR_MALFORMED_PAYLOAD, "endpoint JSON is not an object")
        return data

    def _request_text(
        self,
        *,
        method: str,
        url: str,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> str:
        """One raw-text endpoint call (HTML page) with stable error classification.

        Same retry/classification semantics as ``_request_json`` but returns
        ``response.text`` for HTML endpoints (e.g. Beisen's portal
        registration page that embeds ``BSGlobal``).
        """
        return self._request(method=method, url=url, params=params, payload=payload, headers=headers).text

    # -- execution ----------------------------------------------------------

    @staticmethod
    def _url_from_task(task: Any) -> str:
        """Accept the browse_fetch seam (url str) or a task-like object."""
        if isinstance(task, str):
            return task
        url = getattr(task, "url", None) or getattr(task, "source_url", None)
        if not url:
            raise AdapterError(ERROR_ADAPTER_INVALID, "task has no url/source_url")
        return str(url)

    def execute(self, task: Any, strategy: Any = None, trajectory: Any = None) -> dict[str, Any]:
        """Fetch records for ``task``; any failure raises AdapterError.

        Returns ``{"records": [...], "company": ..., "url": ...}``.  Records
        carry stable ``job_id`` (company-prefixed endpoint id, C3 dedup key).
        """
        url = self._url_from_task(task)
        entry = self._endpoint()  # allowlist gate first (allowlist_* codes)
        if not self.validate(url):
            raise AdapterError(ERROR_URL_NOT_ALLOWLISTED, f"url not allowlisted for {self.company}")
        max_items = int(entry.get("max_items", 300))
        min_delay = float(entry.get("min_delay_s", 0.2))
        max_delay = float(entry.get("max_delay_s", 0.5))
        records: list[dict[str, Any]] = []
        page = 1
        while True:
            if len(records) >= max_items:
                break
            if page > 1:
                self._pace(min_delay, max_delay)
            page_records, has_more = self.fetch_page(page)
            records.extend(page_records)
            if not has_more:
                break
            page += 1
        if not records:
            raise AdapterError(ERROR_EMPTY_RESULT, "endpoint returned no records")
        return {"records": records[:max_items], "company": self.company, "url": url}

    # -- subclass hooks ------------------------------------------------------

    def fetch_page(self, page: int) -> tuple[list[dict[str, Any]], bool]:
        """One page of (normalized records, has_more).  Subclass contract."""
        raise NotImplementedError

    def build_record(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Raw endpoint item -> normalized record.  Subclass contract."""
        raise NotImplementedError
