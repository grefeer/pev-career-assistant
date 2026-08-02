from __future__ import annotations

import hashlib


def build_candidate_idempotency_key(
    company: str,
    title: str,
    location: str,
    apply_url: str,
    evidence_hash: str,
) -> str:
    """Build a deterministic idempotency key for a job candidate.

    SHA-256 of canonicalized (lowercase, stripped) fields joined by '::'.
    Same input -> same output, every time.

    Args:
        company: Company name.
        title: Job title.
        location: Primary location.
        apply_url: Application URL.
        evidence_hash: Content hash of supporting evidence.

    Returns:
        Hex-encoded SHA-256 digest (64 characters).
    """
    canonical_parts = (
        (company or "").strip().lower(),
        (title or "").strip().lower(),
        (location or "").strip().lower(),
        (apply_url or "").strip().lower(),
        (evidence_hash or "").strip().lower(),
    )
    raw = "::".join(canonical_parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_similarity_group_key(
    company: str,
    title: str,
    recruitment_type: str,
    source_family: str,
) -> str:
    """Build a grouping key for similarity-based deduplication.

    Normalized grouping: first 3 chars of canonicalized company +
    first 3 chars of canonicalized title + recruitment_type.

    Args:
        company: Company name.
        title: Job title.
        recruitment_type: Type of recruitment (e.g. internship, full_time,
                          campus_recruitment).
        source_family: Ignored. Kept for backwards-compatible call sites.

    Returns:
        Group key string for bucketing similar candidates.
    """
    company_prefix = (company or "").strip().lower()[:3]
    title_prefix = (title or "").strip().lower()[:3]
    rec_type = (recruitment_type or "unknown").strip().lower()

    return f"{company_prefix}::{title_prefix}::{rec_type}"
