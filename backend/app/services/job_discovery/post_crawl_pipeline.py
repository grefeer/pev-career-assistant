"""Convert deterministic full-crawl output into a discovery result."""

from __future__ import annotations

import hashlib

from backend.app.services.job_discovery.crawling.crawl_executor import (
    CrawlExecutionResult,
)
from backend.app.services.job_discovery.crawling.coverage import verify_coverage
from backend.app.services.job_discovery.deduplication import deduplicate_candidates
from backend.app.services.job_discovery.schemas import (
    DiscoveryRunResult,
    DiscoveryTaskInput,
    NormalizedJobCandidate,
    PageEvidence,
    RawJobDetail,
    RawJobListing,
)
from backend.app.services.job_discovery.tools.evidence_verifier import (
    verify_evidence as _verify_evidence,
)
from backend.app.services.job_discovery.tools.jd_extraction import (
    extract_jd_candidates as _extract_jd_candidates,
)


def run_post_crawl_pipeline(
    task: DiscoveryTaskInput,
    crawl_result: CrawlExecutionResult,
) -> DiscoveryRunResult:
    """Extract, repair, verify, and package candidates from fetched JDs.

    Coverage is evaluated first and remains the sole authority for final
    completeness.  Details fetched before an incomplete crawl still become
    reviewable candidates; their status is never promoted by candidate count.
    """
    coverage_decision = verify_coverage(crawl_result.coverage)
    evidence: list[PageEvidence] = []
    candidates: list[NormalizedJobCandidate] = []

    listings_by_detail_url = _listings_by_detail_url(crawl_result.raw_listings)
    for detail in crawl_result.raw_details:
        detail_evidence = _make_detail_evidence(detail)
        evidence.append(detail_evidence)
        listing = listings_by_detail_url.get(detail.detail_url)
        extracted = _extract_jd_candidates(detail.full_text, detail.detail_url)
        candidates.extend(
            _repair_candidate_metadata(candidate, listing, detail, detail_evidence)
            for candidate in extracted
        )

    verified_candidates = _verify_evidence(candidates, evidence)
    packaged_candidates = deduplicate_candidates(verified_candidates)
    return DiscoveryRunResult(
        status=coverage_decision.status,
        block_reason=None if coverage_decision.complete else coverage_decision.reason,
        evidence=evidence,
        candidates=packaged_candidates,
        execution_error=crawl_result.error,
        coverage=crawl_result.coverage,
        summary=(
            f"Full crawl collected {len(crawl_result.raw_listings)} listing(s), "
            f"fetched {len(crawl_result.raw_details)} detail(s), and produced "
            f"{len(packaged_candidates)} candidate(s)"
        ),
    )


def _listings_by_detail_url(
    listings: list[RawJobListing],
) -> dict[str, RawJobListing]:
    return {
        listing.detail_url: listing
        for listing in listings
        if listing.detail_url is not None
    }


def _make_detail_evidence(detail: RawJobDetail) -> PageEvidence:
    content_hash = hashlib.sha256(detail.full_text.encode("utf-8")).hexdigest()
    return PageEvidence(
        evidence_type="page_text",
        url=detail.detail_url,
        title=detail.title,
        content_hash=content_hash,
        text_excerpt=detail.full_text[:5000],
        metadata={"detail_resource_key": detail.detail_resource_key},
    )


def _repair_candidate_metadata(
    candidate: NormalizedJobCandidate,
    listing: RawJobListing | None,
    detail: RawJobDetail,
    evidence: PageEvidence,
) -> NormalizedJobCandidate:
    """Prefer the associated listing's authoritative display metadata."""
    raw_detail_text = detail.full_text.strip()
    title_placeholder = (listing.title if listing is not None else detail.title) or ""
    responsibilities = candidate.responsibilities
    requirements = candidate.requirements
    # A certified detail resource is already evidence-backed and job-scoped.
    # Some portals provide an unlabelled responsibility paragraph, which the
    # generic section parser intentionally cannot classify. Preserve it rather
    # than silently downgrading a full JD to title-only. Do not promote the
    # driver's title fallback (used only when no detail text exists).
    if (
        not responsibilities
        and not requirements
        and raw_detail_text
        and raw_detail_text != title_placeholder.strip()
    ):
        responsibilities = raw_detail_text
    return NormalizedJobCandidate(
        title=(listing.title if listing is not None else None) or candidate.title or detail.title,
        company_name=(listing.company if listing is not None else None)
        or candidate.company_name
        or detail.company,
        department=candidate.department,
        description_text=candidate.description_text,
        responsibilities=responsibilities,
        requirements=requirements,
        locations=(list(listing.locations) if listing is not None and listing.locations else list(candidate.locations or detail.locations)),
        recruitment_types=list(candidate.recruitment_types),
        industries=list(candidate.industries),
        apply_url=(listing.apply_url if listing is not None else None) or detail.detail_url,
        application_channel_json=candidate.application_channel_json,
        deadline_text=candidate.deadline_text,
        referral_code=candidate.referral_code,
        confidence=candidate.confidence,
        evidence_refs=_merged_evidence_refs(candidate, evidence),
        normalization_warnings=list(candidate.normalization_warnings),
    )


def _merged_evidence_refs(
    candidate: NormalizedJobCandidate,
    evidence: PageEvidence,
) -> list[dict]:
    """Attach the detail evidence using the legacy persistence shape."""
    refs = [ref for ref in candidate.evidence_refs if isinstance(ref, dict)]
    detail_ref = {
        "url": evidence.url,
        "content_hash": evidence.content_hash,
        "evidence_type": evidence.evidence_type,
    }
    if not any(ref.get("content_hash") == evidence.content_hash for ref in refs):
        refs.append(detail_ref)
    return refs
