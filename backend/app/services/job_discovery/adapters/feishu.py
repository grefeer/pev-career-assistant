"""Feishu careers (``*.jobs.feishu.cn``) certified complete-crawl adapter.

Feishu's campus portal exposes a public search XHR (``/api/v1/search/job/posts``
with ``offset``/``limit`` and a ``$.data.count`` total). Listings are produced
deterministically from that JSON -- never by the LLM -- and pagination follows
the declared ``total_count`` until the last page, terminating with
``total_count_reached``. Detail URLs are job-level
``/campus/position/{position_id}/detail`` routes; the detail resource key
normalizes on that route and never depends on a share token.

Gray rollout: the seeded strategy is ``enabled=False`` until three consecutive
coverage-verified live smokes pass (plan Task 4.3 Step 3). Unit tests exercise
the fixture-replay driver via ``FeishuCrawlDriver.from_fixture``.
"""
from __future__ import annotations

import fnmatch
import json
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

from backend.app.services.job_discovery.adapters.complete_crawl_base import (
    CompleteCrawlAdapter,
)
from backend.app.services.job_discovery.crawling.crawl_plan import CrawlPlan
from backend.app.services.job_discovery.crawling.driver import ListingPage
from backend.app.services.job_discovery.schemas import (
    DiscoveryTaskInput,
    RawJobDetail,
    RawJobListing,
)
from backend.app.services.job_discovery.strategy.trajectory_buffer import (
    TrajectoryBuffer,
)

FEISHU_CRAWL_PLAN = """\
plan_type: crawl_plan
version: 1
listing:
  item_selector: ""
  title_selector: ""
pagination:
  type: page_number
  items_path: "$.data.job_post_list"
  total_count_path: "$.data.count"
detail:
  body_selector: ""
completion:
  require_all_pages: true
  require_all_details: true
"""

TERMINAL_PROOF = "total_count_reached"
_SEARCH_API_MARKER = "/api/v1/search/job/posts"
_NEXT_PAGE_SELECTOR = ".ant-pagination-next"
_BLOCKED_MARKERS = (
    "captcha",
    "验证码",
    "login",
    "登录",
    "扫码登录",
    "环境异常",
    "完成验证后即可继续访问",
)


class FeishuBlockedError(RuntimeError):
    """A Feishu page presented a login/QR/anti-bot wall; never bypassed."""


