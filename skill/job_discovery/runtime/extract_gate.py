"""Regex-first JD extraction with an optional LLM gate (spec §4.3).

The deterministic regex extractor runs first and costs zero tokens; the LLM
extractor is consulted only when the gate is enabled AND the regex output is
empty or low-confidence.  The merge is a strict-Pareto union: regex
candidates are preserved verbatim; LLM candidates whose (title, company)
identity is not already present are appended.

Note: ``ExtractedJobDetails.confidence`` is a non-nullable ``float`` in the
career_skills model, so a missing confidence cannot be represented; the gate
therefore triggers on confidence below ``_LOW_CONFIDENCE_BELOW`` only.
"""

from __future__ import annotations

from typing import Callable

from backend.app.services.agent_runtime.tool_context import ToolContext
from skill.job_discovery.runtime.job_discovery import (
    ExtractObservedJobDetailsInput,
    ExtractObservedJobDetailsOutput,
    extract_observed_job_details,
)

_LOW_CONFIDENCE_BELOW = 0.6


def _needs_llm(result: ExtractObservedJobDetailsOutput) -> bool:
    if not result.candidates:
        return True
    return any(
        candidate.confidence < _LOW_CONFIDENCE_BELOW for candidate in result.candidates
    )


def _identity(candidate) -> tuple[str, str]:
    return ((candidate.title or "").strip(), (candidate.company_name or "").strip())


def _pareto_union(
    base: ExtractObservedJobDetailsOutput, extra: ExtractObservedJobDetailsOutput
) -> ExtractObservedJobDetailsOutput:
    base_ids = {_identity(candidate) for candidate in base.candidates}
    for candidate in extra.candidates:
        if _identity(candidate) not in base_ids:
            base.candidates.append(candidate)
            base_ids.add(_identity(candidate))
    return base


def extract_with_gate(
    context: ToolContext,
    payload: ExtractObservedJobDetailsInput,
    *,
    enabled: bool,
    llm_extractor: (
        Callable[
            [ToolContext, ExtractObservedJobDetailsInput],
            ExtractObservedJobDetailsOutput,
        ]
        | None
    ) = None,
) -> ExtractObservedJobDetailsOutput:
    """Run the deterministic extractor; gate the LLM on low-confidence gaps."""
    result = extract_observed_job_details(context, payload)
    if not enabled or llm_extractor is None or not _needs_llm(result):
        return result
    return _pareto_union(result, llm_extractor(context, payload))
