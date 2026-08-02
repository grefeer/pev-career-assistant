"""Runtime result shapes for the resume-tailoring skill."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ResumeTailoringResult:
    """Outcome of one resume-tailoring run.

    ``status`` is one of ``succeeded`` / ``needs_manual_review`` / ``failed``,
    mirroring the job-discovery and company-research vocabulary.  ``diffs`` is
    populated only on ``succeeded`` (and on ``needs_manual_review`` when the
    diffs failed grounding validation - the human can fix them in place).
    ``block_reason`` is set only for ``needs_manual_review``.  ``validation_*``
    fields are set when the post-generation validation step ran and failed.
    """

    status: str
    summary: str
    diffs: list[dict[str, Any]] = field(default_factory=list)
    evidence_refs: list[dict[str, Any]] = field(default_factory=list)
    block_reason: str | None = None
    validation_code: str | None = None
    validation_index: int | None = None
    agent_version: str | None = None
    last_error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status == "succeeded"
