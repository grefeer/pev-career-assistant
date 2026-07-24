"""Deterministic, checkpointed pagination for CrawlPlan listing pages."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import asdict
from typing import Any

from backend.app.services.job_discovery.crawling.checkpoint import CrawlCheckpoint
from backend.app.services.job_discovery.crawling.crawl_plan import CrawlPlan
from backend.app.services.job_discovery.crawling.driver import CrawlDriver, ListingPage
from backend.app.services.job_discovery.schemas import DiscoveryTaskInput, PaginationType


SUPPORTED_PAGINATION = {
    PaginationType.SINGLE_PAGE,
    PaginationType.PAGE_NUMBER,
    PaginationType.API_CURSOR,
}
MAX_PAGE_BUDGET = 100


class PaginationLoopError(RuntimeError):
    pass


class CompletionUnverifiedError(RuntimeError):
    pass


class UnsupportedPaginationError(RuntimeError):
    pass


class CrawlBudgetExhausted(RuntimeError):
    def __init__(self, checkpoint: CrawlCheckpoint) -> None:
        super().__init__("crawl page budget exhausted")
        self.checkpoint = checkpoint


def page_fingerprint(page: ListingPage) -> str:
    """Return a stable digest of the extracted listing content on one page."""
    material = json.dumps(
        [asdict(listing) for listing in page.listings],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def iterate_pages(
    plan: CrawlPlan,
    task: DiscoveryTaskInput,
    driver: CrawlDriver,
    checkpoint: CrawlCheckpoint | None = None,
    trajectory: Any | None = None,
) -> Iterator[ListingPage]:
    """Yield listing pages until the driver supplies positive terminal evidence.

    A page limit is a safety stop only: it preserves the resume checkpoint and
    never becomes evidence that all pages were traversed.
    """
    pagination_type = plan.pagination.type
    if pagination_type not in SUPPORTED_PAGINATION:
        raise UnsupportedPaginationError(
            f"unsupported pagination type: {pagination_type.value}"
        )

    active_checkpoint = checkpoint or CrawlCheckpoint(
        plan_version=plan.version,
        source_url=task.source_url,
    )
    active_checkpoint.validate_for(
        plan_version=plan.version,
        source_url=task.source_url,
    )

    cursor = active_checkpoint.pagination_cursor
    seen_fingerprints = set(active_checkpoint.visited_page_fingerprints)
    page_count = 0

    while True:
        if page_count >= MAX_PAGE_BUDGET:
            raise CrawlBudgetExhausted(active_checkpoint)

        page = driver.fetch_listing_page(plan=plan, task=task, cursor=cursor)
        fingerprint = page_fingerprint(page)
        has_terminal_evidence = bool(
            page.terminal_evidence and page.terminal_evidence.strip()
        )
        if (
            not has_terminal_evidence
            and (
                fingerprint in seen_fingerprints
                or page.page_key in active_checkpoint.visited_page_keys
            )
        ):
            raise PaginationLoopError(
                f"repeated page fingerprint for page_key={page.page_key}"
            )

        seen_fingerprints.add(fingerprint)
        if fingerprint not in active_checkpoint.visited_page_fingerprints:
            active_checkpoint.visited_page_fingerprints.append(fingerprint)
        active_checkpoint.visited_page_keys.append(page.page_key)
        terminal_detail_keys = {
            *active_checkpoint.completed_detail_keys,
            *active_checkpoint.failed_detail_keys,
        }
        active_checkpoint.pending_detail_keys = [
            key
            for key in active_checkpoint.pending_detail_keys
            if key not in terminal_detail_keys
        ]
        for listing in page.listings:
            if (
                listing.source_record_key
                and listing.source_record_key not in active_checkpoint.pending_detail_keys
                and listing.source_record_key not in terminal_detail_keys
            ):
                active_checkpoint.pending_detail_keys.append(listing.source_record_key)
        active_checkpoint.pagination_cursor = page.next_cursor

        if trajectory is not None:
            trajectory.record_step(
                "crawl_page",
                "ok",
                {"cursor": cursor},
                {
                    "page_key": page.page_key,
                    "listing_count": len(page.listings),
                },
            )

        page_count += 1
        yield page

        if has_terminal_evidence:
            return
        if page.next_cursor is None:
            raise CompletionUnverifiedError(
                "listing pagination ended without positive terminal evidence"
            )
        cursor = page.next_cursor
