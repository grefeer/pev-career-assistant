"""Unit tests for the Feishu certified complete-crawl adapter (Task 4.3).

Exercises the fixture-replay ``FeishuCrawlDriver`` against the deterministic
``CrawlExecutor`` and ``CoverageVerifier``. The driver loads the captured
``tests/fixtures/job_discovery/feishu/contract.json`` and replays page-number
pagination driven by the declared ``total_count`` until the last page,
terminating with ``total_count_reached``. Detail URLs are job-level
``/campus/position/{position_id}/detail`` routes sourced from the fixture's
public ``detail_url_examples`` (sample ``id`` fields are PII-redacted).
"""
from __future__ import annotations

import re
from pathlib import Path

from backend.app.services.job_discovery.adapters.feishu import (
    FEISHU_CRAWL_PLAN,
    FeishuCrawlDriver,
)
from backend.app.services.job_discovery.crawling.crawl_plan import CrawlPlan
from backend.app.services.job_discovery.schemas import (
    PaginationType,
    RawJobListing,
)
from tests.unit.job_discovery._fixture_crawl import execute_fixture_crawl

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "job_discovery"
    / "feishu"
)


def feishu_plan() -> CrawlPlan:
    return CrawlPlan.from_yaml(FEISHU_CRAWL_PLAN)


def first_feishu_listing() -> RawJobListing:
    return FeishuCrawlDriver.from_fixture(FIXTURE).first_listing()


def test_feishu_uses_position_detail_route() -> None:
    listing = first_feishu_listing()
    assert re.search(r"/campus/position/\d+/detail", listing.detail_url or "")
    assert "/campus/position/list" not in (listing.apply_url or "")


def test_feishu_follows_total_until_last_page() -> None:
    result = execute_fixture_crawl("feishu")
    assert result.coverage.raw_listing_count == result.coverage.expected_listing_count
    assert result.coverage.completion_evidence == ["total_count_reached"]


def test_feishu_crawl_plan_yaml_parses_as_page_number() -> None:
    plan = feishu_plan()
    assert plan.pagination.type is PaginationType.PAGE_NUMBER
    assert plan.pagination.items_path == "$.data.job_post_list"
    assert plan.pagination.total_count_path == "$.data.count"
    assert plan.completion.require_all_details is True


def test_feishu_detail_resource_is_fetched_once_per_unique_job() -> None:
    """Each ``/campus/position/{id}/detail`` route is fetched exactly once."""
    result = execute_fixture_crawl("feishu")

    detail_urls = [detail.detail_url for detail in result.raw_details]
    assert len(detail_urls) == len(set(detail_urls))
    assert result.coverage.fetched_detail_count == result.coverage.total_detail_count
    assert result.coverage.failed_detail_count == 0


def test_feishu_apply_url_is_job_level_not_listing_page() -> None:
    """The apply URL must never fall back to the listing page route."""
    result = execute_fixture_crawl("feishu")
    for item in result.raw_listings:
        assert item.apply_url != item.source_url
        assert "/campus/position/list" not in (item.apply_url or "")
