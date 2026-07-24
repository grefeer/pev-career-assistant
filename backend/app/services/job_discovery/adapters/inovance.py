"""Inovance (``recruit.inovance.com``) certified complete-crawl adapter.

Inovance's career site is a single-page SPA whose listing page is itself the
``#/jobs`` hash route; each job is a ``#/jobs/<uuid>`` detail route rendered
in the DOM. Listings are produced deterministically from those rendered hash
routes -- never by the LLM -- and the crawl is a single page, terminated by the
fixture-proven ``single_page_inovance_hash_jobs`` marker plus the reached
``expected_listing_count``.

The listing page URL (``.../#/jobs``) and the detail routes
(``.../#/jobs/<uuid>``) both carry the ``#/jobs`` fragment, so the apply URL is
left ``None`` rather than falling back to the listing page (Task 4.4 rule: the
listing page is never a fallback apply URL). Detail resource keys normalize on
the ``#/jobs/<uuid>`` route and never depend on a share token.

Gray rollout: the seeded strategy (``scripts/seed_strategies.py``) is
``enabled=False`` until three consecutive coverage-verified live smokes pass
(plan Task 4.4 Step 3). Unit tests exercise the fixture-replay driver via
``InovanceCrawlDriver.from_fixture``; the production browser path is exercised
by the live smoke.
"""
from __future__ import annotations

import fnmatch
import json
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin

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

#: ``CrawlPlan`` consumed by ``CrawlExecutor``. SINGLE_PAGE sites need no
#: pagination terminal path; the driver supplies ``terminal_evidence`` and
#: ``expected_listing_count`` directly from the rendered DOM / fixture.
INOVANCE_CRAWL_PLAN = """\
plan_type: crawl_plan
version: 1
listing:
  item_selector: "a[href*='#/jobs/']"
  title_selector: ""
  detail_link_selector: "href"
pagination:
  type: single_page
detail:
  body_selector: "main"
completion:
  require_all_pages: true
  require_all_details: true
"""

#: The fragment that identifies a job-level (not a listing-level) Inovance route.
#: Plural ``jobs`` distinguishes Inovance from Moka's singular ``#/job/``.
JOB_ROUTE_MARKER = "#/jobs/"
TERMINAL_PROOF = "single_page_inovance_hash_jobs"

#: Markers that mean the page presented a login/captcha/anti-bot wall. The
#: driver must stop and surface a blocked error -- never attempt to bypass.
_BLOCKED_MARKERS = (
    "captcha",
    "验证码",
    "login",
    "登录",
    "环境异常",
    "完成验证后即可继续访问",
)


class InovanceBlockedError(RuntimeError):
    """An Inovance page presented a login/captcha/anti-bot wall; never bypassed."""


