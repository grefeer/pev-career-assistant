"""Shared parsing and validation for discovery supervisor results."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import Any, Iterator

from backend.app.services.job_discovery.schemas import DiscoveryRunResult


_RESULT_FIELDS = {"status", "block_reason", "evidence", "candidates", "summary"}
_FENCED_JSON_PATTERN = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


class AgentResultParseError(ValueError):
    """Raised when an agent result does not contain a structured result."""

    def __init__(self, *, message_types: list[str], message_count: int) -> None:
        super().__init__("Could not parse structured output from agent result")
        self.message_types = message_types
        self.message_count = message_count


def parse_agent_result(raw: Any) -> DiscoveryRunResult:
    """Parse the first valid discovery result without retaining message bodies."""
    tool_evidence, tool_candidates = _collect_tool_outputs(raw)
    for candidate in _iter_result_candidates(raw):
        parsed = _coerce_result_dict(candidate)
        if parsed is not None:
            return _merge_tool_outputs(
                DiscoveryRunResult(**_known_result_fields(parsed)),
                evidence=tool_evidence,
                candidates=tool_candidates,
            )

    if tool_evidence or tool_candidates:
        return DiscoveryRunResult(
            status="succeeded" if tool_candidates else "needs_manual_review",
            block_reason=None if tool_candidates else "parse_failed",
            evidence=tool_evidence,
            candidates=tool_candidates,
            summary="Recovered discovery output from tool results",
        )

    messages = raw.get("messages", []) if isinstance(raw, dict) else []
    if not isinstance(messages, list):
        messages = []
    raise AgentResultParseError(
        message_types=[type(item).__name__ for item in messages],
        message_count=len(messages),
    )


def enforce_result_invariants(result: DiscoveryRunResult) -> DiscoveryRunResult:
    """Ensure completed outcomes always carry at least one candidate."""
    if result.status == "succeeded" and not result.candidates:
        return replace(result, status="failed", block_reason="parse_failed")
    if result.status == "partial_success" and not result.candidates:
        return replace(
            result,
            status="needs_manual_review",
            block_reason=result.block_reason or "parse_failed",
        )
    return result


def _iter_result_candidates(raw: Any) -> Iterator[Any]:
    if isinstance(raw, dict):
        if "structured_response" in raw:
            yield raw["structured_response"]
        yield raw
        messages = raw.get("messages", [])
        if isinstance(messages, list):
            for message in reversed(messages):
                yield getattr(message, "content", message)
    else:
        yield raw


def _coerce_result_dict(candidate: Any) -> dict[str, Any] | None:
    if hasattr(candidate, "model_dump"):
        candidate = candidate.model_dump()
    if isinstance(candidate, dict):
        return candidate if "status" in candidate else None
    if isinstance(candidate, list):
        for block in candidate:
            if isinstance(block, dict) and block.get("type") == "text":
                parsed = _coerce_result_dict(block.get("text"))
                if parsed is not None:
                    return parsed
        return None
    if not isinstance(candidate, str):
        return None

    content = candidate.strip()
    fenced = _FENCED_JSON_PATTERN.match(content)
    if fenced:
        content = fenced.group(1).strip()
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) and "status" in parsed else None


def _known_result_fields(parsed: dict[str, Any]) -> dict[str, Any]:
    return {name: value for name, value in parsed.items() if name in _RESULT_FIELDS}


def _collect_tool_outputs(raw: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Collect persisted result data emitted by navigation and packaging tools."""
    messages = raw.get("messages", []) if isinstance(raw, dict) else []
    if not isinstance(messages, list):
        return [], []

    evidence: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for message in messages:
        tool_name = getattr(message, "name", None)
        payload = _coerce_json_value(getattr(message, "content", None))
        if tool_name in {"run_web_navigation", "extract_rendered_job_evidence"}:
            if isinstance(payload, dict):
                pages = payload.get("evidence_pages") or payload.get("evidence") or []
                if isinstance(pages, list):
                    evidence.extend(item for item in pages if isinstance(item, dict))
        elif tool_name == "package_candidates" and isinstance(payload, list):
            candidates.extend(item for item in payload if isinstance(item, dict))
    return evidence, candidates


def _coerce_json_value(candidate: Any) -> Any:
    if not isinstance(candidate, str):
        return None
    content = candidate.strip()
    fenced = _FENCED_JSON_PATTERN.match(content)
    if fenced:
        content = fenced.group(1).strip()
    try:
        return json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None


def _merge_tool_outputs(
    result: DiscoveryRunResult,
    *,
    evidence: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
) -> DiscoveryRunResult:
    return replace(
        result,
        evidence=_unique_items([*result.evidence, *evidence]),
        candidates=_unique_items([*result.candidates, *candidates]),
    )


def _unique_items(items: list[Any]) -> list[Any]:
    unique: list[Any] = []
    seen: set[str] = set()
    for item in items:
        key = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique
