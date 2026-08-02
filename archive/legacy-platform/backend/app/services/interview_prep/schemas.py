"""Runtime result shapes for the interview-prep skill."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class InterviewPrepResult:
    """Outcome of one interview-prep run.

    ``status`` is one of ``succeeded`` / ``needs_manual_review`` / ``failed``,
    mirroring the job-discovery / company-research / resume-tailoring vocabulary.
    ``content`` (the five normalized sections) is populated only on ``succeeded``
    and on ``needs_manual_review`` when an artifact publish failure happened
    after a successful generation.  ``block_reason`` is set only for
    ``needs_manual_review``.  ``agent_version`` is set whenever the generate
    script produced a result file.
    """

    status: str
    summary: str
    content: dict[str, list[str]] = field(default_factory=dict)
    evidence_refs: list[dict[str, Any]] = field(default_factory=list)
    block_reason: str | None = None
    agent_version: str | None = None
    last_error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status == "succeeded"
