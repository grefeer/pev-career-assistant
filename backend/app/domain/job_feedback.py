from __future__ import annotations

import sys

if sys.version_info >= (3, 11):
    from enum import StrEnum
else:
    from enum import Enum

    class StrEnum(str, Enum):
        """Minimal StrEnum polyfill for Python < 3.11."""
        pass

import re


class JobFeedbackCategory(StrEnum):
    CLOSED = "closed"
    APPLICATION_CHANNEL_UNAVAILABLE = "application_channel_unavailable"
    CONTENT_CHANGED = "content_changed"
    INCORRECT_INFORMATION = "incorrect_information"


class JobFeedbackStatus(StrEnum):
    OPEN = "open"
    ACCEPTED = "accepted"
    RESOLVED = "resolved"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


class JobFeedbackAction(StrEnum):
    SUBMITTED = "submitted"
    UPDATED = "updated"
    WITHDRAWN = "withdrawn"
    ACCEPTED = "accepted"
    RESOLVED = "resolved"
    REJECTED = "rejected"


class FeedbackStudentAction(StrEnum):
    UPSERT = "upsert"
    WITHDRAW = "withdraw"


class FeedbackAdminDecision(StrEnum):
    ACCEPT = "accept"
    RESOLVE = "resolve"
    REJECT = "reject"


FEEDBACK_NOTE_MAX_LENGTH = 1000
IDEMPOTENCY_KEY_MIN_LENGTH = 16
IDEMPOTENCY_KEY_MAX_LENGTH = 128
IDEMPOTENCY_KEY_PATTERN = re.compile(r"[A-Za-z0-9._:-]{16,128}")

STUDENT_WITHDRAW_FROM = frozenset(
    {JobFeedbackStatus.OPEN, JobFeedbackStatus.ACCEPTED}
)
ADMIN_TRANSITIONS = {
    FeedbackAdminDecision.ACCEPT: (
        frozenset({JobFeedbackStatus.OPEN}),
        JobFeedbackStatus.ACCEPTED,
        JobFeedbackAction.ACCEPTED,
    ),
    FeedbackAdminDecision.RESOLVE: (
        frozenset({JobFeedbackStatus.OPEN, JobFeedbackStatus.ACCEPTED}),
        JobFeedbackStatus.RESOLVED,
        JobFeedbackAction.RESOLVED,
    ),
    FeedbackAdminDecision.REJECT: (
        frozenset({JobFeedbackStatus.OPEN, JobFeedbackStatus.ACCEPTED}),
        JobFeedbackStatus.REJECTED,
        JobFeedbackAction.REJECTED,
    ),
}