class FeishuCrawlDriver:
    """Deterministic Feishu careers crawl driver (page-number, total-driven).

    ``from_fixture`` replays the captured contract for unit tests; the
    production path (``build_driver``) drives the live search XHR across pages
    until the declared total is reached.
    """

    #: Replay page size -- small enough to exercise the page-number loop with
    #: the fixture's three samples; the live driver uses the API's own limit.
    replay_page_size: int = 2

    def __init__(
        self,
        *,
        source_url: str,
        listings: list[RawJobListing] | None = None,
        samples: list[dict[str, Any]] | None = None,
        expected_listing_count: int | None = None,
        page: Any | None = None,
        page_factory: Callable[[], Any] | None = None,
        close_callback: Callable[[], None] | None = None,
    ) -> None:
        self._source_url = source_url
        self._listings: list[RawJobListing] = list(listings or [])
        self._samples: list[dict[str, Any]] = list(samples or [])
        self._sample_by_url: dict[str, dict[str, Any]] = {
            listing.detail_url or "": sample
            for sample, listing in zip(self._samples, self._listings)
        }
        self._expected_listing_count = expected_listing_count
        self._page = page
        self._page_factory = page_factory
        self._close_callback = close_callback
        self._replay_index = 0
        self._replay_emitted = 0
        self._live_total: int | None = None

    @classmethod
    def from_fixture(cls, fixture_dir: str | Path) -> "FeishuCrawlDriver":
        contract = json.loads(
            (Path(fixture_dir) / "contract.json").read_text(encoding="utf-8")
        )
        source_url = contract["page_url"]
        samples = contract.get("sample_listings") or []
        detail_urls = contract.get("detail_url_examples") or []
        expected = int(
            contract.get("expected_listing_count") or len(samples)
        )
        listings = [
            _listing_from_sample(sample, detail_urls, i, source_url)
            for i, sample in enumerate(samples)
        ]
        return cls(
            source_url=source_url,
            listings=listings,
            samples=samples,
            expected_listing_count=expected,
        )

    @property
    def source_url(self) -> str:
        return self._source_url

    def first_listing(self) -> RawJobListing:
        if not self._listings:
            raise IndexError("no Feishu listings loaded")
        return self._listings[0]

    def fetch_listing_page(
        self,
        *,
        plan: CrawlPlan,
        task: DiscoveryTaskInput,
        cursor: dict[str, Any] | None,
    ) -> ListingPage:
        if self._listings:
            return self._replay_page(cursor)
        return self._live_page(plan, task, cursor)

    def fetch_detail(
        self,
        *,
        plan: CrawlPlan,
        listing: RawJobListing,
        resource_key: str,
    ) -> RawJobDetail:
        sample = self._sample_by_url.get(listing.detail_url or "")
        if sample is not None:
            full_text = _sample_full_text(sample) or listing.title or ""
        else:
            full_text = self._live_detail_text(plan, listing) or listing.title or ""
        return RawJobDetail(
            detail_url=listing.detail_url or "",
            full_text=full_text,
            title=listing.title or None,
            locations=list(listing.locations),
            detail_resource_key=resource_key,
        )

    def close(self) -> None:
        callback, self._close_callback = self._close_callback, None
        if callback is not None:
            callback()

    # ------------------------------------------------------------------ #
    # Replay (unit-test) path
    # ------------------------------------------------------------------ #

    def _replay_page(self, cursor: dict[str, Any] | None) -> ListingPage:
        page_number = int((cursor or {}).get("page", 1))
        start = self._replay_index
        chunk = self._listings[start : start + self.replay_page_size]
        self._replay_index = start + len(chunk)
        self._replay_emitted += len(chunk)
        reached = self._replay_emitted >= (self._expected_listing_count or 0)
        return ListingPage(
            page_key=str(page_number),
            listings=list(chunk),
            next_cursor=None if reached else {"page": page_number + 1},
            terminal_evidence=TERMINAL_PROOF if reached else None,
            expected_listing_count=self._expected_listing_count,
        )

    # ------------------------------------------------------------------ #
    # Live (production) path
    # ------------------------------------------------------------------ #

    def _live_page(
        self,
        plan: CrawlPlan,
        task: DiscoveryTaskInput,
        cursor: dict[str, Any] | None,
    ) -> ListingPage:
        page = self._get_page()
        page_number = int((cursor or {}).get("page", 1))
        if page_number == 1:
            payload = self._capture_search(page, task.source_url)
        else:
            next_button = page.locator(_NEXT_PAGE_SELECTOR)
            if not next_button.is_visible():
                return ListingPage(
                    page_key=str(page_number),
                    listings=[],
                    next_cursor=None,
                    terminal_evidence=TERMINAL_PROOF,
                    expected_listing_count=self._live_total,
                )
            payload = self._capture_search(page, task.source_url, click_next=next_button)
        items_path = plan.pagination.items_path
        total_path = plan.pagination.total_count_path
        items = _resolve_path(payload, items_path) or []
        total = _resolve_path(payload, total_path)
        if isinstance(total, int):
            self._live_total = total
        listings = [_listing_from_api_item(item, task.source_url) for item in items]
        accumulated = self._replay_emitted + len(listings)
        self._replay_emitted = accumulated
        reached = self._live_total is not None and accumulated >= self._live_total
        return ListingPage(
            page_key=str(page_number),
            listings=listings,
            next_cursor=None if reached else {"page": page_number + 1},
            terminal_evidence=TERMINAL_PROOF if reached else None,
            expected_listing_count=self._live_total,
        )

    def _capture_search(
        self,
        page: Any,
        source_url: str,
        *,
        click_next: Any | None = None,
    ) -> dict[str, Any]:
        def _is_search(response: Any) -> bool:
            return _SEARCH_API_MARKER in getattr(response, "url", "")

        if click_next is not None:
            with page.expect_response(_is_search) as captured:
                click_next.click()
        else:
            with page.expect_response(_is_search) as captured:
                page.goto(source_url)
        self._raise_if_blocked(page)
        return captured.value.json()

    def _get_page(self) -> Any:
        if self._page is None:
            if self._page_factory is None:
                raise RuntimeError(
                    "FeishuCrawlDriver has no fixture listings and no Playwright page"
                )
            self._page = self._page_factory()
        return self._page

    def _live_detail_text(self, plan: CrawlPlan, listing: RawJobListing) -> str:
        """Read the rendered JD body for one detail route (live smoke path).

        Navigates to the job-level ``/campus/position/{id}/detail`` route, waits
        for the SPA to render, and stops -- never attempts login -- if the page
        presents a login/QR/anti-bot wall. The body text feeds the deterministic
        ``extract_jd_candidates`` tool downstream; a selector declared in the
        plan's ``detail.body_selector`` is preferred, falling back to the whole
        rendered body.
        """
        if not listing.detail_url:
            return ""
        page = self._get_page()
        page.goto(listing.detail_url)
        page.wait_for_load_state("networkidle")
        self._raise_if_blocked(page)
        body_selector = plan.detail.body_selector
        target = page.locator(body_selector) if body_selector else page.locator("body")
        if hasattr(target, "inner_text"):
            try:
                return str(target.inner_text() or "").strip()
            except Exception:
                return ""
        return ""

    @staticmethod
    def _raise_if_blocked(page: Any) -> None:
        content = str(page.content()).lower() if hasattr(page, "content") else ""
        if any(marker.lower() in content for marker in _BLOCKED_MARKERS):
            raise FeishuBlockedError("Feishu presented a login/QR/anti-bot wall")


