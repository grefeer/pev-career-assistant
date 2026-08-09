"""Run-internal deterministic dedup for observed job evidence (P1-4).

Port of the skill's ``deduplicate.py`` canonical-identity semantics as a
career tool: adapter records are keyed by their stable ``job_id`` (the
``MK_``/``BS_``/``NE_``/``DD_``/``BD_`` prefixes), falling back to a
normalized apply_url identity, then to a normalized-title identity only
when the stronger signals are absent.  This is the *input-side* dedup of
one run; the C3 ``seen_jobs`` ledger remains the cross-run output-side
gate and is intentionally untouched here.  Deliberately deterministic --
no LLM semantic clustering (not worth the cost and it breaks evidence
discipline).
"""

from __future__ import annotations

import re
import unicodedata

from pydantic import BaseModel, Field, field_validator

from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.career_skills.job_discovery import (
    ExtractedJobDetails,
    ExtractObservedJobDetailsInput,
    PublicJobFetchError,
    _find_observed_evidence,
    _parse_adapter_evidence,
    extract_observed_job_details,
)

# Same normalization tables as skill/job-discovery/scripts/deduplicate.py.
_INVISIBLE_CHARS = "​‌‍‎‏﻿　\t"
_ASCII_PUNCT = ",.:;!?()[]\"'<>/\\-~"
_CJK_PUNCT = (
    "【】「」『』《》〈〉"
    "〔〕，。、；：！？（）"
)
_DELETE_TABLE = str.maketrans("", "", _ASCII_PUNCT + _CJK_PUNCT)
_WHITESPACE_RE = re.compile(r"\s+")
_TRAILING_QUALIFIER_RE = re.compile(
    r"(?:（[^（）]*）|\([^()]*\)|【[^【】]*】)\s*$"
)


class DeduplicateObservedJobsInput(BaseModel):
    """A bounded set of observed evidence artifacts to dedupe in run order."""

    artifact_ids: list[str] = Field(min_length=1, max_length=20)

    @field_validator("artifact_ids")
    @classmethod
    def validate_artifact_ids(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values]
        if any(not value for value in cleaned) or len(set(cleaned)) != len(cleaned):
            raise ValueError("artifact_ids must be non-empty and unique")
        return cleaned


class DeduplicatedRemoval(BaseModel):
    """One artifact dropped as duplicate (or unprocessable) by the tool."""

    artifact_id: str
    reason: str
    detail: str


class DeduplicateObservedJobsOutput(BaseModel):
    """First-seen-wins kept list plus explicit removals with reasons."""

    kept: list[str]
    removed: list[DeduplicatedRemoval]


def deduplicate_observed_jobs(
    context: ToolContext, payload: DeduplicateObservedJobsInput
) -> DeduplicateObservedJobsOutput:
    """Dedupe observed artifacts by canonical identity, preserving run order.

    Keeps the first artifact that claims an identity; a later artifact
    sharing any identity key is removed with the colliding kept artifact
    named.  Artifacts whose identity cannot be computed (or whose evidence
    is structurally broken) are kept rather than dropped -- this tool only
    removes proven duplicates, never silently loses evidence.
    """
    kept: list[str] = []
    removed: list[DeduplicatedRemoval] = []
    seen: dict[str, str] = {}
    for artifact_id in payload.artifact_ids:
        evidence = _find_observed_evidence(context, artifact_id)
        if evidence is None:
            removed.append(
                DeduplicatedRemoval(
                    artifact_id=artifact_id,
                    reason="evidence_not_found",
                    detail="no observed evidence with this artifact_id",
                )
            )
            continue
        visible_text = evidence.get("visible_text")
        if not isinstance(visible_text, str) or not visible_text:
            removed.append(
                DeduplicatedRemoval(
                    artifact_id=artifact_id,
                    reason="evidence_incomplete",
                    detail="evidence has no visible_text",
                )
            )
            continue
        keys = _evidence_identity_keys(context, artifact_id, evidence)
        if not keys:
            kept.append(artifact_id)
            continue
        collision = next((seen[key] for key in keys if key in seen), None)
        if collision is not None:
            removed.append(
                DeduplicatedRemoval(
                    artifact_id=artifact_id,
                    reason="duplicate_identity",
                    detail=f"shares identity with kept artifact {collision}",
                )
            )
            continue
        kept.append(artifact_id)
        for key in keys:
            seen.setdefault(key, artifact_id)
    return DeduplicateObservedJobsOutput(kept=kept, removed=removed)


