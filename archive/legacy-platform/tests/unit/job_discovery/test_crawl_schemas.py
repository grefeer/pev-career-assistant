"""Unit tests for full-crawl domain contracts (CrawlPlan + raw records + coverage).

Phase 1 of the Planner-Executor-Verifier gray migration. These types are
additive -- existing PATH C / SnapshotExecutor production paths are untouched
(``enforce_result_invariants`` is NOT modified globally; PATH C has no
``coverage`` yet). The deterministic CrawlExecutor and CoverageVerifier defined
later consume these contracts.
"""

from __future__ import annotations

import pytest

from backend.app.services.job_discovery.crawling.crawl_plan import CrawlPlan
from backend.app.services.job_discovery.schemas import (
    CrawlCoverage,
    DiscoveryRunResult,
    PaginationType,
    RawJobDetail,
    RawJobListing,
    RecruitmentScope,
)


class TestRecruitmentScope:
    def test_defaults_to_2027_campus(self) -> None:
        scope = RecruitmentScope()
        assert scope.recruitment_type == "campus"
        assert scope.graduation_year == 2027

    def test_social_scope_forces_graduation_year_to_none(self) -> None:
        scope = RecruitmentScope(recruitment_type="social", graduation_year=2027)
        assert scope.graduation_year is None

    def test_internship_requires_graduation_year(self) -> None:
        with pytest.raises(ValueError, match="graduation_year"):
            RecruitmentScope(recruitment_type="internship", graduation_year=None)


class TestCrawlCoverage:
    def test_defaults_are_incomplete(self) -> None:
        coverage = CrawlCoverage(pagination_type=PaginationType.PAGE_NUMBER)
        assert coverage.coverage_complete is False
        assert coverage.visited_page_count == 0
        assert coverage.completion_evidence == []

    def test_keeps_resume_cursor(self) -> None:
        coverage = CrawlCoverage(
            pagination_type=PaginationType.API_CURSOR,
            visited_page_count=3,
            resumable=True,
            resume_cursor={"cursor": "next-3"},
        )
        assert coverage.resume_cursor == {"cursor": "next-3"}
        assert coverage.coverage_complete is False


class TestRawRecords:
    def test_listing_defaults(self) -> None:
        listing = RawJobListing(
            source_url="https://x/jobs",
            detail_url=None,
            company="C",
            title="T",
        )
        assert listing.locations == []
        assert listing.evidence_refs == []

    def test_detail_defaults(self) -> None:
        detail = RawJobDetail(detail_url="https://x/job/1", full_text="body")
        assert detail.structured_fields == {}
        assert detail.evidence_refs == []


class TestDiscoveryRunResultCoverage:
    def test_coverage_defaults_to_none(self) -> None:
        result = DiscoveryRunResult(status="succeeded")
        assert result.coverage is None

    def test_accepts_coverage(self) -> None:
        coverage = CrawlCoverage(
            pagination_type=PaginationType.SINGLE_PAGE,
            coverage_complete=True,
        )
        result = DiscoveryRunResult(status="succeeded", coverage=coverage)
        assert result.coverage is coverage


_PAGE_NUMBER_PLAN = """
plan_type: crawl_plan
version: 1
listing:
  item_selector: ".job-card"
  title_selector: ".job-title"
  detail_link_selector: "a@href"
pagination:
  type: page_number
  page_selector: ".pagination-item"
  next_selector: ".pagination-next"
detail:
  title_selector: "h1"
  body_selector: ".job-description"
completion:
  require_all_pages: true
  require_all_details: true
"""


class TestCrawlPlanParsing:
    def test_parse_page_number_plan(self) -> None:
        plan = CrawlPlan.from_yaml(_PAGE_NUMBER_PLAN)
        assert plan.pagination.type == PaginationType.PAGE_NUMBER
        assert plan.completion.require_all_pages is True
        assert plan.listing.item_selector == ".job-card"

    def test_unsupported_version_rejected(self) -> None:
        bad = _PAGE_NUMBER_PLAN.replace("version: 1", "version: 2")
        with pytest.raises(ValueError, match="version"):
            CrawlPlan.from_yaml(bad)

    def test_wrong_plan_type_rejected(self) -> None:
        bad = _PAGE_NUMBER_PLAN.replace(
            "plan_type: crawl_plan", "plan_type: snapshot_plan"
        )
        with pytest.raises(ValueError, match="plan_type"):
            CrawlPlan.from_yaml(bad)

    def test_infinite_scroll_requires_terminal_signal(self) -> None:
        plan_yaml = """
plan_type: crawl_plan
version: 1
listing:
  item_selector: ".job"
  title_selector: ".title"
pagination:
  type: infinite_scroll
detail:
  body_selector: ".jd"
completion:
  require_all_pages: true
  require_all_details: true
"""
        with pytest.raises(ValueError, match="terminal"):
            CrawlPlan.from_yaml(plan_yaml)

    def test_infinite_scroll_with_has_more_path_accepted(self) -> None:
        plan_yaml = """
plan_type: crawl_plan
version: 1
listing:
  item_selector: ".job"
  title_selector: ".title"
pagination:
  type: infinite_scroll
  has_more_path: "data.hasMore"
detail:
  body_selector: ".jd"
completion:
  require_all_pages: true
  require_all_details: true
"""
        plan = CrawlPlan.from_yaml(plan_yaml)
        assert plan.pagination.type == PaginationType.INFINITE_SCROLL
        assert plan.pagination.has_more_path == "data.hasMore"
