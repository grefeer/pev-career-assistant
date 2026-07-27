"""Canonical-job deduplication for the job-discovery pipeline."""

from backend.app.services.job_discovery.deduplication.canonical_job_deduplicator import (
    canonical_job_key,
    deduplicate_candidates,
)

__all__ = ["canonical_job_key", "deduplicate_candidates"]
