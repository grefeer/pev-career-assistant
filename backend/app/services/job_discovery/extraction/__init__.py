"""LLM JD-body extraction (PATH C quality port).

Exports :func:`extract_jd_candidates_llm` - a per-page structured-output LLM
extractor that augments the Legacy Supervisor's deterministic title-only
fallback with full-JD candidates. Gated behind
``Settings.job_discovery_llm_extraction_enabled`` (default off).
"""

from backend.app.services.job_discovery.extraction.llm_jd_extractor import (
    extract_jd_candidates_llm,
)

__all__ = ["extract_jd_candidates_llm"]
