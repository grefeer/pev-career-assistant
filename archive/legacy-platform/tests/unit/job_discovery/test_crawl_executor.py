from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from backend.app.services.job_discovery.crawling import (
    crawl_executor as crawl_executor_module,
)
from backend.app.services.job_discovery.crawling.checkpoint import CrawlCheckpoint
from backend.app.services.job_discovery.crawling.crawl_executor import (
    CrawlExecutor,
    make_detail_resource_key,
    normalize_detail_url,
)
from backend.app.services.job_discovery.crawling.crawl_plan import (
    CompletionRules,
    CrawlPlan,
    DetailSchema,
    ListingSchema,
    PaginationSchema,
)
from backend.app.services.job_discovery.crawling.driver import ListingPage
from backend.app.services.job_discovery.schemas import (
    DiscoveryTaskInput,
    PaginationType,
    RawJobDetail,
    RawJobListing,
)


@dataclass
class FakeCrawlDriver:
    pages: list[ListingPage]
    details: dict[str, RawJobDetail | Exception] = field(default_factory=dict)
    detail_fetch_count: Counter[str] = field(default_factory=Counter)
    listing_fetch_count: int = 0
    listing_cursors: list[dict[str, object] | None] = field(default_factory=list)

    def fetch_listing_page(
        self,
        *,
        plan: CrawlPlan,
        task: DiscoveryTaskInput,
        cursor: dict[str, object] | None,
    ) -> ListingPage:
        self.listing_fetch_count += 1
        self.listing_cursors.append(cursor)
        return self.pages.pop(0)

    def fetch_detail(
        self,
        *,
        plan: CrawlPlan,
        listing: RawJobListing,
        resource_key: str,
    ) -> RawJobDetail:
        assert listing.detail_url is not None
        self.detail_fetch_count[listing.detail_url] += 1
        response = self.details[listing.detail_url]
        if isinstance(response, Exception):
            raise response
        return response


def task() -> DiscoveryTaskInput:
    return DiscoveryTaskInput(
        source_id="source",
        raw_record_id="raw",
        external_record_id="external",
        source_key="example",
        source_url="https://jobs.example.com/campus",
        url_hash="hash",
        record_fields=[],
    )


def crawl_plan(
    *,
    require_all_details: bool = True,
    pagination_type: PaginationType = PaginationType.SINGLE_PAGE,
) -> CrawlPlan:
    return CrawlPlan(
        version=1,
        listing=ListingSchema(item_selector=".job", title_selector=".title"),
        pagination=PaginationSchema(type=pagination_type),
        detail=DetailSchema(),
        completion=CompletionRules(require_all_details=require_all_details),
    )


def listing(
    detail_url: str | None,
    *,
    locations: list[str] | None = None,
    title: str = "Software Engineer",
    company: str = "Example",
    job_code: str | None = None,
    apply_url: str | None = None,
    recruitment_type_hint: str | None = None,
    graduation_year_hints: list[int] | None = None,
    evidence_refs: list[str] | None = None,
) -> RawJobListing:
    return RawJobListing(
        source_url="https://jobs.example.com/campus",
        detail_url=detail_url,
        company=company,
        title=title,
        locations=locations or [],
        job_code=job_code,
        apply_url=apply_url,
        recruitment_type_hint=recruitment_type_hint,
        graduation_year_hints=graduation_year_hints or [],
        evidence_refs=evidence_refs or [],
    )


def detail(detail_url: str) -> RawJobDetail:
    return RawJobDetail(detail_url=detail_url, full_text="job description")


def single_page_driver(
    listings: list[RawJobListing],
    details: dict[str, RawJobDetail | Exception],
) -> FakeCrawlDriver:
    return FakeCrawlDriver(
        pages=[
            ListingPage(
                page_key="only",
                listings=listings,
                next_cursor=None,
                terminal_evidence="single_page_verified",
            )
        ],
        details=details,
    )


def test_normalize_detail_url_removes_only_tracking_queries_and_keeps_fragment() -> None:
    left = normalize_detail_url(
        "https://jobs.example.com/job/abc?utm_source=wechat#/job/abc"
    )
    right = normalize_detail_url(
        "https://jobs.example.com/job/abc?source=friend#/job/abc"
    )

    assert left == right == "https://jobs.example.com/job/abc#/job/abc"