class InovanceCrawlDriver:
    """Deterministic Inovance career-site crawl driver (single page, hash routes).

    Two construction modes:

    * ``from_fixture`` -- fixture replay for unit tests: loads
      ``contract.json``, emits the captured ``sample_listings`` (each a
      ``#/jobs/<uuid>`` route) as ``RawJobListing`` rows, and terminates with
      the fixture's ``single_page_proof``. No browser is touched.
    * Production -- ``InovanceCrawlAdapter.build_driver`` supplies a Playwright
      ``page``; ``fetch_listing_page`` navigates the landing URL and extracts
      ``#/jobs/<uuid>`` hash routes from the rendered DOM.
    """

    terminal_proof: str = TERMINAL_PROOF

    def __init__(
        self,
        *,
        source_url: str,
        listings: list[RawJobListing] | None = None,
        expected_listing_count: int | None = None,
        terminal_proof: str | None = None,
        page: Any | None = None,
        page_factory: Callable[[], Any] | None = None,
        close_callback: Callable[[], None] | None = None,
    ) -> None:
        self._source_url = source_url
        self._listings: list[RawJobListing] = list(listings or [])
        self._expected_listing_count = expected_listing_count
        self._terminal_proof = terminal_proof or self.terminal_proof
        self._page = page
        self._page_factory = page_factory
        self._close_callback = close_callback

    @classmethod
    def from_fixture(cls, fixture_dir: str | Path) -> "InovanceCrawlDriver":
        """Build a fixture-replay driver from a captured ``contract.json``."""
        contract = json.loads(
            (Path(fixture_dir) / "contract.json").read_text(encoding="utf-8")
        )
        source_url = contract["page_url"]
        sample_listings = contract.get("sample_listings") or []
        expected = int(
            contract.get("expected_listing_count") or len(sample_listings)
        )
        proof = contract.get("single_page_proof") or cls.terminal_proof
        listings = [
            _listing_from_sample(sample, source_url) for sample in sample_listings
        ]
        return cls(
            source_url=source_url,
            listings=listings,
            expected_listing_count=expected,
            terminal_proof=proof,
        )

    @property
    def source_url(self) -> str:
        return self._source_url

    def first_listing(self) -> RawJobListing:
        if not self._listings:
            raise IndexError("no Inovance listings loaded")
        return self._listings[0]

    def fetch_listing_page(
        self,
        *,
        plan: CrawlPlan,
        task: DiscoveryTaskInput,
        cursor: dict[str, Any] | None,
    ) -> ListingPage:
        listings = self._collect_listings(plan, task.source_url)
        return ListingPage(
            page_key="1",
            listings=listings,
            next_cursor=None,
            terminal_evidence=self._terminal_proof,
            expected_listing_count=self._expected_listing_count,
        )

    def fetch_detail(
        self,
        *,
        plan: CrawlPlan,
        listing: RawJobListing,
        resource_key: str,
    ) -> RawJobDetail:
        detail_url = listing.detail_url or ""
        full_text = self._fetch_detail_text(plan, detail_url) or listing.title or ""
        return RawJobDetail(
            detail_url=detail_url,
            full_text=full_text,
            title=listing.title or None,
            detail_resource_key=resource_key,
        )

    def close(self) -> None:
        """Release any browser resources supplied by the adapter."""
        callback, self._close_callback = self._close_callback, None
        if callback is not None:
            callback()

    # ------------------------------------------------------------------ #
    # Internal: listing + detail collection
    # ------------------------------------------------------------------ #

    def _collect_listings(
        self,
        plan: CrawlPlan,
        source_url: str,
    ) -> list[RawJobListing]:
        if self._listings:
            return list(self._listings)
        page = self._get_page()
        page.goto(source_url)
        page.wait_for_load_state("networkidle")
        self._raise_if_blocked(page)
        return self._extract_dom_listings(page, plan, source_url)

    def _extract_dom_listings(
        self,
        page: Any,
        plan: CrawlPlan,
        source_url: str,
    ) -> list[RawJobListing]:
        selector = plan.listing.item_selector or f"a[href*='{JOB_ROUTE_MARKER}']"
        anchors = page.locator(selector)
        items = anchors.all() if hasattr(anchors, "all") else []
        seen: set[str] = set()
        listings: list[RawJobListing] = []
        for item in items:
            href = item.get_attribute("href") if hasattr(item, "get_attribute") else ""
            if not href or JOB_ROUTE_MARKER not in href:
                continue
            detail_url = urljoin(source_url, href)
            if detail_url in seen:
                continue
            seen.add(detail_url)
            title = ""
            if hasattr(item, "inner_text"):
                title = str(item.inner_text()).strip()
            listings.append(
                RawJobListing(
                    source_url=source_url,
                    detail_url=detail_url,
                    # apply_url stays None: the #/jobs fragment is shared by the
                    # listing page and every detail route, so neither is a valid
                    # apply URL (Task 4.4: never fall back to the listing page).
                    apply_url=None,
                    company=None,
                    title=title or "Inovance position",
                )
            )
        return listings

    def _fetch_detail_text(self, plan: CrawlPlan, detail_url: str) -> str | None:
        if self._page is None and self._page_factory is None:
            return None
        page = self._get_page()
        page.goto(detail_url)
        page.wait_for_load_state("networkidle")
        self._raise_if_blocked(page)
        body_selector = plan.detail.body_selector
        if not body_selector:
            return None
        body = page.locator(body_selector)
        if hasattr(body, "inner_text"):
            return str(body.inner_text()).strip() or None
        return None

    def _get_page(self) -> Any:
        if self._page is None:
            if self._page_factory is None:
                raise RuntimeError(
                    "InovanceCrawlDriver has no fixture listings and no Playwright page"
                )
            self._page = self._page_factory()
        return self._page

    @staticmethod
    def _raise_if_blocked(page: Any) -> None:
        content = str(page.content()).lower() if hasattr(page, "content") else ""
        if any(marker.lower() in content for marker in _BLOCKED_MARKERS):
            raise InovanceBlockedError("Inovance presented a login/captcha/anti-bot wall")


def _listing_from_sample(sample: dict[str, Any], source_url: str) -> RawJobListing:
    """Build a ``RawJobListing`` from a fixture sample.

    The Inovance detail route (``#/jobs/<uuid>``) shares the ``#/jobs``
    fragment with the listing page, so ``apply_url`` is left ``None`` -- the
    listing page is never a fallback apply URL (Task 4.4 rule).
    """
    detail_url = str(sample["detail_url"])
    return RawJobListing(
        source_url=source_url,
        detail_url=detail_url,
        apply_url=None,
        company=None,
        title=str(sample.get("title") or ""),
    )


class InovanceCrawlAdapter(CompleteCrawlAdapter):
    """Certified complete-crawl adapter for Inovance (``recruit.inovance.com``)."""

    url_pattern = "recruit.inovance.com/*"

    def build_driver(
        self,
        plan: CrawlPlan,
        task: DiscoveryTaskInput,
        trajectory: TrajectoryBuffer,
    ) -> InovanceCrawlDriver:
        page, close_callback = _build_playwright_page()
        return InovanceCrawlDriver(
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
    """Lazily start a task-scoped Playwright session for a production crawl.

    Imported lazily so the adapter module is importable in unit-test
    environments without a browser; the unit path uses ``from_fixture`` and
    never reaches here.
    """
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
