"""Build context manifest for agent decision observability.

The context manifest contains only character counts and item counts,
never raw content, URLs, or user data. It is safe to persist and audit.
"""

from __future__ import annotations

import json
import re


def compute_evidence_chars(observed_public_evidence: list[dict] | None) -> int:
    """Compute total character count of visible text in observed public evidence.

    Only counts lengths, never the raw content itself. Returns 0 for empty/None.

    Args:
        observed_public_evidence: List of evidence dicts from context.metadata,
            each optionally containing a "visible_text" string key.

    Returns:
        Sum of len(visible_text) across all evidence items.
    """
    if not observed_public_evidence:
        return 0
    total = 0
    for item in observed_public_evidence:
        if isinstance(item, dict):
            visible_text = item.get("visible_text")
            if isinstance(visible_text, str):
                total += len(visible_text)
    return total


def prompt_section_stats(instruction: str) -> dict[str, int]:
    """Per-section char counts, keyed by section header text (without the '## ' prefix).

    Sections are delimited by lines beginning with '## '. Characters between a
    header and the next header (or end) belong to that section. Text before the
    first '## ' header (if any) is keyed '_preamble'. Returns {} for an empty
    string. Counts characters only, never raw content.

    Args:
        instruction: The agent's system prompt/instruction text, potentially
            with section headers.

    Returns:
        Dict mapping section names (without '## ' prefix) to character counts.
        Header lines themselves are counted in the section they introduce.
    """
    if not instruction:
        return {}

    # Find all section headers with their positions
    header_pattern = re.compile(r"^## (.+)$", re.MULTILINE)
    matches = list(header_pattern.finditer(instruction))

    if not matches:
        # No headers: count everything as preamble
        return {"_preamble": len(instruction)}

    stats: dict[str, int] = {}

    # Preamble: everything before the first header
    first_start = matches[0].start()
    if first_start > 0:
        stats["_preamble"] = first_start

    # Calculate each section's length
    for i, match in enumerate(matches):
        section_name = match.group(1)
        section_start = match.start()
        if i + 1 < len(matches):
            next_start = matches[i + 1].start()
            section_length = next_start - section_start
        else:
            section_length = len(instruction) - section_start
        stats[section_name] = section_length

    return stats


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
        "prompt_sections": prompt_section_stats(instruction),
    }
