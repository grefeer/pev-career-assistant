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

import pytest

from backend.app.services.job_discovery.adapters.inovance import (
    INOVANCE_CRAWL_PLAN,
    InovanceBlockedError,
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


def test_inovance_block_detection_ignores_normal_login_navigation() -> None:
    class _Body:
        def inner_text(self) -> str:
            return "首页\n社会招聘\n登录 / 注册\n共有 139 个在招职位"

    class _Page:
        def locator(self, selector: str) -> _Body:
            assert selector == "body"
            return _Body()

    InovanceCrawlDriver._raise_if_blocked(_Page())


def test_inovance_block_detection_rejects_explicit_login_wall() -> None:
    class _Body:
        def inner_text(self) -> str:
            return "请先登录后继续访问"

    class _Page:
        def locator(self, selector: str) -> _Body:
            return _Body()

    with pytest.raises(InovanceBlockedError):
        InovanceCrawlDriver._raise_if_blocked(_Page())


def test_inovance_retries_one_transient_detail_navigation_failure() -> None:
    driver = InovanceCrawlDriver(source_url="https://recruit.inovance.com/#/jobs")
    attempts = 0

    def _temporary_failure(plan, detail_url):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary navigation failure")
        return "职责\n要求"

    driver._fetch_detail_text = _temporary_failure  # type: ignore[method-assign]
    listing = RawJobListing(
        source_url=driver.source_url,
        detail_url="https://recruit.inovance.com/#/jobs/example",
        company=None,
        title="职位",
    )

    result = driver.fetch_detail(
        plan=inovance_plan(), listing=listing, resource_key="resource"
    )

    assert attempts == 2
    assert result.full_text == "职责\n要求"
