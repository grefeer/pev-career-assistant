"""Runtime result shapes for the company-research skill."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class CompanyResearchResult:
    """Outcome of one company-research run.

    ``status`` is one of ``succeeded`` / ``needs_manual_review`` / ``failed``,
    mirroring the job-discovery vocabulary.  ``block_reason`` is set only for
    ``needs_manual_review`` and is a ``CompanyResearchBlockReason`` value.
    ``profile`` and ``openings`` are populated only on ``succeeded``.
    """

    status: str
    summary: str
    profile: dict[str, Any] | None = None
    openings: list[dict[str, Any]] = field(default_factory=list)
    evidence_refs: list[dict[str, Any]] = field(default_factory=list)
    block_reason: str | None = None
    last_error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status == "succeeded"
