"""Build context manifest for agent decision observability.

The context manifest contains only character counts and item counts,
never raw content, URLs, or user data. It is safe to persist and audit.
"""

from __future__ import annotations

import json


def build_context_manifest(
    *,
    instruction: str,
    available_tools: list | None,
    observations_for_decision: list,
    evidence_chars: int | None = None,
    model_name: str | None = None,
) -> dict[str, object]:
    """Build a privacy-safe context manifest for an agent decision.

    Args:
        instruction: The agent's system prompt/instruction text.
        available_tools: List of tool definitions visible to the agent
            (None for roles that have no tool catalog).
        observations_for_decision: List of observation projections used
            for this decision.
        evidence_chars: Total characters in the evidence corpus (if known).
        model_name: Name of the model used for the decision (if known).

    Returns:
        A dict with only counts and lengths:
        - system_prompt_chars: Length of the instruction string
        - tool_catalog_count: Number of tools available
        - tool_catalog_chars: Total serialized length of available_tools
        - observation_count: Number of observations
        - observation_chars: Total serialized length of observations
        - evidence_chars: Evidence corpus character count (if supplied)
        - model_name: Model name string (if supplied)
    """
    tool_catalog = available_tools or []
    return {
        "system_prompt_chars": len(instruction),
        "tool_catalog_count": len(tool_catalog),
        "tool_catalog_chars": len(
            json.dumps(tool_catalog, ensure_ascii=False, separators=(",", ":"))
        ),
        "observation_count": len(observations_for_decision),
        "observation_chars": len(
            json.dumps(observations_for_decision, ensure_ascii=False, separators=(",", ":"))
        ),
        "evidence_chars": evidence_chars,
        "model_name": model_name,
    }
