"""Certified public-JSON adapters (A1, docs/findjobs-optimization-plan.zh-CN.md).

Public surface used by the backend fetch seam (browse_fetch.py):

  - ``company_for_url(url)``   -> company key or None (hostname match);
  - ``load_company_adapter(company)`` -> an Adapter instance with
    ``validate(url)`` + ``execute(task, strategy, trajectory)``.

Contract (SKILL.md "Certified public-JSON adapters"): fetch-only channel,
official unauthenticated public endpoints, no login/captcha/anti-bot bypass,
TLS on, polite 0.2-0.5s pacing, 300 items/company hard cap, every failure an
explicit ``blocked`` code.  Double-gated: backend flag
``use_public_api_adapters`` AND ``endpoint_allowlist.json`` human review.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from .baidu import BaiduAdapter
from .base import AdapterError
from .beisen import BeisenAdapter
from .didi import DidiAdapter
from .moka import MokaAdapter
from .netease import NeteaseAdapter

__all__ = [
    "AdapterError",
    "CompanyAdapter",
    "company_for_url",
    "load_company_adapter",
]

#: Registry keyed by company; host matching lives in the allowlist entries.
_ADAPTERS: dict[str, type[Any]] = {
    "didi": DidiAdapter,
    "netease": NeteaseAdapter,
    "baidu": BaiduAdapter,
    "moka": MokaAdapter,
    "beisen": BeisenAdapter,
}

CompanyAdapter = type[Any]


def company_for_url(url: str) -> str | None:
    """Company key for a URL, or None when no adapter claims its host."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return None
    for company, adapter_cls in _ADAPTERS.items():
        instance = adapter_cls()
        for pattern in getattr(instance, "hosts", ()):
            if host == pattern or host.endswith("." + pattern.lstrip("*.")):
                return company
    return None


def load_company_adapter(company: str) -> CompanyAdapter:
    """Instantiate the adapter for ``company`` (raises AdapterError when unknown)."""
    adapter_cls = _ADAPTERS.get(company)
    if adapter_cls is None:
        raise AdapterError("adapter_unknown", f"no adapter for company {company!r}")
    return adapter_cls()
