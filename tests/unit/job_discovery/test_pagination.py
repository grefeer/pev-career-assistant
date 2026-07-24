from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from backend.app.services.job_discovery.crawling.checkpoint import CrawlCheckpoint
from backend.app.services.job_discovery.crawling.crawl_plan import (
    CompletionRules,
    CrawlPlan,
    DetailSchema,
    ListingSchema,
    PaginationSchema,
)
from backend.app.services.job_discovery.crawling.driver import ListingPage
from backend.app.services.job_discovery.crawling.pagination import (
    CompletionUnverifiedError,
    CrawlBudgetExhausted,
    PaginationLoopError,
    UnsupportedPaginationError,
    iterate_pages,
    page_fingerprint,
)
from backend.app.services.job_discovery.schemas import (
    DiscoveryTaskInput,
    PaginationType,
    RawJobListing,
)


@dataclass
class FakeDriver:
    pages: list[ListingPage]
    calls: list[dict[str, object] | None] = field(default_factory=list)

    def fetch_listing_page(
        self,
        *,
        plan: CrawlPlan,
        task: DiscoveryTaskInput,
        cursor: dict[str, object] | None,
    ) -> ListingPage:
        self.calls.append(cursor)
        return self.pages.pop(0)

    def fetch_detail(self, **kwargs: object) -> object:
        raise AssertionError("detail fetching is outside pagination")


@dataclass
class FakeTrajectory:
    steps: list[tuple[object, ...]] = field(default_factory=list)

    def record_step(self, *args: object) -> None:
        self.steps.append(args)


def listing(identifier: str) -> RawJobListing:
    return RawJobListing(
        source_url="https://jobs.example.com/campus",
        detail_url=f"https://jobs.example.com/jobs/{identifier}",
        company="Example",
        title=f"Job {identifier}",
        source_record_key=identifier,
    )


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


def plan_for(pagination_type: PaginationType) -> CrawlPlan:
    return CrawlPlan(
        version=1,
        listing=ListingSchema(item_selector=".job", title_selector=".title"),
        pagination=PaginationSchema(type=pagination_type),
        detail=DetailSchema(),
        completion=CompletionRules(),
    )


def page_number_plan() -> CrawlPlan:
    return plan_for(PaginationType.PAGE_NUMBER)


def api_cursor_plan() -> CrawlPlan:
    return plan_for(PaginationType.API_CURSOR)


def test_page_number_requires_positive_terminal_evidence() -> None:
    driver = FakeDriver(
        pages=[
            ListingPage("p1", [listing("1")], {"page": 2}),
            ListingPage("p2", [listing("2")], None),
        ],
    )

    with pytest.raises(CompletionUnverifiedError):
        list(iterate_pages(page_number_plan(), task(), driver))


def test_repeated_page_fingerprint_raises_loop_error() -> None:
    repeated = ListingPage("same", [listing("1")], {"page": 2})
    driver = FakeDriver(pages=[repeated, repeated])

    with pytest.raises(PaginationLoopError):
        list(iterate_pages(page_number_plan(), task(), driver))


def test_terminal_page_with_repeated_fingerprint_completes() -> None:
    driver = FakeDriver(
        pages=[
            ListingPage("p1", [listing("1")], {"page": 2}),
            ListingPage(
                "p2",
                [listing("1")],
                None,
                terminal_evidence="last_page_reached",
            ),
        ],
    )

    pages = list(iterate_pages(page_number_plan(), task(), driver))

    assert [page.page_key for page in pages] == ["p1", "p2"]


def test_terminal_page_with_repeated_page_key_completes() -> None:
    driver = FakeDriver(
        pages=[
            ListingPage("same", [listing("1")], {"page": 2}),
            ListingPage(
                "same",
                [listing("2")],
                None,
                terminal_evidence="last_page_reached",
            ),
        ],
    )

    pages = list(iterate_pages(page_number_plan(), task(), driver))

    assert [page.page_key for page in pages] == ["same", "same"]


def test_api_cursor_accepts_next_cursor_null() -> None:
    driver = FakeDriver(
        pages=[
            ListingPage("c1", [listing("1")], {"cursor": "c2"}),
            ListingPage(
                "c2",
                [listing("2")],
                None,
                terminal_evidence="next_cursor_null",
            ),
        ],
    )

    pages = list(iterate_pages(api_cursor_plan(), task(), driver))

    assert [page.page_key for page in pages] == ["c1", "c2"]
    assert pages[-1].terminal_evidence == "next_cursor_null"


