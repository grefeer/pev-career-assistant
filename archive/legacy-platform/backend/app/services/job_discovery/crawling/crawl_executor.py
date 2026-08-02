"""Deterministic listing/detail crawl orchestration for a validated plan."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass

from backend.app.services.job_discovery.crawling.checkpoint import CrawlCheckpoint
from backend.app.services.job_discovery.crawling.coverage import verify_coverage
from backend.app.services.job_discovery.crawling.crawl_plan import CrawlPlan
from backend.app.services.job_discovery.crawling.driver import CrawlDriver
from backend.app.services.job_discovery.crawling.pagination import iterate_pages
from backend.app.services.job_discovery.schemas import (
    CrawlCoverage,
    DiscoveryTaskInput,
    RawJobDetail,
    RawJobListing,
)
from backend.app.services.job_discovery.strategy.trajectory_buffer import (
    TrajectoryBuffer,
)


TRACKING_QUERY_KEYS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "source",
        "channel",
        "ref",
        "referer_code",
        "timestamp",
        "session",
    }
)


@dataclass
class CrawlExecutionResult:
    raw_listings: list[RawJobListing]
    raw_details: list[RawJobDetail]
    coverage: CrawlCoverage
    checkpoint: CrawlCheckpoint | None
    error: str | None = None


def normalize_detail_url(url: str) -> str:
    """Normalize a detail URL without removing job-identifying parameters."""
    before_fragment, fragment_separator, fragment = url.partition("#")
    before_query, query_separator, query = before_fragment.partition("?")
    if not query_separator:
        return url
    filtered_segments = [
        segment
        for segment in query.split("&")
        if segment.split("=", 1)[0] not in TRACKING_QUERY_KEYS
    ]
    filtered_query = "&".join(filtered_segments)
    normalized = before_query
    if filtered_query:
        normalized = f"{normalized}?{filtered_query}"
    return f"{normalized}{fragment_separator}{fragment}"


def make_source_record_key(listing: RawJobListing) -> str:
    """Return a stable key for a listing row before detail-resource grouping."""
    material = "|".join(
        [
            _normalized_text(listing.company),
            _normalized_text(listing.title),
            ",".join(sorted(listing.locations)),
            _normalized_text(listing.job_code),
            normalize_detail_url(listing.detail_url or listing.source_url),
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def make_detail_resource_key(listing: RawJobListing) -> str:
    """Return a stable key for the JD resource shared by listing rows."""
    job_code = _normalized_text(listing.job_code)
    if job_code:
        material = f"{_normalized_text(listing.company)}|job_code|{job_code}"
    elif listing.detail_url:
        material = normalize_detail_url(listing.detail_url)
    else:
        source_record_key = listing.source_record_key or make_source_record_key(listing)
        material = f"missing_detail|{source_record_key}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class CrawlExecutor:
    """Collect all listing pages, then fetch each unique detail resource once."""

    def __init__(
        self,
        driver: CrawlDriver,
        trajectory: TrajectoryBuffer | None = None,
    ) -> None:
        self.driver = driver
        self.trajectory = trajectory

    def execute(
        self,
        *,
        plan: CrawlPlan,
        task: DiscoveryTaskInput,
        checkpoint: CrawlCheckpoint | None = None,
    ) -> CrawlExecutionResult:
        active_checkpoint = checkpoint or CrawlCheckpoint(
            plan_version=plan.version,
            source_url=task.source_url,
        )
        active_checkpoint.validate_for(
            plan_version=plan.version,
            source_url=task.source_url,
        )
        _reset_unrecoverable_legacy_checkpoint(active_checkpoint)
        raw_listings = _restore_listings(active_checkpoint)
        details = _restore_details(active_checkpoint)
        error: str | None = None

        if not active_checkpoint.pagination_complete:
            try:
                for page in iterate_pages(
                    plan,
                    task,
                    self.driver,
                    checkpoint=active_checkpoint,
                    trajectory=self.trajectory,
                ):
                    raw_listings.extend(page.listings)
                    if page.terminal_evidence:
                        active_checkpoint.completion_evidence.append(
                            page.terminal_evidence
                        )
                        active_checkpoint.pagination_complete = True
                    if page.expected_page_count is not None:
                        active_checkpoint.expected_page_count = page.expected_page_count
                    if page.expected_listing_count is not None:
                        active_checkpoint.expected_listing_count = (
                            page.expected_listing_count
                        )
                    _persist_listings(active_checkpoint, raw_listings)
            except Exception as exc:
                error = _sanitize_error(exc)

        source_merged = _merge_source_records(raw_listings)
        resource_listings = _merge_detail_resources(source_merged.values())
        _persist_listings(active_checkpoint, raw_listings)
        completed_keys = {
            detail.detail_resource_key
            for detail in details
            if detail.detail_resource_key is not None
        }
        failed_keys = set(active_checkpoint.failed_detail_keys)
        pending_keys = set(active_checkpoint.pending_detail_keys)

        for resource_key, listing in resource_listings.items():
            if resource_key in completed_keys:
                continue
            if listing.detail_url is None:
                if plan.completion.require_all_details:
                    failed_keys.add(resource_key)
                    pending_keys.add(resource_key)
                    error = error or "MissingDetailUrl: missing_detail_url"
                continue
            try:
                detail = self.driver.fetch_detail(
                    plan=plan,
                    listing=listing,
                    resource_key=resource_key,
                )
            except Exception as exc:
                if plan.completion.require_all_details:
                    failed_keys.add(resource_key)
                    pending_keys.add(resource_key)
                    error = error or _sanitize_error(exc)
                self._record_detail_failure(resource_key)
                continue

            detail.detail_resource_key = resource_key
            details.append(detail)
            completed_keys.add(resource_key)
            failed_keys.discard(resource_key)
            pending_keys.discard(resource_key)
            _persist_details(active_checkpoint, details)
            self._record_detail_success(resource_key)

        current_resource_keys = set(resource_listings)
        if plan.completion.require_all_details:
            pending_keys.update(current_resource_keys - completed_keys)
        pending_keys.difference_update(completed_keys)
        active_checkpoint.completed_detail_keys = _ordered_keys(completed_keys)
        active_checkpoint.failed_detail_keys = _ordered_keys(failed_keys)
        active_checkpoint.pending_detail_keys = _ordered_keys(pending_keys)
        _persist_details(active_checkpoint, details)

        coverage = CrawlCoverage(
            pagination_type=plan.pagination.type,
            expected_page_count=active_checkpoint.expected_page_count,
            visited_page_count=len(active_checkpoint.visited_page_keys),
            visited_page_keys=list(active_checkpoint.visited_page_keys),
            expected_listing_count=active_checkpoint.expected_listing_count,
            raw_listing_count=len(raw_listings),
            unique_listing_count=len(source_merged),
            total_detail_count=len(resource_listings),
            fetched_detail_count=sum(
                key in completed_keys for key in resource_listings
            ),
            failed_detail_count=sum(key in failed_keys for key in resource_listings),
            require_all_details=plan.completion.require_all_details,
            completion_evidence=list(active_checkpoint.completion_evidence),
            resumable=bool(
                error
                or failed_keys
                or not active_checkpoint.pagination_complete
            ),
            resume_cursor=active_checkpoint.pagination_cursor,
        )
        decision = verify_coverage(coverage)
        coverage.coverage_complete = decision.complete

        return CrawlExecutionResult(
            raw_listings=list(resource_listings.values()),
            raw_details=details,
            coverage=coverage,
            checkpoint=active_checkpoint if coverage.resumable else None,
            error=error,
        )

    def _record_detail_success(self, resource_key: str) -> None:
        if self.trajectory is not None:
            self.trajectory.record_step(
                "crawl_detail",
                "ok",
                {"detail_resource_key": resource_key},
                {"fetched": True},
            )

    def _record_detail_failure(self, resource_key: str) -> None:
        if self.trajectory is not None:
            self.trajectory.record_step(
                "crawl_detail",
                "failed",
                {"detail_resource_key": resource_key},
            )


def _restore_listings(checkpoint: CrawlCheckpoint) -> list[RawJobListing]:
    return [RawJobListing(**listing) for listing in checkpoint.collected_listings]


def _reset_unrecoverable_legacy_checkpoint(checkpoint: CrawlCheckpoint) -> None:
    payload_completed_keys = {
        detail.get("detail_resource_key")
        for detail in checkpoint.completed_details
        if detail.get("detail_resource_key")
    }
    has_unbacked_completed_key = bool(
        set(checkpoint.completed_detail_keys) - payload_completed_keys
    )
    has_historical_pagination = bool(
        checkpoint.pagination_cursor
        or checkpoint.visited_page_keys
        or checkpoint.visited_page_fingerprints
        or checkpoint.completion_evidence
    )
    cannot_reconstruct_history = has_historical_pagination and not checkpoint.collected_listings
    if not has_unbacked_completed_key and not cannot_reconstruct_history:
        return

    checkpoint.pagination_cursor = None
    checkpoint.visited_page_keys = []
    checkpoint.visited_page_fingerprints = []
    checkpoint.pending_detail_keys = []
    checkpoint.completed_detail_keys = []
    checkpoint.failed_detail_keys = []
    checkpoint.collected_listings = []
    checkpoint.completed_details = []
    checkpoint.pagination_complete = False
    checkpoint.completion_evidence = []
    checkpoint.expected_page_count = None
    checkpoint.expected_listing_count = None


def _restore_details(checkpoint: CrawlCheckpoint) -> list[RawJobDetail]:
    return [RawJobDetail(**detail) for detail in checkpoint.completed_details]


def _persist_listings(
    checkpoint: CrawlCheckpoint,
    listings: Iterable[RawJobListing],
) -> None:
    checkpoint.collected_listings = [asdict(listing) for listing in listings]


def _persist_details(
    checkpoint: CrawlCheckpoint,
    details: Iterable[RawJobDetail],
) -> None:
    checkpoint.completed_details = [asdict(detail) for detail in details]


def _merge_source_records(
    listings: Iterable[RawJobListing],
) -> dict[str, RawJobListing]:
    merged: dict[str, RawJobListing] = {}
    for listing in listings:
        source_record_key = listing.source_record_key or make_source_record_key(listing)
        listing.source_record_key = source_record_key
        existing = merged.get(source_record_key)
        if existing is None:
            merged[source_record_key] = listing
            continue
        _merge_listing_fields(existing, listing)
    return merged


def _merge_detail_resources(
    listings: Iterable[RawJobListing],
) -> dict[str, RawJobListing]:
    merged: dict[str, RawJobListing] = {}
    for listing in listings:
        resource_key = make_detail_resource_key(listing)
        existing = merged.get(resource_key)
        if existing is None:
            merged[resource_key] = listing
            continue
        _merge_listing_fields(existing, listing)
    return merged


def _merge_listing_fields(target: RawJobListing, incoming: RawJobListing) -> None:
    target.locations = sorted({*target.locations, *incoming.locations})
    target.evidence_refs = list(
        dict.fromkeys([*target.evidence_refs, *incoming.evidence_refs])
    )
    target.apply_url = target.apply_url or incoming.apply_url
    target.recruitment_type_hint = (
        target.recruitment_type_hint or incoming.recruitment_type_hint
    )
    target.graduation_year_hints = sorted(
        {*target.graduation_year_hints, *incoming.graduation_year_hints}
    )
    target.job_code = target.job_code or incoming.job_code
    target.detail_url = target.detail_url or incoming.detail_url
    target.company = target.company or incoming.company
    target.title = target.title or incoming.title


def _normalized_text(value: str | None) -> str:
    return (value or "").strip().lower()


def _ordered_keys(keys: Iterable[str]) -> list[str]:
    return sorted(set(keys))


def _sanitize_error(exc: Exception) -> str:
    exception_type = type(exc).__name__
    reason_code = re.sub(r"(?<!^)(?=[A-Z])", "_", exception_type).lower()
    return f"{exception_type}: {reason_code}"
