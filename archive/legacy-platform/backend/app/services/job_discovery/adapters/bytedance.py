"""ByteDance campus adapter using the shared public portal search contract."""
from __future__ import annotations

import fnmatch
from typing import Any

from backend.app.services.job_discovery.adapters.complete_crawl_base import CompleteCrawlAdapter
from backend.app.services.job_discovery.adapters.feishu import (
    FEISHU_CRAWL_PLAN,
    FeishuCrawlDriver,
    _build_playwright_page,
    _listing_from_api_item,
    _resolve_path,
    _sample_full_text,
)
from backend.app.services.job_discovery.crawling.crawl_plan import CrawlPlan
from backend.app.services.job_discovery.crawling.driver import ListingPage
from backend.app.services.job_discovery.schemas import DiscoveryTaskInput
from backend.app.services.job_discovery.strategy.trajectory_buffer import TrajectoryBuffer

BYTEDANCE_CRAWL_PLAN = FEISHU_CRAWL_PLAN
_FILTER_CONFIG_MARKER = "/api/v1/config/job/filters/"


def _campus_subject_ids(payload: dict[str, Any]) -> list[str]:
    """Return the public portal's current campus-recruitment project IDs.

    ByteDance's portal combines campus, internship, and other programmes in
    the same listing API.  Project IDs change between recruiting cycles, so
    they must be selected from the portal's public filter response instead of
    being copied from a historical evaluation.
    """
    data = payload.get("data")
    subjects = data.get("job_subject_list") if isinstance(data, dict) else None
    if not isinstance(subjects, list):
        return []
    ids: list[str] = []
    for subject in subjects:
        if not isinstance(subject, dict):
            continue
        name = subject.get("name")
        zh_name = name.get("zh_cn") if isinstance(name, dict) else name
        subject_id = subject.get("id")
        if "校招" in str(zh_name or "") and subject_id:
            ids.append(str(subject_id))
    return list(dict.fromkeys(ids))


def _is_filter_config_response(response: Any) -> bool:
    return (
        _FILTER_CONFIG_MARKER in str(getattr(response, "url", ""))
        and getattr(response, "status", None) == 200
    )


class ByteDanceCrawlDriver(FeishuCrawlDriver):
    """Feishu-compatible driver restricted to the live campus scope."""

    def _live_page(
        self,
        plan: CrawlPlan,
        task: DiscoveryTaskInput,
        cursor: dict[str, Any] | None,
    ) -> ListingPage:
        page = self._get_page()
        page_number = int((cursor or {}).get("page", 1))
        if page_number == 1:
            # The filter configuration is emitted during the same public SPA
            # navigation that yields the initial search response.
            with page.expect_response(_is_filter_config_response) as captured:
                self._capture_search(page, task.source_url)
            config = captured.value.json()
            subject_ids = _campus_subject_ids(config if isinstance(config, dict) else {})
            if not subject_ids or self._search_payload is None:
                raise RuntimeError("ByteDance campus project filters are unavailable")
            self._search_payload["subject_id_list"] = subject_ids
            payload = self._fetch_search_offset(page, offset=0)
        else:
            payload = self._fetch_search_offset(page, offset=self._replay_emitted)

        items = _resolve_path(payload, plan.pagination.items_path) or []
        total = _resolve_path(payload, plan.pagination.total_count_path)
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
        self._replay_emitted += len(listings)
        reached = self._live_total is not None and self._replay_emitted >= self._live_total
        return ListingPage(
            page_key=str(page_number),
            listings=listings,
            next_cursor=None if reached else {"page": page_number + 1},
            terminal_evidence="total_count_reached" if reached else None,
            expected_listing_count=self._live_total,
        )


class ByteDanceCrawlAdapter(CompleteCrawlAdapter):
    url_pattern = "jobs.bytedance.com/*"

    def build_driver(
        self, plan: CrawlPlan, task: DiscoveryTaskInput, trajectory: TrajectoryBuffer
    ) -> ByteDanceCrawlDriver:
        page, close_callback = _build_playwright_page()
        return ByteDanceCrawlDriver(
            source_url=task.source_url, page=page, close_callback=close_callback
        )

    def validate(self, url: str) -> bool:
        target = url.replace("https://", "").replace("http://", "")
        return fnmatch.fnmatch(url, self.url_pattern) or fnmatch.fnmatch(target, self.url_pattern)
