"""Protocol shared by deterministic listing and detail crawl drivers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from backend.app.services.job_discovery.crawling.crawl_plan import CrawlPlan
from backend.app.services.job_discovery.schemas import (
    DiscoveryTaskInput,
    RawJobDetail,
    RawJobListing,
)


@dataclass
class ListingPage:
    page_key: str
    listings: list[RawJobListing]
    next_cursor: dict[str, Any] | None
    terminal_evidence: str | None = None
    expected_page_count: int | None = None
    expected_listing_count: int | None = None


class CrawlDriver(Protocol):
    def fetch_listing_page(
        self,
        *,
        plan: CrawlPlan,
        task: DiscoveryTaskInput,
        cursor: dict[str, Any] | None,
    ) -> ListingPage: ...

    def fetch_detail(
        self,
        *,
        plan: CrawlPlan,
        listing: RawJobListing,
        resource_key: str,
    ) -> RawJobDetail: ...
