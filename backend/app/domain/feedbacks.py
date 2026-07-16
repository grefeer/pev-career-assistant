from __future__ import annotations

from enum import StrEnum


FEEDBACK_CATEGORIES = frozenset({
    "closed",
    "application_channel_unavailable",
    "content_changed",
    "incorrect_information",
})


class JobFeedbackCategory(StrEnum):
    CLOSED = "closed"
    APPLICATION_CHANNEL_UNAVAILABLE = "application_channel_unavailable"
    CONTENT_CHANGED = "content_changed"
    INCORRECT_INFORMATION = "incorrect_information"
