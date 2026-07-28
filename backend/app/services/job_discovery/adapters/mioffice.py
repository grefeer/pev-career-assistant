"""Mioffice certified driver: controlled list pagination with inline JD text."""
from __future__ import annotations

import fnmatch
import re
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

from backend.app.services.job_discovery.adapters.complete_crawl_base import CompleteCrawlAdapter
from backend.app.services.job_discovery.adapters.feishu import _build_playwright_page
from backend.app.services.job_discovery.crawling.crawl_plan import CrawlPlan
from backend.app.services.job_discovery.crawling.driver import ListingPage
from backend.app.services.job_discovery.schemas import DiscoveryTaskInput, RawJobDetail, RawJobListing
from backend.app.services.job_discovery.strategy.trajectory_buffer import TrajectoryBuffer

MIOFFICE_CRAWL_PLAN = """\
plan_type: crawl_plan
version: 1
listing:
  item_selector: "a[href*='/toptalent/position/'][href*='/detail']"
  title_selector: ""
pagination:
  type: page_number
detail:
  body_selector: ""
completion:
  require_all_pages: true
  require_all_details: true
"""

_CARD_SELECTOR = "a[href*='/toptalent/position/'][href*='/detail']"
_TOTAL_PATTERN = re.compile(r"开启新的工作（\s*(\d+)\s*）")
_TERMINAL_PROOF = "mioffice_total_reached"


def _parse_card_text(text: str) -> tuple[str, str, list[str]]:
    """Return title, a labelled JD body, and locations from a rendered card."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return "", "", []
    title = lines[0]
    meta_index = next((i for i, line in enumerate(lines) if "校招" in line), None)
    if meta_index is None:
        return title, "", []
    meta = lines[meta_index]
    location = meta.split("校招", 1)[0].strip()
    if not location and meta_index > 1:
        location = lines[meta_index - 1]
    body_start = next(
        (
            index for index in range(meta_index + 1, len(lines))
            if re.match(r"(?:\d+[、.．]|岗位职责|职位描述)", lines[index])
        ),
        len(lines),
    )
    body = "\n".join(lines[body_start:]).strip()
    return title, f"岗位职责\n{body}" if body else "", [location] if location else []


class MiofficeCrawlDriver:
    def __init__(
        self, *, source_url: str, page: Any, close_callback: Callable[[], None]
    ) -> None:
        self._source_url = source_url
        self._page = page
        self._close_callback = close_callback
        self._total: int | None = None
        self._emitted = 0
        self._base_list_url: str | None = None
        self._jd_by_url: dict[str, str] = {}

    def fetch_listing_page(
        self, *, plan: CrawlPlan, task: DiscoveryTaskInput, cursor: dict[str, Any] | None
    ) -> ListingPage:
        page_number = int((cursor or {}).get("page", 1))
        url = self._listing_url(page_number)
        self._page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        cards = self._page.locator(_CARD_SELECTOR)
        cards.first.wait_for(state="visible", timeout=10_000)
        self._page.wait_for_timeout(250)
        self._total = self._total or self._visible_total()
        listings: list[RawJobListing] = []
        for card in cards.all():
            href = str(card.get_attribute("href") or "")
            title, jd, locations = _parse_card_text(str(card.inner_text() or ""))
            if not href or not title:
                continue
            detail_url = urljoin(self._page.url, href)
            listings.append(RawJobListing(
                source_url=task.source_url, detail_url=detail_url, apply_url=detail_url,
                company="小米", title=title, locations=locations,
            ))
            if jd:
                self._jd_by_url[detail_url] = jd
        self._emitted += len(listings)
        reached = self._total is not None and self._emitted >= self._total
        return ListingPage(
            page_key=str(page_number), listings=listings,
            next_cursor=None if reached else {"page": page_number + 1},
            terminal_evidence=_TERMINAL_PROOF if reached else None,
            expected_listing_count=self._total,
        )

    def fetch_detail(
        self, *, plan: CrawlPlan, listing: RawJobListing, resource_key: str
    ) -> RawJobDetail:
        detail_url = listing.detail_url or ""
        body = self._jd_by_url.get(detail_url)
        if not body:
            # A small minority of public cards omit their preview snippet.
            # Fetch only those job-level public detail pages; normal cards never
            # pay this cost and no login/captcha action is attempted.
            self._page.goto(detail_url, wait_until="domcontentloaded", timeout=30_000)
            self._page.wait_for_timeout(250)
            detail_text = str(self._page.locator("main").inner_text() or "").strip()
            body = f"职位描述\n{detail_text}" if detail_text else ""
        if not body:
            raise RuntimeError("Mioffice position has no public JD body")
        return RawJobDetail(
            detail_url=detail_url, full_text=body, title=listing.title or None,
            locations=list(listing.locations), detail_resource_key=resource_key,
        )

    def close(self) -> None:
        self._close_callback()

    def _listing_url(self, page_number: int) -> str:
        if self._base_list_url is None:
            self._page.goto(self._source_url, wait_until="domcontentloaded", timeout=30_000)
            self._base_list_url = self._page.url
        parsed = urlparse(self._base_list_url)
        query = parse_qs(parsed.query, keep_blank_values=True)
        query["current"] = [str(page_number)]
        query["limit"] = ["10"]
        return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))

    def _visible_total(self) -> int | None:
        text = str(self._page.locator("body").inner_text() or "")
        match = _TOTAL_PATTERN.search(text)
        return int(match.group(1)) if match else None


class MiofficeCrawlAdapter(CompleteCrawlAdapter):
    url_pattern = "*.jobs.f.mioffice.cn/*"

    def build_driver(
        self, plan: CrawlPlan, task: DiscoveryTaskInput, trajectory: TrajectoryBuffer
    ) -> MiofficeCrawlDriver:
        page, close_callback = _build_playwright_page()
        return MiofficeCrawlDriver(source_url=task.source_url, page=page, close_callback=close_callback)

    def validate(self, url: str) -> bool:
        target = url.replace("https://", "").replace("http://", "")
        return fnmatch.fnmatch(url, self.url_pattern) or fnmatch.fnmatch(target, self.url_pattern)
