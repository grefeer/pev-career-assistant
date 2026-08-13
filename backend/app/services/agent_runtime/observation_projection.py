"""Shared decision-state projection for PEV Agent observations.

The Planner, Executor and Verifier all feed accumulated ``ToolObservation``
values to the model gateway each turn. Projecting each observation once (into
bounded identifiers and short evidence excerpts) instead of re-dumping the full
raw list every turn keeps the per-turn state O(turns) rather than O(turns^2)
and caps the page text that reaches the model. All three roles share the same
projection so a run's observations are represented consistently.

``summarize_observations`` bounds the *list* itself: when the accumulated
projections grow past the character budget, the most-recent projections stay
full (each already bounded per-item by ``observation_for_decision``) while
older ones collapse to identifier-only summary lines. This preserves
early-link evidence as traceable pointers rather than silently dropping it,
and keeps the list the model sees within a bounded character budget.
"""

from __future__ import annotations

import json
from typing import Any

from backend.app.services.agent_runtime.schemas import ToolObservation

#: Maximum visible-text characters exposed per observation or page to the model.
_VISIBLE_TEXT_EXCERPT = 1_200
#: Maximum number of pages/details projected per observation (oldest are dropped).
_MAX_PROJECTED_ITEMS = 10

#: Default number of most-recent observations kept full (not summarized) when
#: the accumulated list exceeds the character budget. Conservative on purpose:
#: the most-recent observations carry the current step's actionable evidence,
#: and the run-level ``max_agent_turns`` default (12) means a healthy step
#: rarely accumulates more than this before completing.
_DEFAULT_KEEP_RECENT_OBSERVATIONS = 5
#: Default character budget for the observation list passed to the model.
#: Matches the run-level evidence budget (48_000) so observations and evidence
#: share the same ceiling; the per-observation projection already bounds each
#: item, so this cap only engages for unusually long observation chains.
_DEFAULT_OBSERVATION_BUDGET_CHARS = 48_000


def observation_for_decision(observation: ToolObservation) -> dict[str, Any]:
    """Give the Agent identifiers and bounded evidence excerpts, never full page bodies."""
    payload = observation.model_dump(mode="json")
    output = payload.get("output")
    if not isinstance(output, dict):
        return payload
    projected = dict(output)
    if isinstance(projected.get("visible_text"), str):
        projected["visible_text"] = projected["visible_text"][:_VISIBLE_TEXT_EXCERPT]
    pages = projected.get("pages")
    if isinstance(pages, list):
        projected["pages"] = [
            page_for_decision(page)
            for page in pages[:_MAX_PROJECTED_ITEMS]
            if isinstance(page, dict)
        ]
    details = projected.get("details")
    if isinstance(details, list):
        projected["details"] = [
            detail_for_decision(detail)
            for detail in details[:_MAX_PROJECTED_ITEMS]
            if isinstance(detail, dict)
        ]
    payload["output"] = projected
    return payload


def record_observation(
    observations: list[ToolObservation],
    observations_for_decision: list[dict[str, Any]],
    observation: ToolObservation,
) -> None:
    """Append a raw observation and its decision projection in lockstep."""
    observations.append(observation)
    observations_for_decision.append(observation_for_decision(observation))


def page_for_decision(page: dict[str, Any]) -> dict[str, Any]:
    """Project one page's traceable identity and a small visible-text excerpt."""
    projected = dict(page)
    visible_text = projected.get("visible_text")
    if isinstance(visible_text, str):
        projected["visible_text"] = visible_text[:_VISIBLE_TEXT_EXCERPT]
    return projected


def detail_for_decision(detail: dict[str, Any]) -> dict[str, Any]:
    """Keep a batch detail trace actionable without copying all JD sections again."""
    candidates = detail.get("candidates")
    titles = [
        candidate.get("title")
        for candidate in candidates
        if isinstance(candidate, dict) and isinstance(candidate.get("title"), str)
    ] if isinstance(candidates, list) else []
    projected = {
        key: detail[key]
        for key in (
            "artifact_id", "candidate_id", "source_artifact_id",
            "source_url", "content_hash",
        )
        if key in detail
    } | {"candidate_titles": titles}
    # Keep bounded candidate references so later turns can select the same
    # evidence without replaying the page body.
    if isinstance(candidates, list):
        refs = []
        for candidate in candidates[:_MAX_PROJECTED_ITEMS]:
            if not isinstance(candidate, dict):
                continue
            ref = {
                key: candidate[key]
                for key in (
                    "artifact_id", "candidate_id", "source_artifact_id",
                    "source_url", "content_hash",
                )
                if isinstance(candidate.get(key), str)
            }
            if ref:
                refs.append(ref)
        if refs:
            projected["candidate_refs"] = refs
    return projected


