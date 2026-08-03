"""Shared decision-state projection for PEV Agent observations.

The Planner, Executor and Verifier all feed accumulated ``ToolObservation``
values to the model gateway each turn. Projecting each observation once (into
bounded identifiers and short evidence excerpts) instead of re-dumping the full
raw list every turn keeps the per-turn state O(turns) rather than O(turns^2)
and caps the page text that reaches the model. All three roles share the same
projection so a run's observations are represented consistently.
"""

from __future__ import annotations

from typing import Any

from backend.app.services.agent_runtime.schemas import ToolObservation

#: Maximum visible-text characters exposed per observation or page to the model.
_VISIBLE_TEXT_EXCERPT = 1_200
#: Maximum number of pages/details projected per observation (oldest are dropped).
_MAX_PROJECTED_ITEMS = 10


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
    return {
        key: detail[key]
        for key in ("source_artifact_id", "source_url", "content_hash")
        if key in detail
    } | {"candidate_titles": titles}
