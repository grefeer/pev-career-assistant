from __future__ import annotations

import pytest

from backend.app.services.job_discovery.crawling.crawl_plan import (
    CompletionRules,
    CrawlPlan,
    DetailSchema,
    ListingSchema,
    PaginationSchema,
)
from backend.app.services.job_discovery.crawling.playwright_driver import (
    ApiPayloadChangedError,
    PlaywrightCrawlDriver,
    UnsafePlanExecutionError,
)
from backend.app.services.job_discovery.schemas import (
    DiscoveryTaskInput,
    PaginationType,
    RawJobListing,
)


class _FakeLocator:
    def __init__(self, text: str = "", href: str | None = None, children=None):
        self.text = text
        self.href = href
        self.children = children or {}

    def all(self):
        return self.children.get("all", [])

    def locator(self, selector: str):
        return self.children.get(selector, _FakeLocator())

    def inner_text(self):
        return self.text

    def get_attribute(self, name: str):
        return self.href if name == "href" else None


class _FakePage:
    def __init__(self, payloads=None):
        self.payloads = payloads or {}
        self.gotos: list[str] = []
        self.context = _FakeContext(self.payloads)

    def goto(self, url: str):
        self.gotos.append(url)

    def content(self):
        return "ordinary job board"

    def locator(self, selector: str):
        return _FakeLocator()


class _FakeResponse:
    ok = True

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _FakeRequest:
    def __init__(self, payloads):
        self.payloads = payloads
        self.urls: list[str] = []

    def get(self, url: str):
        self.urls.append(url)
        return _FakeResponse(self.payloads[url])


class _FakeContext:
    def __init__(self, payloads):
        self.request = _FakeRequest(payloads)


def _task() -> DiscoveryTaskInput:
    return DiscoveryTaskInput(
        source_id="source", raw_record_id="raw", external_record_id="external",
        source_key="source", source_url="https://jobs.example.test/list",
        url_hash="hash", record_fields=[],
    )


def _plan(*, items_path: str = "data.items") -> CrawlPlan:
    return CrawlPlan(
        version=1,
        listing=ListingSchema(item_selector=".job", title_selector=".title"),
        pagination=PaginationSchema(
            type=PaginationType.API_CURSOR,
            endpoint_pattern="https://jobs.example.test/api?cursor={cursor}",
            items_path=items_path,
            has_more_path="data.has_more",
            next_cursor_path="data.next_cursor",
        ),
        detail=DetailSchema(body_selector=".description"),
        completion=CompletionRules(),
    )


def test_api_cursor_uses_only_declared_json_paths() -> None:
    page = _FakePage({
        "https://jobs.example.test/api?cursor=": {
            "data": {
                "items": [{"title": "Engineer", "detail_url": "/jobs/1"}],
                "has_more": False,
                "next_cursor": None,
            }
        }
    })

    result = PlaywrightCrawlDriver(page).fetch_listing_page(
        plan=_plan(), task=_task(), cursor=None
    )

    assert result.listings[0].title == "Engineer"
    assert result.listings[0].detail_url == "https://jobs.example.test/jobs/1"
    assert result.terminal_evidence == "api_cursor_exhausted"
    assert page.context.request.urls == ["https://jobs.example.test/api?cursor="]


def test_missing_declared_api_json_path_is_a_structural_error() -> None:
    page = _FakePage({"https://jobs.example.test/api?cursor=": {"data": {}}})

    with pytest.raises(ApiPayloadChangedError):
        PlaywrightCrawlDriver(page).fetch_listing_page(
            plan=_plan(), task=_task(), cursor=None
        )


def test_cross_origin_detail_url_is_rejected_before_navigation() -> None:
    page = _FakePage()
    listing = RawJobListing(
        source_url=_task().source_url,
        detail_url="https://attacker.example/jobs/1",
        company="Example",
        title="Engineer",
    )

    with pytest.raises(UnsafePlanExecutionError):
        PlaywrightCrawlDriver(page).fetch_detail(
            plan=_plan(), listing=listing, resource_key="resource"
        )


def test_single_page_empty_listing_has_positive_declared_completion() -> None:
    plan = CrawlPlan(
        version=1,
        listing=ListingSchema(item_selector=".job", title_selector=".title"),
        pagination=PaginationSchema(type=PaginationType.SINGLE_PAGE),
        detail=DetailSchema(body_selector=".description"),
        completion=CompletionRules(),
    )

    result = PlaywrightCrawlDriver(_FakePage()).fetch_listing_page(
        plan=plan, task=_task(), cursor=None
    )

    assert result.listings == []
    assert result.terminal_evidence == "single_page_declared"
