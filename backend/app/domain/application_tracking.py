"""Domain rules for the application-tracking skill.

Application tracking is a user-scoped, *non*-agent skill: the user records the
real-world jobs they have applied to (or plan to) and advances each record
through a state machine as the application progresses.  There is no crawl, no
LLM, and - critically - **no auto-submit** (security gate #1): the platform never
files an application on the user's behalf.  Status transitions are an explicit
human action recorded as an append-only event log.

State machine::

    saved -> applied -> screening -> interview -> offer
                                   \\           \\           \\
                                    rejected    rejected    rejected
        \\-- withdrawn <-- (any non-terminal state) -- offer

``saved``/``applied``/``screening``/``interview`` are non-terminal (the user can
keep advancing or abandon).  ``offer``, ``rejected`` and ``withdrawn`` are
terminal.  ``withdrawn`` is reachable from every non-terminal state (and from
``offer`` - the user declines) and is itself terminal.
"""

from __future__ import annotations

import sys

if sys.version_info >= (3, 11):
    from enum import StrEnum
else:  # pragma: no cover - Python < 3.11 polyfill, dead on the 3.12 runtime
    from enum import Enum

    class StrEnum(str, Enum):  # pragma: no cover
        """Minimal StrEnum polyfill for Python < 3.11."""

        pass


class ApplicationStatus(StrEnum):
    """Lifecycle of one tracked application."""

    saved = "saved"
    applied = "applied"
    screening = "screening"
    interview = "interview"
    offer = "offer"
    rejected = "rejected"
    withdrawn = "withdrawn"


# Terminal states: no further transitions are allowed once reached.
TERMINAL_STATUSES = frozenset(
    {
        ApplicationStatus.offer,
        ApplicationStatus.rejected,
        ApplicationStatus.withdrawn,
    }
)

# Allowed forward transitions.  ``withdrawn`` is reachable from every
# non-terminal state (plus ``offer``, for declining an offer); ``rejected`` is
# reachable from the active pipeline states.  Terminal states (``rejected``,
# ``withdrawn``) deliberately have no entry - no transitions out.
_TRANSITIONS: dict[ApplicationStatus, frozenset[ApplicationStatus]] = {
    ApplicationStatus.saved: frozenset(
        {ApplicationStatus.applied, ApplicationStatus.withdrawn}
    ),
    ApplicationStatus.applied: frozenset(
        {
            ApplicationStatus.screening,
            ApplicationStatus.rejected,
            ApplicationStatus.withdrawn,
        }
    ),
    ApplicationStatus.screening: frozenset(
        {
            ApplicationStatus.interview,
            ApplicationStatus.rejected,
            ApplicationStatus.withdrawn,
        }
    ),
    ApplicationStatus.interview: frozenset(
        {
            ApplicationStatus.offer,
            ApplicationStatus.rejected,
            ApplicationStatus.withdrawn,
        }
    ),
    ApplicationStatus.offer: frozenset({ApplicationStatus.withdrawn}),
}


def is_terminal(status: ApplicationStatus) -> bool:
    """Return True when ``status`` admits no further transition."""
    return status in TERMINAL_STATUSES


def allowed_transitions(status: ApplicationStatus) -> frozenset[ApplicationStatus]:
    """Return the set of statuses ``status`` may legally transition to."""
    return _TRANSITIONS.get(status, frozenset())


def is_valid_transition(
    from_status: ApplicationStatus, to_status: ApplicationStatus
) -> bool:
    """Return True when ``from_status`` -> ``to_status`` is a legal move."""
    return to_status in allowed_transitions(from_status)


# Field-length guards shared by the repository and the DTO layer.
COMPANY_NAME_MAX_LENGTH = 200
TITLE_MAX_LENGTH = 200
APPLY_URL_MAX_LENGTH = 1024
SOURCE_MAX_LENGTH = 64
NOTE_MAX_LENGTH = 2000
