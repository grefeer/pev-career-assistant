"""Unit tests for the Inovance certified complete-crawl adapter (Task 4.4).

Exercises the fixture-replay ``InovanceCrawlDriver`` against the deterministic
``CrawlExecutor`` and ``CoverageVerifier``. The driver loads the captured
``tests/fixtures/job_discovery/inovance/contract.json`` and emits every
``#/jobs/<uuid>`` hash route as a job-level listing; the executor fetches each
unique detail once and the coverage verifier proves the crawl complete. The
apply URL is left ``None`` because the ``#/jobs`` fragment is shared by the
listing page and every detail route -- the listing page is never a fallback.
"""
from __future__ import annotations

from pathlib import Path

from backend.app.services.job_discovery.adapters.inovance import (
    INOVANCE_CRAWL_PLAN,
    InovanceCrawlDriver,
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
    / "inovance"
)


def inovance_plan() -> CrawlPlan:
    return CrawlPlan.from_yaml(INOVANCE_CRAWL_PLAN)


def first_inovance_listing() -> RawJobListing:
    return InovanceCrawlDriver.from_fixture(FIXTURE).first_listing()


def test_inovance_fetches_body_for_every_unique_detail() -> None:
    result = execute_fixture_crawl("inovance")
    assert result.coverage.fetched_detail_count == result.coverage.total_detail_count
    assert all(detail.full_text.strip() for detail in result.raw_details)
    assert result.coverage.failed_detail_count == 0


def test_inovance_never_uses_jobs_hash_as_apply_url() -> None:
    result = execute_fixture_crawl("inovance")
    assert all(
        "#/jobs" not in (listing.apply_url or "").rstrip("/")
        for listing in result.raw_listings
    )


def test_inovance_crawl_plan_yaml_parses_as_single_page() -> None:
    plan = inovance_plan()
    assert plan.pagination.type is PaginationType.SINGLE_PAGE
    assert "#/jobs/" in plan.listing.item_selector
    assert plan.completion.require_all_details is True


def test_inovance_emits_job_level_hash_routes() -> None:
    listing = first_inovance_listing()
    assert "#/jobs/" in (listing.detail_url or "")
    assert listing.detail_url != listing.source_url


def test_inovance_coverage_is_complete() -> None:
    result = execute_fixture_crawl("inovance")
    assert result.coverage.raw_listing_count == result.coverage.expected_listing_count
    assert result.coverage.coverage_complete is True
    assert result.coverage.completion_evidence == ["single_page_inovance_hash_jobs"]