def summarize_observations(
    observations: list[dict[str, Any]],
    *,
    keep_recent: int = _DEFAULT_KEEP_RECENT_OBSERVATIONS,
    budget_chars: int = _DEFAULT_OBSERVATION_BUDGET_CHARS,
) -> list[dict[str, Any]]:
    """Bound the observation list: keep recent full, collapse older to identifiers.

    Returns a new list where the ``keep_recent`` most-recent observations stay
    full (as projected by :func:`observation_for_decision`) and any older
    observations collapse to single summary lines carrying only
    ``tool_name``/``status``/``source_url``/``content_hash`` - never
    ``visible_text``/``pages``/``details``/``output`` (security hard gate #4:
    summary lines must never leak payloads).

    If the list has ``keep_recent`` or fewer observations, or the total
    serialized character count is within ``budget_chars``, the list is
    returned as a shallow copy (no summarization). This conservative
    short-circuit ensures short, healthy chains never pay a summarization
    penalty and the model always sees the full evidence it just produced.
    """
    if len(observations) <= keep_recent:
        return list(observations)
    if _serialized_chars(observations) <= budget_chars:
        return list(observations)
    # Walk the list by index rather than [-keep_recent:] so that keep_recent=0
    # correctly splits into "all older / none recent" (Python's list[-0:]
    # returns the whole list, not an empty slice).
    split = len(observations) - keep_recent
    recent = list(observations[split:])
    older = list(observations[:split])
    summarized = [_summarize_observation(obs) for obs in older]
    return summarized + recent


def _summarize_observation(observation: dict[str, Any]) -> dict[str, Any]:
    """Collapse one observation to an identifier-only summary line.

    The summary line carries ``tool_name``/``status`` (from the observation
    root) and ``source_url``/``content_hash`` (from its ``output`` dict, when
    present). It NEVER includes ``visible_text``, ``pages``, ``details``, or
    the full ``output`` payload - only traceable identifiers, so a long
    observation chain stays bounded without leaking page bodies.
    """
    summary: dict[str, Any] = {}
    for key in ("tool_name", "status"):
        value = observation.get(key)
        if value is not None:
            summary[key] = value
    output = observation.get("output")
    if isinstance(output, dict):
        for key in (
            "artifact_id", "candidate_id", "source_artifact_id",
            "source_url", "content_hash",
        ):
            value = output.get(key)
            if isinstance(value, str):
                summary[key] = value
    return summary


#: Maximum tool-call history entries projected for the Verifier (newest kept).
_MAX_TOOL_CALL_HISTORY = 15
#: Maximum characters of one tool call's output excerpted for the Verifier.
_TOOL_CALL_OUTPUT_EXCERPT = 200


def tool_call_summary(observation: ToolObservation) -> dict[str, Any]:
    """Bounded tool-call identity + output excerpt for cross-agent review.

    Non-evidence tool outputs (sheet queries, match rankings, dedup/validate
    results) carry no page-evidence keys, so the standard projection leaves
    them opaque to the Verifier: a deliverable call exists in the list but
    its produced content is invisible (R002 "observations 数组为空" class).
    This summary exposes the call's identity and a short output excerpt --
    never the full payload (security hard gate #4) -- so the Verifier can
    confirm a promised tool-backed deliverable actually ran and see what it
    produced.
    """
    summary: dict[str, Any] = {
        "tool_name": observation.tool_name,
        "status": observation.status,
    }
    if observation.error_code is not None:
        summary["error_code"] = observation.error_code
    output = observation.output
    if isinstance(output, dict):
        excerpt = json.dumps(output, ensure_ascii=False, separators=(",", ":"))
        summary["output_excerpt"] = excerpt[:_TOOL_CALL_OUTPUT_EXCERPT]
    return summary


def summarize_tool_call_history(
    observations: list[ToolObservation],
    *,
    max_calls: int = _MAX_TOOL_CALL_HISTORY,
) -> list[dict[str, Any]]:
    """Project the newest tool calls into bounded summaries for the Verifier.

    The Verifier's ``execution_tool_calls`` state is built from the Executor's
    full tool-call history so the deliverable-tool rule in its instruction can
    be checked against the actual calls, not against a prose claim. Bounded by
    ``max_calls`` (newest kept) with each entry bounded by
    :func:`tool_call_summary`.
    """
    return [
        tool_call_summary(observation) for observation in observations[-max_calls:]
    ]


def _serialized_chars(items: list[dict[str, Any]]) -> int:
    """Measure the serialized character count the model gateway would receive."""
    return len(json.dumps(items, ensure_ascii=False, separators=(",", ":")))