def test_listing_keeps_application_url_distinct_from_source_url() -> None:
    record = RawJobListing(
        source_url="https://jobs.example.com/campus",
        detail_url="https://jobs.example.com/job/1",
        apply_url="https://apply.example.com/job/1?recommendCode=abc",
        company="Example",
        title="Software Engineer",
    )

    assert record.source_url == "https://jobs.example.com/campus"
    assert record.apply_url == "https://apply.example.com/job/1?recommendCode=abc"


def test_normalize_detail_url_preserves_meaningful_referral_and_token_queries() -> None:
    normalized = normalize_detail_url(
        "https://jobs.example.com/job/abc?recommendCode=R1&"
        "external_referral_code=R2&share_token=R3&utm_medium=email"
    )

    assert "recommendCode=R1" in normalized
    assert "external_referral_code=R2" in normalized
    assert "share_token=R3" in normalized
    assert "utm_medium" not in normalized


def test_normalize_detail_url_only_strips_exact_lowercase_tracking_keys() -> None:
    normalized = normalize_detail_url(
        "https://jobs.example.com/job/abc?Source=keep&REF=keep&source=drop&ref=drop"
    )

    assert "Source=keep" in normalized
    assert "REF=keep" in normalized
    assert "source=drop" not in normalized
    assert "ref=drop" not in normalized


def test_normalize_detail_url_preserves_trailing_slash_identity() -> None:
    trailing_slash = normalize_detail_url("https://jobs.example.com/job/abc/")
    no_trailing_slash = normalize_detail_url("https://jobs.example.com/job/abc")

    assert trailing_slash == "https://jobs.example.com/job/abc/"
    assert trailing_slash != no_trailing_slash


def test_normalize_detail_url_preserves_raw_non_tracking_url_components() -> None:
    url = (
        "HTTPS://Jobs.Example.COM/Job%2FABC/?step=2&step=1&"
        "encoded=%2F&dup=a%2Bb&dup=%20&utm_source=wechat#Frag"
    )

    normalized = normalize_detail_url(url)

    assert normalized == (
        "HTTPS://Jobs.Example.COM/Job%2FABC/?step=2&step=1&"
        "encoded=%2F&dup=a%2Bb&dup=%20#Frag"
    )


def test_executor_fetches_shared_detail_once_and_merges_locations() -> None:
    first = listing("https://jobs.example.com/job/123", locations=["北京"])
    second = listing("https://jobs.example.com/job/123", locations=["上海"])
    driver = single_page_driver(
        [first, second],
        {first.detail_url: detail(first.detail_url)},
    )

    result = CrawlExecutor(driver).execute(plan=crawl_plan(), task=task())

    assert driver.detail_fetch_count[first.detail_url] == 1
    assert len(result.raw_listings) == 1
    assert result.raw_listings[0].locations == ["上海", "北京"]
    assert result.raw_details[0].detail_resource_key == make_detail_resource_key(first)
    assert result.coverage.coverage_complete is True


def test_executor_marks_missing_detail_url_as_failed_when_details_are_required() -> None:
    driver = single_page_driver([listing(None)], {})

    result = CrawlExecutor(driver).execute(plan=crawl_plan(), task=task())

    assert result.coverage.total_detail_count == 1
    assert result.coverage.failed_detail_count == 1
    assert result.coverage.coverage_complete is False
    assert result.checkpoint is not None
    assert len(result.checkpoint.failed_detail_keys) == 1


def test_executor_keeps_missing_detail_urls_as_separate_failed_resources() -> None:
    driver = single_page_driver(
        [listing(None, title="Platform"), listing(None, title="Backend")], {}
    )

    result = CrawlExecutor(driver).execute(plan=crawl_plan(), task=task())

    assert result.coverage.total_detail_count == 2
    assert result.coverage.failed_detail_count == 2
    assert result.checkpoint is not None
    assert len(result.checkpoint.failed_detail_keys) == 2


def test_executor_allows_missing_detail_url_when_details_are_optional() -> None:
    driver = single_page_driver([listing(None)], {})

    result = CrawlExecutor(driver).execute(
        plan=crawl_plan(require_all_details=False), task=task()
    )

    assert result.coverage.require_all_details is False
    assert result.coverage.failed_detail_count == 0
    assert result.coverage.coverage_complete is True
    assert result.checkpoint is None


