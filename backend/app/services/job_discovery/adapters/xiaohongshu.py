"""Xiaohongshu (``job.xiaohongshu.com``) certified complete-crawl adapter.

Xiaohongshu's campus portal exposes a public search XHR (``$.data.list`` items
with a ``$.data.total`` count). Listings are produced deterministically from
that JSON -- never by the LLM -- and pagination follows the API cursor until
``nextCursor == null`` (the only positive completion signal this site emits;
the probe captured a DOM ``ant_pagination_disabled`` terminal selector rather
than a JSON cursor path, so the production driver drives the SPA's next-page
control and treats its disabled state as ``next_cursor_null``).

Each listing is one ``RawJobListing`` (one row per ``positionId``) and is
queued as a separate detail fetch -- the landing JSON blob is never handed to
``_extract_jd_candidates`` for one-shot splitting (Task 5 Step 3: the 43->1
false-negative regression). Detail URLs are ``/campus/position/{positionId}``;
detail resource keys normalize on that route and never depend on a token.

Gray rollout: the seeded strategy (``scripts/seed_strategies.py``) is
``enabled=False`` until three consecutive coverage-verified live smokes pass
(plan Task 5 Step 4). Unit tests exercise the fixture-replay driver via
``XiaohongshuCrawlDriver.from_fixture``; the production browser path is
exercised by the live smoke.
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

#: ``CrawlPlan`` consumed by ``CrawlExecutor``. API_CURSOR: the driver supplies
#: ``next_cursor`` and the ``next_cursor_null`` terminal directly; the declared
#: JSON paths are the public ``items``/``total`` fields the live driver reads.
XHS_CRAWL_PLAN = """\
plan_type: crawl_plan
version: 1
listing:
  item_selector: ""
  title_selector: ""
pagination:
  type: api_cursor
  items_path: "$.data.list"
  total_count_path: "$.data.total"
detail:
  body_selector: ""
completion:
  require_all_pages: true
  require_all_details: true