def _evidence_identity_keys(
    context: ToolContext, artifact_id: str, evidence: dict[str, object]
) -> tuple[str, ...]:
    """All canonical identity keys one artifact claims, in priority order.

    Adapter-record JSON keys each record by ``job_id`` (or its url/title
    fallback); everything else goes through the normal extraction path and
    keys each candidate the same way.  A structurally broken artifact (one
    the extractor cannot process) claims no identity and is kept.
    """
    records = _parse_adapter_evidence(evidence["visible_text"])
    if records is not None:
        keys = [key for record in records for key in _record_identity_keys(record)]
        return tuple(dict.fromkeys(keys))
    try:
        output = extract_observed_job_details(
            context, ExtractObservedJobDetailsInput(artifact_id=artifact_id)
        )
    except PublicJobFetchError:
        return ()
    keys = [key for candidate in output.candidates for key in _detail_identity_keys(candidate)]
    return tuple(dict.fromkeys(keys))


def _record_identity_keys(record: dict[str, object]) -> tuple[str, ...]:
    """Identity keys for one normalized adapter record.

    ``job_id`` (MK_/BS_/NE_/DD_/BD_ prefix) is the strongest identity and,
    when present, is the only key -- a title change on the same posting
    must not create a second identity.  Without it the record falls back to
    apply_url, then normalized title (mirrors deduplicate.py: job_id is the
    C3 ledger key, so input-side dedup uses the same authority).
    """
    job_id = record.get("job_id")
    if isinstance(job_id, str) and job_id:
        return (f"job_id:{job_id}",)
    apply_url = record.get("apply_url")
    url_key = _url_identity(apply_url if isinstance(apply_url, str) else None)
    location = record.get("location")
    locations = [location] if isinstance(location, str) and location else []
    title = record.get("title")
    title_key = _title_identity(
        title if isinstance(title, str) else None, locations, []
    )
    return tuple(key for key in (url_key, title_key) if key)


def _detail_identity_keys(detail: ExtractedJobDetails) -> tuple[str, ...]:
    """Identity keys for one extracted JD candidate (page-text path)."""
    url_key = _url_identity(detail.apply_url)
    if url_key:
        return (url_key,)
    title_key = _title_identity(
        detail.title, detail.locations, detail.recruitment_types
    )
    return (title_key,) if title_key else ()


def _url_identity(apply_url: str | None) -> str:
    normalized = _normalize_apply_url(apply_url)
    return f"url:{normalized}" if normalized else ""


def _title_identity(
    title: str | None,
    locations: list[str],
    recruitment_types: list[str],
) -> str:
    """Normalized-title identity, scoped by location/recruitment type.

    Mirrors deduplicate.py's title-only branch (company + normalized title
    + location + recruitment type); company_name is almost never extractable
    from a public page, so location/recruitment type carry the disambiguation.
    """
    normalized = _normalize_title(title)
    if not normalized:
        return ""
    loc = "|".join(sorted({_normalize_text(loc) for loc in locations if loc}))
    rt = _normalize_text(recruitment_types[0]) if recruitment_types else ""
    return f"title:{normalized}|{loc}|{rt}"


def _normalize_text(value: str | None) -> str:
    """NFKC + invisible-char strip + lowercase + whitespace/punctuation out."""
    if not value:
        return ""
    s = unicodedata.normalize("NFKC", value)
    for ch in _INVISIBLE_CHARS:
        s = s.replace(ch, "")
    s = s.lower()
    s = _WHITESPACE_RE.sub("", s)
    s = s.translate(_DELETE_TABLE)
    return s


def _normalize_title(title: str | None) -> str:
    """Normalized title, dropping trailing bracketed qualifiers first."""
    if not title:
        return ""
    s = unicodedata.normalize("NFKC", title)
    for ch in _INVISIBLE_CHARS:
        s = s.replace(ch, "")
    while _TRAILING_QUALIFIER_RE.search(s):
        s = _TRAILING_QUALIFIER_RE.sub("", s).rstrip()
    s = s.lower()
    s = _WHITESPACE_RE.sub("", s)
    s = s.translate(_DELETE_TABLE)
    return s


def _normalize_apply_url(url: str | None) -> str:
    """Minimal URL canonicalization (trailing slash + case), nothing else."""
    if not url:
        return ""
    return url.strip().rstrip("/").lower()