def test_single_page_requires_positive_terminal_evidence() -> None:
    driver = FakeDriver(pages=[ListingPage("only", [listing("1")], None)])

    with pytest.raises(CompletionUnverifiedError):
        list(iterate_pages(plan_for(PaginationType.SINGLE_PAGE), task(), driver))


def test_single_page_rejects_whitespace_only_terminal_evidence() -> None:
    driver = FakeDriver(
        pages=[ListingPage("only", [listing("1")], None, terminal_evidence="  ")]
    )

    with pytest.raises(CompletionUnverifiedError):
        list(iterate_pages(plan_for(PaginationType.SINGLE_PAGE), task(), driver))


def test_unsupported_pagination_type_raises_explicit_error() -> None:
    driver = FakeDriver(pages=[])

    with pytest.raises(UnsupportedPaginationError):
        list(iterate_pages(plan_for(PaginationType.NEXT_BUTTON), task(), driver))


def test_checkpoint_and_trajectory_update_after_each_page() -> None:
    checkpoint = CrawlCheckpoint(plan_version=1, source_url=task().source_url)
    trajectory = FakeTrajectory()
    driver = FakeDriver(
        pages=[
            ListingPage(
                "only",
                [listing("1")],
                None,
                terminal_evidence="single_page_verified",
            ),
        ],
    )

    pages = list(
        iterate_pages(
            plan_for(PaginationType.SINGLE_PAGE),
            task(),
            driver,
            checkpoint=checkpoint,
            trajectory=trajectory,
        )
    )

    assert pages[0].page_key == "only"
    assert checkpoint.visited_page_keys == ["only"]
    assert checkpoint.pending_detail_keys == ["1"]
    assert checkpoint.pagination_cursor is None
    assert trajectory.steps == [
        (
            "crawl_page",
            "ok",
            {"cursor": None},
            {"page_key": "only", "listing_count": 1},
        )
    ]


def test_resume_rejects_listing_fingerprint_seen_in_checkpoint() -> None:
    prior_page = ListingPage("previous-key", [listing("1")], {"page": 2})
    checkpoint = CrawlCheckpoint(
        plan_version=1,
        source_url=task().source_url,
        pagination_cursor={"page": 2},
        visited_page_keys=["previous-key"],
        visited_page_fingerprints=[page_fingerprint(prior_page)],
    )
    driver = FakeDriver(
        pages=[
            ListingPage(
                "new-key",
                [listing("1")],
                {"page": 3},
            )
        ]
    )

    with pytest.raises(PaginationLoopError):
        list(iterate_pages(page_number_plan(), task(), driver, checkpoint=checkpoint))


def test_completed_and_failed_details_are_not_readded_to_pending() -> None:
    checkpoint = CrawlCheckpoint(
        plan_version=1,
        source_url=task().source_url,
        completed_detail_keys=["1"],
        failed_detail_keys=["2"],
    )
    driver = FakeDriver(
        pages=[
            ListingPage(
                "only",
                [listing("1"), listing("2"), listing("3")],
                None,
                terminal_evidence="single_page_verified",
            )
        ]
    )

    list(
        iterate_pages(
            plan_for(PaginationType.SINGLE_PAGE),
            task(),
            driver,
            checkpoint=checkpoint,
        )
    )

    assert checkpoint.pending_detail_keys == ["3"]


def test_page_budget_raises_with_resume_checkpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "backend.app.services.job_discovery.crawling.pagination.MAX_PAGE_BUDGET",
        1,
    )
    driver = FakeDriver(
        pages=[
            ListingPage("p1", [listing("1")], {"page": 2}),
            ListingPage("p2", [listing("2")], {"page": 3}),
        ],
    )

    with pytest.raises(CrawlBudgetExhausted) as exc_info:
        list(iterate_pages(page_number_plan(), task(), driver))

    assert exc_info.value.checkpoint.visited_page_keys == ["p1"]
    assert exc_info.value.checkpoint.pagination_cursor == {"page": 2}