def test_executor_refetches_legacy_key_only_completed_detail_on_resume() -> None:
    first = listing("https://jobs.example.com/job/1")
    second = listing("https://jobs.example.com/job/2")
    completed_key = make_detail_resource_key(first)
    driver = single_page_driver(
        [first, second],
        {
            first.detail_url: detail(first.detail_url),
            second.detail_url: detail(second.detail_url),
        },
    )
    checkpoint = CrawlCheckpoint(
        plan_version=1,
        source_url=task().source_url,
        completed_detail_keys=[completed_key],
    )

    result = CrawlExecutor(driver).execute(
        plan=crawl_plan(), task=task(), checkpoint=checkpoint
    )

    assert driver.detail_fetch_count[first.detail_url] == 1
    assert driver.detail_fetch_count[second.detail_url] == 1
    assert result.coverage.fetched_detail_count == 2
    assert result.coverage.coverage_complete is True
    assert [record.detail_url for record in result.raw_details] == [
        first.detail_url,
        second.detail_url,
    ]


def test_executor_restarts_legacy_cursor_without_history_payload() -> None:
    first = listing("https://jobs.example.com/job/1", title="First")
    second = listing("https://jobs.example.com/job/2", title="Second")
    checkpoint = CrawlCheckpoint(
        plan_version=1,
        source_url=task().source_url,
        pagination_cursor={"page": 2},
        visited_page_keys=["p1"],
        visited_page_fingerprints=["legacy-fingerprint"],
        completed_detail_keys=[make_detail_resource_key(first)],
        pending_detail_keys=[make_detail_resource_key(second)],
        failed_detail_keys=[make_detail_resource_key(second)],
        completion_evidence=["legacy-terminal"],
    )
    driver = FakeCrawlDriver(
        pages=[
            ListingPage("p1", [first], {"page": 2}),
            ListingPage(
                "p2",
                [second],
                None,
                terminal_evidence="last_page_reached",
            ),
        ],
        details={
            first.detail_url: detail(first.detail_url),
            second.detail_url: detail(second.detail_url),
        },
    )

    result = CrawlExecutor(driver).execute(
        plan=crawl_plan(pagination_type=PaginationType.PAGE_NUMBER),
        task=task(),
        checkpoint=checkpoint,
    )

    assert driver.listing_cursors == [None, {"page": 2}]
    assert driver.detail_fetch_count[first.detail_url] == 1
    assert driver.detail_fetch_count[second.detail_url] == 1
    assert [record.title for record in result.raw_listings] == ["First", "Second"]
    assert [record.detail_url for record in result.raw_details] == [
        first.detail_url,
        second.detail_url,
    ]
    assert result.coverage.coverage_complete is True


def test_executor_sanitizes_detail_fetch_failure_and_keeps_checkpoint() -> None:
    failed = listing("https://jobs.example.com/job/2?share_token=secret")
    resource_key = make_detail_resource_key(failed)
    driver = single_page_driver(
        [failed],
        {failed.detail_url: ConnectionError("body=private&token=secret")},
    )

    result = CrawlExecutor(driver).execute(plan=crawl_plan(), task=task())

    assert result.error == "ConnectionError: connection_error"
    assert "secret" not in result.error
    assert result.coverage.failed_detail_count == 1
    assert result.checkpoint is not None
    assert result.checkpoint.failed_detail_keys == [resource_key]
    assert result.coverage.resumable is True


def test_executor_normalizes_job_code_identity_before_fetching() -> None:
    first = listing(
        "https://jobs.example.com/job/1",
        company=" Example ",
        job_code="  ABC-1 ",
    )
    second = listing(
        "https://jobs.example.com/job/2",
        company="example",
        job_code="abc-1",
    )
    driver = single_page_driver(
        [first, second],
        {first.detail_url: detail(first.detail_url)},
    )

    result = CrawlExecutor(driver).execute(plan=crawl_plan(), task=task())

    assert len(result.raw_listings) == 1
    assert driver.detail_fetch_count[first.detail_url] == 1
    assert driver.detail_fetch_count[second.detail_url] == 0


def test_executor_merges_non_location_listing_fields_without_loss() -> None:
    first = listing(
        "https://jobs.example.com/job/1",
        job_code="ABC-1",
        recruitment_type_hint="campus",
        graduation_year_hints=[2026],
        evidence_refs=["list-a"],
    )
    second = listing(
        "https://jobs.example.com/job/1",
        job_code="abc-1",
        apply_url="https://apply.example.com/job/1",
        recruitment_type_hint="internship",
        graduation_year_hints=[2027, 2026],
        evidence_refs=["list-a", "list-b"],
    )
    driver = single_page_driver(
        [first, second],
        {first.detail_url: detail(first.detail_url)},
    )

    result = CrawlExecutor(driver).execute(plan=crawl_plan(), task=task())

    merged = result.raw_listings[0]
    assert merged.apply_url == "https://apply.example.com/job/1"
    assert merged.recruitment_type_hint == "campus"
    assert merged.graduation_year_hints == [2026, 2027]
    assert merged.job_code == "ABC-1"
    assert merged.evidence_refs == ["list-a", "list-b"]


