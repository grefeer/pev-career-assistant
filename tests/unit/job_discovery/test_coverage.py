"""Unit tests for the deterministic CoverageVerifier.

Phase 1 of the Planner-Executor-Verifier gray migration. ``verify_coverage`` is
a pure function that proves crawl completion from pagination + detail evidence.
It is NOT wired into the global ``enforce_result_invariants`` (PATH C has no
coverage yet); the post-crawl pipeline will call it directly.
"""

from __future__ import annotations

from backend.app.services.job_discovery.crawling.coverage import (
    CoverageDecision,
    verify_coverage,
)
from backend.app.services.job_discovery.schemas import CrawlCoverage, PaginationType


def _complete_coverage() -> CrawlCoverage:
    return CrawlCoverage(
        pagination_type=PaginationType.PAGE_NUMBER,
        expected_page_count=5,
        visited_page_count=5,
        expected_listing_count=10,
        raw_listing_count=10,
        unique_listing_count=10,
        total_detail_count=10,
        fetched_detail_count=10,
        failed_detail_count=0,
        completion_evidence=["visited_all_numbered_pages"],
        coverage_complete=True,
    )


class TestVerifyCoverage:
    def test_jobs_found_does_not_mean_complete(self) -> None:
        coverage = CrawlCoverage(
            pagination_type=PaginationType.PAGE_NUMBER,
            expected_page_count=10,
            visited_page_count=4,
            raw_listing_count=80,
            unique_listing_count=70,
            total_detail_count=70,
            fetched_detail_count=70,
            failed_detail_count=0,
            completion_evidence=["visited_all_numbered_pages"],
        )
        decision = verify_coverage(coverage)
        assert decision.complete is False
        assert "page" in decision.reason.lower()

    def test_failed_detail_blocks_success(self) -> None:
        coverage = CrawlCoverage(
            pagination_type=PaginationType.PAGE_NUMBER,
            expected_page_count=1,
            visited_page_count=1,
            total_detail_count=2,
            fetched_detail_count=1,
            failed_detail_count=1,
            completion_evidence=["visited_all_numbered_pages"],
        )
        decision = verify_coverage(coverage)
        assert decision.complete is False
        assert "detail" in decision.reason.lower()

    def test_unfetched_detail_blocks_success_before_pagination_checks(self) -> None:
        coverage = CrawlCoverage(
            pagination_type=PaginationType.PAGE_NUMBER,
            expected_page_count=2,
            visited_page_count=1,
            total_detail_count=2,
            fetched_detail_count=1,
            completion_evidence=["visited_all_numbered_pages"],
        )

        decision = verify_coverage(coverage)

        assert decision.complete is False
        assert decision.reason == "fetched 1/2 detail resources"

    def test_optional_details_do_not_block_complete_coverage(self) -> None:
        coverage = CrawlCoverage(
            pagination_type=PaginationType.SINGLE_PAGE,
            total_detail_count=2,
            fetched_detail_count=0,
            failed_detail_count=2,
            require_all_details=False,
            completion_evidence=["single_page_verified"],
        )

        decision = verify_coverage(coverage)

        assert decision.complete is True

    def test_unknown_pagination_cannot_be_proven(self) -> None:
        coverage = CrawlCoverage(
            pagination_type=PaginationType.UNKNOWN,
            completion_evidence=["something"],
        )
        decision = verify_coverage(coverage)
        assert decision.complete is False

    def test_missing_completion_evidence_blocks_success(self) -> None:
        coverage = CrawlCoverage(
            pagination_type=PaginationType.PAGE_NUMBER,
            expected_page_count=1,
            visited_page_count=1,
            total_detail_count=1,
            fetched_detail_count=1,
            failed_detail_count=0,
            completion_evidence=[],
        )
        decision = verify_coverage(coverage)
        assert decision.complete is False
        assert "completion" in decision.reason.lower()

    def test_complete_coverage_passes(self) -> None:
        decision = verify_coverage(_complete_coverage())
        assert decision.complete is True
        assert decision.status == "succeeded"

    def test_returns_coverage_decision(self) -> None:
        decision = verify_coverage(_complete_coverage())
        assert isinstance(decision, CoverageDecision)

    def test_resumable_failure_is_partial_success(self) -> None:
        coverage = CrawlCoverage(
            pagination_type=PaginationType.PAGE_NUMBER,
            expected_page_count=10,
            visited_page_count=4,
            total_detail_count=70,
            fetched_detail_count=70,
            failed_detail_count=0,
            completion_evidence=["visited_all_numbered_pages"],
            resumable=True,
        )
        decision = verify_coverage(coverage)
        assert decision.complete is False
        assert decision.status == "partial_success"
