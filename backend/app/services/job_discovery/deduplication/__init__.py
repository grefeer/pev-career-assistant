"""Canonical-job deduplication for the job-discovery pipeline."""

from backend.app.services.job_discovery.deduplication.canonical_job_deduplicator import (
    deduplicate_candidates,
)

__all__ = ["deduplicate_candidates"]
