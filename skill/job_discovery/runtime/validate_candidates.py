"""Evidence-quality gate for the PEV ``job-discovery`` Skill.

Deterministic port of the skill's ``validate.py --verify`` quality checks
(staleness / vagueness / non-JD text) as a career tool. The skill script
operates on its own JSON file format; this module applies the same three
checks to observed page evidence, so the Verifier/Executor can reject or
re-plan a low-quality artifact without launching a subprocess. Structural
validation (required fields, schema) is already enforced by pydantic and
the extract path -- this gate only adds evidence-quality gates.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator

from backend.app.services.agent_runtime.tool_context import ToolContext
from skill.job_discovery.runtime.job_discovery import _find_observed_evidence

# Same constants as skill/job-discovery/scripts/validate.py.
_MIN_DESCRIPTION_LENGTH = 50
_STALE_YEAR_THRESHOLD = 2024
_JD_KEYWORDS = (
    "岗位",
    "职位",
    "招聘",
    "要求",
    "职责",
    "job",
    "position",
    "requirement",
    "responsibility",
    "qualification",
)
_YEAR_RE = re.compile(r"\b(20[0-9]{2})\b")


class ValidateObservedCandidatesInput(BaseModel):
    """A bounded set of previously observed evidence artifacts to check."""

    artifact_ids: list[str] = Field(min_length=1, max_length=10)

    @field_validator("artifact_ids")
    @classmethod
    def validate_artifact_ids(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values]
        if any(not value for value in cleaned) or len(set(cleaned)) != len(cleaned):
            raise ValueError("artifact_ids must be non-empty and unique")
        return cleaned


class CandidateIssue(BaseModel):
    """One evidence-quality finding for one artifact."""

    artifact_id: str
    code: str
    detail: str


class ValidateObservedCandidatesOutput(BaseModel):
    """Aggregate quality verdict; valid is False when any issue exists."""

    valid: bool
    issues: list[CandidateIssue]


def validate_observed_candidates(
    context: ToolContext, payload: ValidateObservedCandidatesInput
) -> ValidateObservedCandidatesOutput:
    """Run the staleness / vagueness / non-JD gates over observed evidence.

    Missing or text-less artifacts are reported as issues so the caller can
    tell "extract will fail" apart from "extract succeeded but the page is
    low quality".
    """
    issues: list[CandidateIssue] = []
    for artifact_id in payload.artifact_ids:
        evidence = _find_observed_evidence(context, artifact_id)
        if evidence is None:
            issues.append(
                CandidateIssue(
                    artifact_id=artifact_id,
                    code="evidence_not_found",
                    detail="no observed evidence with this artifact_id",
                )
            )
            continue
        visible_text = evidence.get("visible_text")
        if not isinstance(visible_text, str) or not visible_text:
            issues.append(
                CandidateIssue(
                    artifact_id=artifact_id,
                    code="evidence_incomplete",
                    detail="evidence has no visible_text",
                )
            )
            continue
        issues.extend(_quality_issues(artifact_id, visible_text))
    return ValidateObservedCandidatesOutput(valid=not issues, issues=issues)


def _quality_issues(artifact_id: str, text: str) -> list[CandidateIssue]:
    """The three evidence-quality gates (validate.py --verify semantics)."""
    issues: list[CandidateIssue] = []
    for year in _YEAR_RE.findall(text):
        if 2000 < int(year) < _STALE_YEAR_THRESHOLD:
            issues.append(
                CandidateIssue(
                    artifact_id=artifact_id,
                    code="stale_year",
                    detail=f"references year {year} (threshold: {_STALE_YEAR_THRESHOLD})",
                )
            )
            break
    stripped = text.strip()
    if len(stripped) < _MIN_DESCRIPTION_LENGTH:
        issues.append(
            CandidateIssue(
                artifact_id=artifact_id,
                code="vague_description",
                detail=f"{len(stripped)} chars (min: {_MIN_DESCRIPTION_LENGTH})",
            )
        )
    lowered = text.lower()
    if len(text) > 100:
        keyword_hits = sum(1 for keyword in _JD_KEYWORDS if keyword in lowered)
        if keyword_hits < 2:
            issues.append(
                CandidateIssue(
                    artifact_id=artifact_id,
                    code="non_jd_text",
                    detail=f"only {keyword_hits} JD keywords found",
                )
            )
    return issues
