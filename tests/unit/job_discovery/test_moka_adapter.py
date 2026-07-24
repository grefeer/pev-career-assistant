"""Unit tests for the Moka certified complete-crawl adapter (Task 4.2).

Exercises the fixture-replay ``MokaCrawlDriver`` against the deterministic
``CrawlExecutor`` and ``CoverageVerifier``. The driver loads the captured
``tests/fixtures/job_discovery/moka/contract.json`` and emits every
``#/job/<uuid>`` hash route as a job-level listing; the executor fetches each
unique detail once and the coverage verifier proves the crawl complete.
"""
from __future__ import annotations

from pathlib import Path

from backend.app.services.job_discovery.adapters.moka import (
    MOKA_CRAWL_PLAN,
    MokaCrawlDriver,
)
from backend.app.services.job_discovery.crawling.crawl_executor import (
    CrawlExecutor,
    normalize_detail_url,
)
from backend.app.services.job_discovery.crawling.crawl_plan import CrawlPlan
from backend.app.services.job_discovery.schemas import (
    DiscoveryTaskInput,
    RawJobListing,
)

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "job_discovery"
    / "moka"
)


def deeprute_task() -> DiscoveryTaskInput:
    driver = MokaCrawlDriver.from_fixture(FIXTURE)
    return DiscoveryTaskInput(
        source_id="deeprute",
        raw_record_id="raw-1",
        external_record_id="deeproute-145894",
        source_key="deeprute",
        source_url=driver.source_url,
        url_hash="deeprute-hash",
        record_fields=[],
    )


def moka_plan() -> CrawlPlan:
    return CrawlPlan.from_yaml(MOKA_CRAWL_PLAN)


def first_moka_listing() -> RawJobListing:
    return MokaCrawlDriver.from_fixture(FIXTURE).first_listing()


def test_moka_emits_every_listing_with_job_level_url() -> None:
    driver = MokaCrawlDriver.from_fixture(FIXTURE)
    result = CrawlExecutor(driver).execute(plan=moka_plan(), task=deeprute_task())

    assert result.coverage.raw_listing_count == result.coverage.expected_listing_count
    assert result.coverage.failed_detail_count == 0
    assert all(item.detail_url for item in result.raw_listings)
    assert all(item.apply_url != item.source_url for item in result.raw_listings)


def test_moka_hash_routes_survive_normalization() -> None:
    listing = first_moka_listing()
    assert "#/job/" in normalize_detail_url(listing.detail_url)


def test_moka_crawl_plan_yaml_parses_as_single_page() -> None:
    plan = moka_plan()
    from backend.app.services.job_discovery.schemas import PaginationType

    assert plan.pagination.type is PaginationType.SINGLE_PAGE
    assert plan.completion.require_all_details is True


def test_moka_detail_resource_is_fetched_once_per_unique_job() -> None:
    """Each ``#/job/`` route is a unique detail resource fetched exactly once."""
    driver = MokaCrawlDriver.from_fixture(FIXTURE)
    result = CrawlExecutor(driver).execute(plan=moka_plan(), task=deeprute_task())

    detail_urls = [detail.detail_url for detail in result.raw_details]
    assert len(detail_urls) == len(set(detail_urls))
    assert result.coverage.fetched_detail_count == result.coverage.total_detail_count
    assert result.coverage.coverage_complete is True
