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
    for candidate in _iter_result_candidates(raw):
        parsed = _coerce_result_dict(candidate)
        if parsed is not None:
            return DiscoveryRunResult(**_known_result_fields(parsed))

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