"""

TERMINAL_PROOF = "next_cursor_null"
# Captured public listing response.  This differs from Feishu's endpoint even
# though both sites expose a similarly shaped listing payload.
_SEARCH_API_MARKER = "/websiterecruit/position/pageQueryPosition"
_NEXT_PAGE_SELECTOR = ".ant-pagination-next"
_NEXT_PAGE_DISABLED_SELECTOR = ".ant-pagination-next.ant-pagination-disabled"
_BLOCKED_MARKERS = (
    "captcha",
    "验证码",
    "请先登录",
    "登录后继续",
    "扫码登录",
    "环境异常",
    "完成验证后即可继续访问",
)


class XiaohongshuBlockedError(RuntimeError):
    """A Xiaohongshu page presented a login/captcha/anti-bot wall; never bypassed."""


class XiaohongshuCrawlDriver:
    """Deterministic Xiaohongshu crawl driver (API cursor, total-driven).

    ``from_fixture`` replays the captured contract for unit tests; the
    production path (``build_driver``) drives the live search XHR across pages
    until the cursor goes null (next-page control disabled) and the declared
    total is reached.
    """

    #: Replay page size -- small enough to exercise the cursor loop with the
    #: fixture's three samples; the live driver uses the API's own page size.
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
        self._page_count = 0
        self._live_total: int | None = None
        # The public listing response already carries ``duty`` and
        # ``qualification`` for each position.  Keep that task-scoped evidence
        # so the deterministic executor does not serially re-open hundreds of
        # detail pages merely to retrieve the same JD body.
        self._live_detail_text_by_url: dict[str, str] = {}

    @classmethod
    def from_fixture(cls, fixture_dir: str | Path) -> "XiaohongshuCrawlDriver":
        contract = json.loads(
            (Path(fixture_dir) / "contract.json").read_text(encoding="utf-8")
        )
        source_url = contract["page_url"]
        samples = contract.get("sample_listings") or []
        expected = int(
            contract.get("expected_listing_count") or len(samples)
        )
        listings = [_listing_from_sample(sample, source_url) for sample in samples]
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
            raise IndexError("no Xiaohongshu listings loaded")
        return self._listings[0]

    def fetch_listing_page(
        self,
        *,
        plan: CrawlPlan,
        task: DiscoveryTaskInput,
        cursor: dict[str, Any] | str | None,
    ) -> ListingPage:
        if self._listings:
            return self._replay_page()
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

    def _replay_page(self) -> ListingPage:
        self._page_count += 1
        start = self._replay_index
        chunk = self._listings[start : start + self.replay_page_size]
        self._replay_index = start + len(chunk)
        self._replay_emitted += len(chunk)
        reached = self._replay_emitted >= (self._expected_listing_count or 0)
        return ListingPage(
            page_key=str(self._page_count),
            listings=list(chunk),
            # API cursor: null next_cursor on the last page is the ONLY positive
            # completion signal; a non-null cursor means more pages remain.
            next_cursor=None if reached else f"cursor-{self._page_count}",
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
        cursor: Any,
    ) -> ListingPage:
        page = self._get_page()
        self._page_count += 1
        if self._page_count == 1:
            payload = self._capture_search(page, task.source_url)
        else:
            next_button = page.locator(_NEXT_PAGE_SELECTOR)
            if not next_button.is_visible() or self._is_next_disabled(page):
                return ListingPage(
                    page_key=str(self._page_count),
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
        listings = []
        for item in items:
            listing = _listing_from_api_item(item, task.source_url)
            listings.append(listing)
            if listing.detail_url:
                full_text = _sample_full_text(item if isinstance(item, dict) else None)
                if full_text:
                    self._live_detail_text_by_url[listing.detail_url] = full_text
        accumulated = self._replay_emitted + len(listings)
        self._replay_emitted = accumulated
        cursor_null = self._is_next_disabled(page) or (
            self._live_total is not None and accumulated >= self._live_total
        )
        return ListingPage(
            page_key=str(self._page_count),
            listings=listings,
            next_cursor=None if cursor_null else f"cursor-{self._page_count}",
            terminal_evidence=TERMINAL_PROOF if cursor_null else None,
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

    def _is_next_disabled(self, page: Any) -> bool:
        disabled = page.locator(_NEXT_PAGE_DISABLED_SELECTOR)
        try:
            return bool(disabled.count() > 0) if hasattr(disabled, "count") else False
        except Exception:
            return False

    def _live_detail_text(self, plan: CrawlPlan, listing: RawJobListing) -> str:
        if not listing.detail_url:
            return ""
        cached = self._live_detail_text_by_url.get(listing.detail_url)
        if cached:
            return cached
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

    def _get_page(self) -> Any:
        if self._page is None:
            if self._page_factory is None:
                raise RuntimeError(
                    "XiaohongshuCrawlDriver has no fixture listings and no Playwright page"
                )
            self._page = self._page_factory()
        return self._page

    @staticmethod
    def _raise_if_blocked(page: Any) -> None:
        # Inspect rendered, user-visible text only.  The raw HTML routinely
        # contains bundled captcha/login library identifiers, which are not an
        # anti-bot wall and must not force a false blocked result.
        content = ""
        try:
            body = page.locator("body") if hasattr(page, "locator") else None
            if body is not None and hasattr(body, "inner_text"):
                content = str(body.inner_text() or "").lower()
        except Exception:
            # A missing/unreadable body is not proof of a wall. The subsequent
            # navigation/response operation will surface its own failure.
            content = ""
        if any(marker.lower() in content for marker in _BLOCKED_MARKERS):
            raise XiaohongshuBlockedError(
                "Xiaohongshu presented a login/QR/anti-bot wall"
            )


def _listing_from_sample(sample: dict[str, Any], source_url: str) -> RawJobListing:
    """Build a listing from a fixture sample (one row per ``positionId``)."""
    detail_url = _detail_url_for(source_url, sample.get("positionId"))
    return RawJobListing(
        source_url=source_url,
        detail_url=detail_url,
        apply_url=detail_url,
        company=None,
        title=str(sample.get("positionName") or ""),
        locations=_workplace_from_sample(sample),
    )


def _listing_from_api_item(item: Any, source_url: str) -> RawJobListing:
    if not isinstance(item, dict):
        return RawJobListing(
            source_url=source_url, detail_url=None, company=None, title=""
        )
    detail_url = _detail_url_for(source_url, item.get("positionId"))
    return RawJobListing(
        source_url=source_url,
        detail_url=detail_url,
        apply_url=detail_url,
        company=None,
        title=str(item.get("positionName") or ""),
        locations=_workplace_from_sample(item),
    )


def _detail_url_for(source_url: str, position_id: Any) -> str | None:
    if position_id in (None, ""):
        return None
    base = f"https://{urlparse(source_url).netloc}/"
    return urljoin(base, f"campus/position/{position_id}")


def _workplace_from_sample(sample: dict[str, Any]) -> list[str]:
    workplace = sample.get("workplace")
    if isinstance(workplace, str) and workplace.strip():
        return [workplace.strip()]
    return []


def _sample_full_text(sample: dict[str, Any] | None) -> str:
    if not sample:
        return ""
    parts = [sample.get("duty") or "", sample.get("qualification") or ""]
    return "\n".join(part for part in parts if part).strip()


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


class XiaohongshuCrawlAdapter(CompleteCrawlAdapter):
    """Certified complete-crawl adapter for Xiaohongshu (``job.xiaohongshu.com``)."""

    url_pattern = "job.xiaohongshu.com/*"

    def build_driver(
        self,
        plan: CrawlPlan,
        task: DiscoveryTaskInput,
        trajectory: TrajectoryBuffer,
    ) -> XiaohongshuCrawlDriver:
        page, close_callback = _build_playwright_page()
        return XiaohongshuCrawlDriver(
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