def test_executor_resumes_pagination_and_restores_completed_records() -> None:
    first = listing("https://jobs.example.com/job/1", title="First")
    second = listing("https://jobs.example.com/job/2", title="Second")
    interrupted_driver = FakeCrawlDriver(
        pages=[ListingPage("p1", [first], {"page": 2})],
        details={first.detail_url: detail(first.detail_url)},
    )

    interrupted = CrawlExecutor(interrupted_driver).execute(
        plan=crawl_plan(pagination_type=PaginationType.PAGE_NUMBER),
        task=task(),
    )

    assert interrupted.coverage.coverage_complete is False
    assert interrupted.checkpoint is not None
    assert interrupted.checkpoint.pagination_complete is False
    assert interrupted.checkpoint.collected_listings
    assert interrupted.checkpoint.completed_details

    resumed_driver = FakeCrawlDriver(
        pages=[
            ListingPage(
                "p2",
                [second],
                None,
                terminal_evidence="last_page_reached",
            )
        ],
        details={second.detail_url: detail(second.detail_url)},
    )

    resumed = CrawlExecutor(resumed_driver).execute(
        plan=crawl_plan(pagination_type=PaginationType.PAGE_NUMBER),
        task=task(),
        checkpoint=interrupted.checkpoint,
    )

    assert resumed_driver.listing_cursors == [{"page": 2}]
    assert resumed_driver.detail_fetch_count[first.detail_url] == 0
    assert resumed_driver.detail_fetch_count[second.detail_url] == 1
    assert [record.title for record in resumed.raw_listings] == ["First", "Second"]
    assert [record.detail_url for record in resumed.raw_details] == [
        first.detail_url,
        second.detail_url,
    ]
    assert resumed.coverage.coverage_complete is True


def test_executor_retries_failed_detail_from_completed_pagination_checkpoint() -> None:
    failed = listing("https://jobs.example.com/job/failed")
    failed_driver = single_page_driver(
        [failed],
        {failed.detail_url: ConnectionError("token=secret")},
    )
    interrupted = CrawlExecutor(failed_driver).execute(plan=crawl_plan(), task=task())

    assert interrupted.checkpoint is not None
    assert interrupted.checkpoint.pagination_complete is True

    resumed_driver = FakeCrawlDriver(
        pages=[],
        details={failed.detail_url: detail(failed.detail_url)},
    )
    resumed = CrawlExecutor(resumed_driver).execute(
        plan=crawl_plan(), task=task(), checkpoint=interrupted.checkpoint
    )

    assert resumed_driver.listing_fetch_count == 0
    assert resumed_driver.detail_fetch_count[failed.detail_url] == 1
    assert resumed.raw_details[0].detail_url == failed.detail_url
    assert resumed.coverage.coverage_complete is True


def test_executor_ignores_optional_detail_fetch_failure_for_completion() -> None:
    optional = listing("https://jobs.example.com/job/optional")
    driver = single_page_driver(
        [optional],
        {optional.detail_url: ConnectionError("body=private&token=secret")},
    )

    result = CrawlExecutor(driver).execute(
        plan=crawl_plan(require_all_details=False), task=task()
    )

    assert driver.detail_fetch_count[optional.detail_url] == 1
    assert result.coverage.coverage_complete is True
    assert result.coverage.failed_detail_count == 0
    assert result.coverage.resumable is False
    assert result.checkpoint is None
    assert result.error is None


def test_executor_verifies_coverage_once_after_pagination_error(
    monkeypatch,
) -> None:
    calls = 0
    original = crawl_executor_module.verify_coverage

    def verify_once(coverage):
        nonlocal calls
        calls += 1
        return original(coverage)

    monkeypatch.setattr(crawl_executor_module, "verify_coverage", verify_once)
    driver = FakeCrawlDriver(pages=[])

    result = CrawlExecutor(driver).execute(plan=crawl_plan(), task=task())

    assert calls == 1
    assert result.error == "IndexError: index_error"
