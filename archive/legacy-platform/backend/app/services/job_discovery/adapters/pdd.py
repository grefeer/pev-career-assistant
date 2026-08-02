"""PDD careers certified adapter backed by its public position-list API."""
from __future__ import annotations

import fnmatch
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from backend.app.services.job_discovery.adapters.complete_crawl_base import CompleteCrawlAdapter
from backend.app.services.job_discovery.crawling.crawl_plan import CrawlPlan
from backend.app.services.job_discovery.crawling.driver import ListingPage
from backend.app.services.job_discovery.schemas import DiscoveryTaskInput, RawJobDetail, RawJobListing
from backend.app.services.job_discovery.strategy.trajectory_buffer import TrajectoryBuffer

PDD_CRAWL_PLAN = """\
plan_type: crawl_plan
version: 1
listing:
  item_selector: ""
  title_selector: ""
pagination:
  type: page_number
  items_path: "$.result.list"
  total_count_path: "$.result.total"
detail:
  body_selector: ""
completion:
  require_all_pages: true
  require_all_details: true
"""

_LIST_API = "https://careers.pddglobalhr.com/api/careers/api/recruit/position/list"
_DETAIL_PATH = "/campus/grad/detail"
_PAGE_SIZE = 100
_TERMINAL_PROOF = "pdd_api_total_reached"


def _share_token(source_url: str) -> str:
    token = (parse_qs(urlparse(source_url).query).get("t") or [""])[0]
    if not token:
        raise ValueError("PDD source URL is missing its public share token")
    return token


def _detail_url(source_url: str, position_id: str, token: str) -> str:
    parsed = urlparse(source_url)
    return f"{parsed.scheme}://{parsed.netloc}{_DETAIL_PATH}?{urlencode({'positionId': position_id, 't': token})}"


def _listing_from_item(item: dict[str, Any], source_url: str, token: str) -> RawJobListing:
    position_id = str(item.get("id") or "")
    return RawJobListing(
        source_url=source_url,
        detail_url=_detail_url(source_url, position_id, token) if position_id else None,
        apply_url=_detail_url(source_url, position_id, token) if position_id else None,
        company="拼多多",
        title=str(item.get("name") or ""),
        locations=[str(item["workLocationName"])] if item.get("workLocationName") else [],
        job_code=str(item.get("code") or "") or None,
    )


class PddCrawlDriver:
    """Total-driven API driver; the public listing payload contains JD text."""

    def __init__(
        self,
        *,
        source_url: str,
        api_fetcher: Callable[[int, int, str], dict[str, Any]] | None = None,
    ) -> None:
        self._source_url = source_url
        self._api_fetcher = api_fetcher or self._fetch_api_page
        self._emitted = 0
        self._total: int | None = None
        self._jd_by_url: dict[str, str] = {}

    def fetch_listing_page(
        self, *, plan: CrawlPlan, task: DiscoveryTaskInput, cursor: dict[str, Any] | None
    ) -> ListingPage:
        page_number = int((cursor or {}).get("page", 1))
        token = _share_token(task.source_url)
        payload = self._api_fetcher(page_number, _PAGE_SIZE, token)
        result = payload.get("result") if isinstance(payload, dict) else None
        if not isinstance(result, dict):
            raise RuntimeError("PDD list API returned no result object")
        total = result.get("total")
        self._total = int(total) if str(total or "").isdigit() else None
        rows = result.get("list") or []
        listings: list[RawJobListing] = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            listing = _listing_from_item(item, task.source_url, token)
            if listing.detail_url:
                jd = str(item.get("jobDuty") or "").strip()
                if jd:
                    # The API field is semantically a responsibility section,
                    # but its raw value has no heading.  Preserve that
                    # authoritative semantics for the deterministic normalizer.
                    self._jd_by_url[listing.detail_url] = f"岗位职责\n{jd}"
            listings.append(listing)
        self._emitted += len(listings)
        reached = self._total is not None and self._emitted >= self._total
        return ListingPage(
            page_key=str(page_number),
            listings=listings,
            next_cursor=None if reached else {"page": page_number + 1},
            terminal_evidence=_TERMINAL_PROOF if reached else None,
            expected_listing_count=self._total,
        )

    def fetch_detail(
        self, *, plan: CrawlPlan, listing: RawJobListing, resource_key: str
    ) -> RawJobDetail:
        detail_url = listing.detail_url or ""
        body = self._jd_by_url.get(detail_url)
        if not body:
            raise RuntimeError("PDD listing payload did not include jobDuty")
        return RawJobDetail(
            detail_url=detail_url,
            full_text=body,
            title=listing.title or None,
            locations=list(listing.locations),
            detail_resource_key=resource_key,
        )

    @staticmethod
    def _fetch_api_page(page: int, page_size: int, token: str) -> dict[str, Any]:
        response = requests.post(
            _LIST_API,
            json={"page": page, "pageSize": page_size, "t": token},
            headers={
                "Origin": "https://careers.pddglobalhr.com",
                "Referer": "https://careers.pddglobalhr.com/campus/grad",
            },
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("success"):
            raise RuntimeError("PDD list API reported success=false")
        return payload


class PddCrawlAdapter(CompleteCrawlAdapter):
    url_pattern = "careers.pddglobalhr.com/*"

    def build_driver(
        self, plan: CrawlPlan, task: DiscoveryTaskInput, trajectory: TrajectoryBuffer
    ) -> PddCrawlDriver:
        return PddCrawlDriver(source_url=task.source_url)

    def validate(self, url: str) -> bool:
        target = url.replace("https://", "").replace("http://", "")
        return fnmatch.fnmatch(url, self.url_pattern) or fnmatch.fnmatch(target, self.url_pattern)
