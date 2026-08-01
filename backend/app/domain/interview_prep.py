"""Domain rules for the interview-prep skill.

Interview prep is a user-scoped, agent-driven generation skill: given a target
job snapshot and (optionally) the user's confirmed profile + preferences, an LLM
produces a structured interview-prep kit - likely technical and behavioral
questions, talking points grounded in the user's strengths, topics to review,
and questions to ask the interviewer.  The kit is persisted as an
``InterviewPrepKit`` row.

Unlike job discovery / company research there is no crawl and therefore no
anti-bot / captcha wall: the only failure mode is the LLM not producing
parseable output, which surfaces as ``failed``.  No auto-submit and no
review_version - interview prep is read-only study material.
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


class InterviewPrepKitStatus(StrEnum):
    """Lifecycle of one interview-prep kit."""

    generating = "generating"
    ready = "ready"
    failed = "failed"


# Terminal states: the kit is never mutated again after ``ready`` / ``failed``.
TERMINAL_STATUSES = frozenset(
    {
        InterviewPrepKitStatus.ready,
        InterviewPrepKitStatus.failed,
    }
)


def is_terminal(status: InterviewPrepKitStatus) -> bool:
    """Return True when ``status`` admits no further mutation."""
    return status in TERMINAL_STATUSES


# Field-length guards shared by the repository and the DTO layer.
LAST_ERROR_MAX_LENGTH = 2000
ERROR_CODE_MAX_LENGTH = 64
AGENT_VERSION_MAX_LENGTH = 32
