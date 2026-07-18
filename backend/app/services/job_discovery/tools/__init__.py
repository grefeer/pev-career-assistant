from __future__ import annotations

from backend.app.services.job_discovery.tools.link_triage import triage_link
from backend.app.services.job_discovery.tools.wechat_article_parser import parse_wechat_article
from backend.app.services.job_discovery.tools.ocr_pipeline import ocr_image
from backend.app.services.job_discovery.tools.jd_extraction import extract_jd_candidates
from backend.app.services.job_discovery.tools.evidence_verifier import verify_evidence
from backend.app.services.job_discovery.tools.candidate_packager import (
    build_candidate_idempotency_key,
    build_similarity_group_key,
)

__all__ = [
    "triage_link",
    "parse_wechat_article",
    "ocr_image",
    "extract_jd_candidates",
    "verify_evidence",
    "build_candidate_idempotency_key",
    "build_similarity_group_key",
]
