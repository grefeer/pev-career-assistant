from __future__ import annotations

import sys

if sys.version_info >= (3, 11):
    from enum import StrEnum
else:
    from enum import Enum

    class StrEnum(str, Enum):
        """Minimal StrEnum polyfill for Python < 3.11."""

        pass


class JobInteractionType(StrEnum):
    """User behavior toward a job. Feeds the relevance feedback loop."""

    VIEWED = "viewed"
    DISMISSED = "dismissed"
    SAVED = "saved"
    HIDDEN = "hidden"
    CLICKED_APPLY = "clicked_apply"


class WorkModePreference(StrEnum):
    """Where the user prefers to work."""

    ONSITE = "onsite"
    HYBRID = "hybrid"
    REMOTE = "remote"
