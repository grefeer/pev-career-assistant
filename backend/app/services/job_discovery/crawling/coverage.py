"""Deterministic CoverageVerifier for the full-crawl pipeline.

``verify_coverage`` is a pure function that proves whether a crawl is complete
from pagination + detail evidence. It is the single authority that may declare
a crawl ``succeeded``; an Agent may not. It is NOT wired into the global
``enforce_result_invariants`` (gray migration: PATH C has no ``coverage`` yet);
the post-crawl pipeline (run_post_crawl_pipeline) calls it directly.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.app.services.job_discovery.schemas import CrawlCoverage, PaginationType


@dataclass(frozen=True)
class CoverageDecision:
    """Verdict on whether a crawl reached a provably complete state."""

    complete: bool
    status: str
    reason: str


def verify_coverage(coverage: CrawlCoverage) -> CoverageDecision:
    """Return a deterministic coverage verdict.

    Checks run in order: detail failures first, then visited vs expected
    page count, then collected vs expected listing count, then pagination
    provability (``UNKNOWN`` cannot be proven), then the presence of positive
    terminal evidence. A crawl is ``succeeded`` only when every check passes.

    ``resumable`` failures downgrade to ``partial_success`` (the checkpoint is
    kept for a later resume); non-resumable or unprovable failures go to
    ``needs_manual_review``.
    """
    incomplete_status = (
        "partial_success" if coverage.resumable else "needs_manual_review"
    )

    if coverage.failed_detail_count > 0:
        return CoverageDecision(
            complete=False,
            status=incomplete_status,
            reason=f"{coverage.failed_detail_count} detail pages failed",
        )

    if (
        coverage.expected_page_count is not None
        and coverage.visited_page_count != coverage.expected_page_count
    ):
        return CoverageDecision(
            complete=False,
            status=incomplete_status,
            reason=(
                f"visited {coverage.visited_page_count}/"
                f"{coverage.expected_page_count} pages"
            ),
        )

    if (
        coverage.expected_listing_count is not None
        and coverage.raw_listing_count < coverage.expected_listing_count
    ):
        return CoverageDecision(
            complete=False,
            status=incomplete_status,
            reason=(
                f"collected {coverage.raw_listing_count}/"
                f"{coverage.expected_listing_count} listings"
            ),
        )

    if coverage.pagination_type == PaginationType.UNKNOWN:
        return CoverageDecision(
            complete=False,
            status="needs_manual_review",
            reason="pagination completion cannot be proven",
        )

    if not coverage.completion_evidence:
        return CoverageDecision(
            complete=False,
            status="needs_manual_review",
            reason="missing positive completion evidence",
        )

    return CoverageDecision(
        complete=True,
        status="succeeded",
        reason="all pages and detail resources completed",
    )
