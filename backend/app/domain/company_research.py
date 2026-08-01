"""Domain rules for the company-research skill.

Company research is a user-scoped, crawl-and-extract skill: given a company
name and a public careers/about URL, the runtime browses the page, extracts a
company profile plus its open positions, and persists a
``CompanyResearchReport``.  Like job discovery it is bounded by the security
hard gates - a login/captcha/anti-bot wall surfaces as
``needs_manual_review`` and is never bypassed.
"""

from __future__ import annotations

import sys

if sys.version_info >= (3, 11):
    from enum import StrEnum
else:
    from enum import Enum

    class StrEnum(str, Enum):
        """Minimal StrEnum polyfill for Python < 3.11."""

        pass


class CompanyResearchStatus(StrEnum):
    """Lifecycle of one company-research report."""

    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    needs_manual_review = "needs_manual_review"
    failed = "failed"
    cancelled = "cancelled"


class CompanyResearchBlockReason(StrEnum):
    """Why a report could not complete autonomously.

    Mirrors the discovery block vocabulary so an operator reads the same
    reason codes across skills.  ``no_evidence`` is the company-research
    analogue of an empty crawl (the page rendered nothing parseable).
    """

    anti_bot = "anti_bot"
    login_required = "login_required"
    captcha = "captcha"
    no_evidence = "no_evidence"
    artifact_error = "artifact_error"


# Field-length guards shared by the repository and the DTO layer so a
# pathological input cannot overflow the column or smuggle a huge blob into
# the report row.
COMPANY_NAME_MAX_LENGTH = 256
SOURCE_URL_MAX_LENGTH = 2048
SUMMARY_MAX_LENGTH = 1000
LAST_ERROR_MAX_LENGTH = 2000

# Terminal states: once reached the runtime never mutates the report again.
# ``cancelled`` is the only human-only transition out of a non-terminal state.
TERMINAL_STATUSES = frozenset(
    {
        CompanyResearchStatus.succeeded,
        CompanyResearchStatus.needs_manual_review,
        CompanyResearchStatus.failed,
        CompanyResearchStatus.cancelled,
    }
)

# A queued report may be claimed into ``running`` or cancelled.  ``running``
# may resolve to any of the terminal outcomes.  Terminal states are closed.
RUNTIME_TRANSITIONS: dict[CompanyResearchStatus, frozenset[CompanyResearchStatus]] = {
    CompanyResearchStatus.queued: frozenset(
        {CompanyResearchStatus.running, CompanyResearchStatus.cancelled}
    ),
    CompanyResearchStatus.running: frozenset(
        {
            CompanyResearchStatus.succeeded,
            CompanyResearchStatus.needs_manual_review,
            CompanyResearchStatus.failed,
            CompanyResearchStatus.cancelled,
        }
    ),
}


def is_valid_transition(
    from_status: CompanyResearchStatus,
    to_status: CompanyResearchStatus,
) -> bool:
    """Return True when ``to_status`` is an allowed runtime transition."""
    return to_status in RUNTIME_TRANSITIONS.get(from_status, frozenset())


def is_terminal(status: CompanyResearchStatus) -> bool:
    """Return True when ``status`` admits no further runtime mutation."""
    return status in TERMINAL_STATUSES
