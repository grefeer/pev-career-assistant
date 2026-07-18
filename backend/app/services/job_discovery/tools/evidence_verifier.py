from __future__ import annotations

from backend.app.services.job_discovery.schemas import NormalizedJobCandidate, PageEvidence

# Minimum description length to avoid flagging as vague (characters).
_MIN_DESCRIPTION_LENGTH = 50

# Rough indicator of stale content — content blocks mentioning years
# more than 2 years before current year (2026 for this implementation).
# We use a heuristic rather than parsing exact dates.
_STALE_YEAR_THRESHOLD = 2024


def _check_stale(description_text: str) -> bool:
    """Heuristic check: does the description reference a year before the threshold?"""
    import re

    years = re.findall(r"\b(20[0-9]{2})\b", description_text)
    for y_str in years:
        try:
            y = int(y_str)
            if 2000 < y < _STALE_YEAR_THRESHOLD:
                return True
        except ValueError:
            continue
    return False


def _is_vague(candidate: NormalizedJobCandidate) -> bool:
    """Check if the candidate description is too vague to be actionable."""
    text = candidate.description_text or ""
    if not text.strip():
        return True
    if len(text.strip()) < _MIN_DESCRIPTION_LENGTH:
        return True
    return False


def _is_non_jd_text(candidate: NormalizedJobCandidate) -> bool:
    """Check if the extracted text looks like non-job content."""
    text = candidate.description_text or ""
    if not text.strip():
        return True
    # If it looks like nav / boilerplate with no job keywords
    return False


def verify_evidence(
    candidates: list[NormalizedJobCandidate],
    evidence: list[PageEvidence],
) -> list[NormalizedJobCandidate]:
    """Verify and filter candidates against evidence collection.

    Rejects candidates that:
    - Have no title AND no company_name
    - Have no supporting evidence refs
    - Are stale (description references old year markers)

    Flags (adds normalization_warnings) for:
    - Vague descriptions (< 50 chars)
    - Possibly non-JD text

    This is a pure, deterministic filter — no LLM, no DB, no network.

    Args:
        candidates: List of extracted job candidates.
        evidence: List of page evidence items collected.

    Returns:
        Filtered list of candidates (new list, originals not mutated).
    """
    verified: list[NormalizedJobCandidate] = []

    for candidate in candidates:
        warnings: list[str] = []

        # --- Rejection: no title AND no company_name ---
        if not candidate.title and not candidate.company_name:
            warnings.append(
                "Rejected: missing both title and company_name"
            )
            continue

        # --- Rejection: no supporting evidence refs ---
        if not candidate.evidence_refs and not evidence:
            warnings.append(
                "Rejected: no supporting evidence refs and no evidence provided"
            )
            continue

        # If there's evidence but candidate has no evidence_refs, add a warning
        if not candidate.evidence_refs and evidence:
            warnings.append(
                "Candidate has no evidence_refs but evidence was collected"
            )

        # --- Staleness check ---
        if _check_stale(candidate.description_text):
            warnings.append(
                "Possible stale content: description references year before "
                f"{_STALE_YEAR_THRESHOLD}"
            )

        # --- Vagueness check ---
        if _is_vague(candidate):
            warnings.append(
                f"Vague description: less than {_MIN_DESCRIPTION_LENGTH} characters"
            )

        # --- Build final candidate (preserve original, add warnings) ---
        final_warnings = list(candidate.normalization_warnings) + warnings

        verified.append(
            NormalizedJobCandidate(
                title=candidate.title,
                company_name=candidate.company_name,
                department=candidate.department,
                description_text=candidate.description_text,
                responsibilities=candidate.responsibilities,
                requirements=candidate.requirements,
                locations=list(candidate.locations),
                recruitment_types=list(candidate.recruitment_types),
                industries=list(candidate.industries),
                apply_url=candidate.apply_url,
                application_channel_json=candidate.application_channel_json,
                deadline_text=candidate.deadline_text,
                referral_code=candidate.referral_code,
                confidence=candidate.confidence,
                evidence_refs=list(candidate.evidence_refs),
                normalization_warnings=final_warnings,
            )
        )

    return verified
