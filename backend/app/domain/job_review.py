from __future__ import annotations


REJECT_REASON_CODES = frozenset(
    {
        "invalid_source",
        "wrong_company",
        "insufficient_job_details",
        "unsafe_or_invalid_apply_channel",
    }
)

EXPIRE_REASON_CODES = frozenset(
    {
        "closed_on_official_site",
        "deadline_passed",
        "application_channel_unavailable",
    }
)
