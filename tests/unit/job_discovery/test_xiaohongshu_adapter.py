"""Unit tests for the Xiaohongshu certified complete-crawl adapter (Task 5).

Exercises two drivers against the deterministic ``CrawlExecutor`` and
``CoverageVerifier``:

* ``XiaohongshuCrawlDriver.from_fixture`` -- the adapter's fixture-replay path,
  loading ``tests/fixtures/job_discovery/xiaohongshu/contract.json`` and
  following the API cursor until ``next_cursor_null``.
* ``XhsFixtureDriver`` -- a synthetic test driver that parametrizes the
  cursor's terminal value, used to prove (a) a non-null cursor never terminates
  successfully, and (b) the 43->1 false-negative regression: one
  ``RawJobListing`` per ``positionId``, each queued as a separate detail fetch
  (never a one-shot blob split).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from backend.app.services.job_discovery.adapters.xiaohongshu import (
    XHS_CRAWL_PLAN,
    XiaohongshuCrawlDriver,
)
from backend.app.services.job_discovery.crawling.crawl_executor import CrawlExecutor
from backend.app.services.job_discovery.crawling.crawl_plan import CrawlPlan
from backend.app.services.job_discovery.crawling.driver import ListingPage
from backend.app.services.job_discovery.schemas import (
    DiscoveryTaskInput,
    PaginationType,
    RawJobDetail,
    RawJobListing,
)
from tests.unit.job_discovery._fixture_crawl import execute_fixture_crawl

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "job_discovery"
    / "xiaohongshu"
)

SOURCE_URL = "https://job.xiaohongshu.com/campus/position"


def xhs_plan() -> CrawlPlan:
    return CrawlPlan.from_yaml(XHS_CRAWL_PLAN)


def xhs_task() -> DiscoveryTaskInput:
    return DiscoveryTaskInput(
        source_id="xiaohongshu",
        raw_record_id="raw-xhs",
        external_record_id="xhs-1",
        source_key="xiaohongshu",
        source_url=SOURCE_URL,
        url_hash="xhs-hash",
        record_fields=[],
    )


def _synthetic_listing(index: int) -> RawJobListing:
    """One redacted Xiaohongshu-shaped listing; unique per ``positionId``."""
    detail_url = f"https://job.xiaohongshu.com/campus/position/{1000 + index}"
    return RawJobListing(
        source_url=SOURCE_URL,
        detail_url=detail_url,
        apply_url=detail_url,
        company=None,
        title=f"XHS position {index}",
    )


@dataclass
class XhsFixtureDriver:
    """Synthetic API-cursor driver for the cursor-completion regression tests.

    Emits ``record_count`` listings (one per ``positionId``) across pages of
    ``page_size``. When the last page is reached:

    * if ``last_cursor`` is ``None`` (default) the page returns
      ``next_cursor=None`` + ``next_cursor_null`` terminal evidence -- a
      provably complete crawl;
    * if ``last_cursor`` is set (e.g. ``"still-more"``) the page returns that
      non-null cursor with no terminal evidence -- pagination never terminates,
      which the verifier must report as incomplete.
    """

    record_count: int = 4
    page_size: int = 2
    last_cursor: str | None = None
    _emitted: int = field(default=0, init=False)
    _page: int = field(default=0, init=False)

    def fetch_listing_page(
        self,
        *,
        plan: CrawlPlan,
        task: DiscoveryTaskInput,
        cursor: object,
    ) -> ListingPage:
        self._page += 1
        if self._emitted >= self.record_count:
            # Exhausted: an empty page whose cursor is the configured last
            # cursor. Repeats trigger loop detection (or budget exhaustion).
            return ListingPage(
                page_key="exhausted",
                listings=[],
                next_cursor=self.last_cursor,
                terminal_evidence=(
                    "next_cursor_null" if self.last_cursor is None else None
                ),
                expected_listing_count=self.record_count,
            )
        remaining = self.record_count - self._emitted
        chunk_n = min(self.page_size, remaining)
        listings = [
            _synthetic_listing(self._emitted + i) for i in range(chunk_n)
        ]
        self._emitted += chunk_n
        reached = self._emitted >= self.record_count
        if reached:
            next_cursor = None if self.last_cursor is None else self.last_cursor
            terminal = "next_cursor_null" if self.last_cursor is None else None
        else:
            next_cursor = f"cursor-{self._page}"
            terminal = None
        return ListingPage(
            page_key=str(self._page),
            listings=listings,
            next_cursor=next_cursor,
            terminal_evidence=terminal,
            expected_listing_count=self.record_count,
        )

    def fetch_detail(
        self,
        *,
        plan: CrawlPlan,
        listing: RawJobListing,
        resource_key: str,
    ) -> RawJobDetail:
        return RawJobDetail(
            detail_url=listing.detail_url or "",
            full_text=listing.title or "xhs jd body",
            title=listing.title,
            detail_resource_key=resource_key,
        )


def test_xhs_cursor_requires_null_next_cursor() -> None:
    """A non-null final cursor must never declare coverage complete."""
    driver = XhsFixtureDriver(last_cursor="still-more")
    result = CrawlExecutor(driver).execute(plan=xhs_plan(), task=xhs_task())
    assert result.coverage.coverage_complete is False


def test_xhs_collects_total_before_success() -> None:
    result = execute_fixture_crawl("xiaohongshu")
    assert result.coverage.raw_listing_count == result.coverage.expected_listing_count
    assert "next_cursor_null" in result.coverage.completion_evidence
    assert result.coverage.coverage_complete is True


def test_xhs_crawl_plan_yaml_parses_as_api_cursor() -> None:
    plan = xhs_plan()
    assert plan.pagination.type is PaginationType.API_CURSOR
    assert plan.pagination.items_path == "$.data.list"
    assert plan.pagination.total_count_path == "$.data.total"
    assert plan.completion.require_all_details is True


def test_xhs_emits_one_listing_per_position_id_no_blob_split() -> None:
    """43->1 false-negative regression: 43 records yield 43 raw listings and 43
    separately-queued detail fetches -- the landing blob is never handed to
    ``_extract_jd_candidates`` for one-shot splitting."""
    driver = XhsFixtureDriver(record_count=43, page_size=10)
    result = CrawlExecutor(driver).execute(plan=xhs_plan(), task=xhs_task())

    assert result.coverage.raw_listing_count == 43
    assert result.coverage.expected_listing_count == 43
    # Every listing is a distinct positionId -> distinct detail resource.
    assert len(result.raw_details) == 43
    assert result.coverage.fetched_detail_count == result.coverage.total_detail_count
    assert result.coverage.coverage_complete is True
    assert "next_cursor_null" in result.coverage.completion_evidence


def test_xhs_detail_route_is_position_id() -> None:
    listing = XiaohongshuCrawlDriver.from_fixture(FIXTURE).first_listing()
    assert "/campus/position/" in (listing.detail_url or "")
    assert listing.detail_url != listing.source_url
