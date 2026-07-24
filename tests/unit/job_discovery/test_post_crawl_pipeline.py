from __future__ import annotations

from backend.app.services.job_discovery.crawling.crawl_executor import CrawlExecutionResult
from backend.app.services.job_discovery.crawling.checkpoint import CrawlCheckpoint
from backend.app.services.job_discovery.post_crawl_pipeline import run_post_crawl_pipeline
from backend.app.services.job_discovery.schemas import (
    CrawlCoverage,
    DiscoveryTaskInput,
    PaginationType,
    RawJobDetail,
    RawJobListing,
)


def _task() -> DiscoveryTaskInput:
    return DiscoveryTaskInput(
        source_id="source",
        raw_record_id="record",
        external_record_id="external",
        source_key="test-source",
        source_url="https://jobs.example.test/list",
        url_hash="hash",
        record_fields=[],
    )


def _crawl_result(*, complete: bool) -> CrawlExecutionResult:
    listing = RawJobListing(
        source_url="https://jobs.example.test/list",
        detail_url="https://jobs.example.test/jobs/1",
        apply_url=None,
        company="Listing Company",
        title="Listing Title",
        locations=["上海"],
    )
    detail = RawJobDetail(
        detail_url=listing.detail_url,
        full_text="岗位职责：构建可靠服务。任职要求：熟悉 Python 和系统设计。",
    )
    coverage = CrawlCoverage(
        pagination_type=PaginationType.SINGLE_PAGE,
        visited_page_count=1,
        raw_listing_count=1,
        unique_listing_count=1,
        total_detail_count=1,
        fetched_detail_count=1 if complete else 0,
        failed_detail_count=0 if complete else 1,
        completion_evidence=["terminal"],
        resumable=not complete,
    )
    return CrawlExecutionResult(
        raw_listings=[listing],
        raw_details=[detail],
        coverage=coverage,
        checkpoint=CrawlCheckpoint(1, _task().source_url) if not complete else None,
    )


def test_incomplete_crawl_keeps_fetched_detail_candidate_but_not_verified_success() -> None:
    result = run_post_crawl_pipeline(_task(), _crawl_result(complete=False))

    assert result.status == "partial_success"
    assert result.block_reason == "1 detail pages failed"
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.company_name == "Listing Company"
    assert candidate.title == "Listing Title"
    assert candidate.locations == ["上海"]
    assert candidate.apply_url == "https://jobs.example.test/jobs/1"
    assert candidate.evidence_refs


def test_complete_zero_listing_crawl_succeeds_without_candidates() -> None:
    coverage = CrawlCoverage(
        pagination_type=PaginationType.SINGLE_PAGE,
        visited_page_count=1,
        raw_listing_count=0,
        unique_listing_count=0,
        total_detail_count=0,
        fetched_detail_count=0,
        completion_evidence=["empty_listing_terminal"],
    )
    crawl_result = CrawlExecutionResult([], [], coverage, None)

    result = run_post_crawl_pipeline(_task(), crawl_result)

    assert result.status == "succeeded"
    assert result.candidates == []
    assert result.coverage is coverage


def test_post_crawl_pipeline_preserves_sanitized_execution_error() -> None:
    crawl_result = _crawl_result(complete=True)
    crawl_result.error = "captcha"

    result = run_post_crawl_pipeline(_task(), crawl_result)

    assert result.execution_error == "captcha"
