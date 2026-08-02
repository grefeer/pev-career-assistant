"""JD normalization helpers for the job-discovery pipeline.

Pure text normalization (no I/O, no LLM). Re-exports the public helpers from
``jd_normalizer`` so callers can do ``from ...normalization import normalize_company``.
"""

from backend.app.services.job_discovery.normalization.jd_normalizer import (
    core_hash,
    normalize_company,
    normalize_text,
    normalize_title,
)

__all__ = [
    "core_hash",
    "normalize_company",
    "normalize_text",
    "normalize_title",
]