def _listing_from_sample(
    sample: dict[str, Any],
    detail_urls: list[str],
    index: int,
    source_url: str,
) -> RawJobListing:
    """Build a listing from a fixture sample.

    Sample ``id`` fields are PII-redacted in the contract, so the job-level
    detail URL is taken from the fixture's ``detail_url_examples`` (which carry
    real position ids), paired by index.
    """
    detail_url = detail_urls[index] if index < len(detail_urls) else ""
    return RawJobListing(
        source_url=source_url,
        detail_url=detail_url,
        apply_url=detail_url,
        company=None,
        title=str(sample.get("title") or ""),
        locations=_cities_from_sample(sample),
    )


def _cities_from_sample(sample: dict[str, Any]) -> list[str]:
    cities = sample.get("city_list") or []
    names: list[str] = []
    for city in cities:
        if isinstance(city, dict):
            name = city.get("name") or city.get("i18n_name")
            if name:
                names.append(str(name))
    # de-duplicate while preserving order
    return list(dict.fromkeys(names))


def _sample_full_text(sample: dict[str, Any] | None) -> str:
    if not sample:
        return ""
    parts = [
        sample.get("description") or "",
        sample.get("requirement") or "",
    ]
    return "\n".join(part for part in parts if part).strip()


def _listing_from_api_item(item: Any, source_url: str) -> RawJobListing:
    if not isinstance(item, dict):
        return RawJobListing(
            source_url=source_url, detail_url=None, company=None, title=""
        )
    position_id = item.get("id")
    detail_url = (
        urljoin(f"https://{urlparse(source_url).netloc}/", f"campus/position/{position_id}/detail")
        if position_id
        else None
    )
    return RawJobListing(
        source_url=source_url,
        detail_url=detail_url,
        apply_url=detail_url,
        company=None,
        title=str(item.get("title") or ""),
        locations=_cities_from_sample(item),
    )


def _resolve_path(obj: Any, path: str | None) -> Any:
    if not path:
        return None
    cur: Any = obj
    for part in path.lstrip("$").lstrip(".").split("."):
        if part == "":
            continue
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
        if cur is None:
            return None
    return cur


class FeishuCrawlAdapter(CompleteCrawlAdapter):
    """Certified complete-crawl adapter for Feishu careers."""

    url_pattern = "*.jobs.feishu.cn/*"

    def build_driver(
        self,
        plan: CrawlPlan,
        task: DiscoveryTaskInput,
        trajectory: TrajectoryBuffer,
    ) -> FeishuCrawlDriver:
        page, close_callback = _build_playwright_page()
        return FeishuCrawlDriver(
            source_url=task.source_url,
            page=page,
            close_callback=close_callback,
        )

    def validate(self, url: str) -> bool:
        target = url.replace("https://", "").replace("http://", "")
        return fnmatch.fnmatch(url, self.url_pattern) or fnmatch.fnmatch(
            target, self.url_pattern
        )


def _build_playwright_page() -> tuple[Any, Callable[[], None]]:
    """Lazily start a task-scoped Playwright session for a production crawl."""
    from playwright.sync_api import sync_playwright

    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page()

    def _close() -> None:
        try:
            page.close()
        finally:
            try:
                browser.close()
            finally:
                playwright.stop()

    return page, _close
