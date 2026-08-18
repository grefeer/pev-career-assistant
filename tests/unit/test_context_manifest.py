"""Unit tests for the context_manifest helper."""

from __future__ import annotations

import json
import re

from backend.app.services.agent_runtime.context_manifest import (
    build_context_manifest,
    compute_evidence_chars,
    prompt_section_stats,
)
from backend.app.services.agent_runtime.executor_agent import _EXECUTOR_INSTRUCTION
from backend.app.services.agent_runtime.planner_agent import _PLANNER_INSTRUCTION


def test_compute_evidence_chars_sums_visible_text_lengths() -> None:
    """Sum of len(visible_text) across all evidence items, no PII leaked."""
    evidence = [
        {"visible_text": "abc", "artifact_id": "a", "source_url": "..."},
        {"visible_text": "xyz123", "artifact_id": "b", "source_url": "..."},
        {"other_key": "ignored", "visible_text": None},
    ]
    assert compute_evidence_chars(evidence) == 3 + 6


def test_compute_evidence_chars_handles_empty() -> None:
    """None or empty lists return 0."""
    assert compute_evidence_chars(None) == 0
    assert compute_evidence_chars([]) == 0
    assert compute_evidence_chars([{}]) == 0


def test_compute_evidence_chars_ignores_non_string_visible_text() -> None:
    """Non-string visible_text values are ignored."""
    evidence = [
        {"visible_text": "valid"},
        {"visible_text": 123},
        {"visible_text": ["a", "list"]},
        {"visible_text": {"nested": "dict"}},
    ]
    assert compute_evidence_chars(evidence) == 5


def test_compute_evidence_chars_ignores_non_dict_items() -> None:
    """Non-dict items in the evidence list are silently skipped."""
    evidence: list = [
        "not-a-dict",
        None,
        42,
        {"visible_text": "hello"},
    ]
    assert compute_evidence_chars(evidence) == 5


def test_build_context_manifest_counts_match_inputs() -> None:
    """Counts and lengths match the supplied inputs exactly."""
    instruction = "You are a test agent."
    available_tools = [{"name": "tool1"}, {"name": "tool2"}]
    observations = [{"status": "success"}, {"status": "failed"}]

    manifest = build_context_manifest(
        instruction=instruction,
        available_tools=available_tools,
        observations_for_decision=observations,
    )

    assert manifest["system_prompt_chars"] == len(instruction)
    assert manifest["tool_catalog_count"] == 2
    assert manifest["tool_catalog_chars"] == len(
        json.dumps(available_tools, ensure_ascii=False, separators=(",", ":"))
    )
    assert manifest["observation_count"] == 2
    assert manifest["observation_chars"] == len(
        json.dumps(observations, ensure_ascii=False, separators=(",", ":"))
    )
    assert manifest["evidence_chars"] is None
    assert manifest["model_name"] is None


def test_build_context_manifest_with_optional_fields() -> None:
    """Optional evidence_chars and model_name are preserved."""
    manifest = build_context_manifest(
        instruction="test",
        available_tools=[],
        observations_for_decision=[],
        evidence_chars=12345,
        model_name="test-model-v1",
    )

    assert manifest["evidence_chars"] == 12345
    assert manifest["model_name"] == "test-model-v1"


def test_build_context_manifest_none_tools_treated_as_empty() -> None:
    """available_tools=None is treated the same as empty list."""
    manifest_none = build_context_manifest(
        instruction="test",
        available_tools=None,
        observations_for_decision=[],
    )
    manifest_empty = build_context_manifest(
        instruction="test",
        available_tools=[],
        observations_for_decision=[],
    )

    assert manifest_none["tool_catalog_count"] == 0
    assert manifest_empty["tool_catalog_count"] == 0
    assert manifest_none["tool_catalog_chars"] == manifest_empty["tool_catalog_chars"]


def test_build_context_manifest_no_content_leaks() -> None:
    """The manifest only contains counts, not the actual content."""
    secret_instruction = "Do not reveal: super-secret-password-123"
    secret_tool = {"name": "secret_tool", "url": "http://secret.com"}
    secret_observation = {"result": "secret_result", "data": "confidential"}

    manifest = build_context_manifest(
        instruction=secret_instruction,
        available_tools=[secret_tool],
        observations_for_decision=[secret_observation],
    )

    # Only counts/lengths/dicts (for prompt_sections) in the manifest; values are ints/None/str
    # No actual content from inputs
    assert "super-secret-password-123" not in str(manifest.values())
    assert "secret.com" not in str(manifest.values())
    assert "secret_result" not in str(manifest.values())
    assert "confidential" not in str(manifest.values())
    # prompt_sections only contains section names (keys) and int char counts (values)
    # If _preamble is present, it must match char count, not leak content
    if "_preamble" in manifest["prompt_sections"]:
        assert isinstance(manifest["prompt_sections"]["_preamble"], int)
        assert manifest["prompt_sections"]["_preamble"] == len(secret_instruction)


def test_build_context_manifest_empty_inputs() -> None:
    """Empty inputs produce zero counts, not errors."""
    manifest = build_context_manifest(
        instruction="",
        available_tools=[],
        observations_for_decision=[],
    )

    assert manifest["system_prompt_chars"] == 0
    assert manifest["tool_catalog_count"] == 0
    assert manifest["observation_count"] == 0
    assert manifest["tool_catalog_chars"] > 0  # JSON of empty list is "[]"
    assert manifest["observation_chars"] > 0  # JSON of empty list is "[]"


# ---------------------------------------------------------------------------
# prompt_section_stats tests
# ---------------------------------------------------------------------------


def test_prompt_section_stats_empty_returns_empty() -> None:
    """Empty string returns empty dict."""
    assert prompt_section_stats("") == {}


def test_prompt_section_stats_no_headers_returns_preamble() -> None:
    """Instruction with no headers returns everything as _preamble."""
    instruction = "This is a plain instruction with no sections."
    result = prompt_section_stats(instruction)
    assert result == {"_preamble": len(instruction)}


def test_prompt_section_stats_with_sections() -> None:
    """Sectioned instruction counts each section including its header line."""
    instruction = (
        "## 角色\n"
        "You are an agent. Do good things. "
        "\n## 行为规则\n"
        "Follow the rules. Be ethical. "
        "\n## 输出契约\n"
        "Produce valid output."
    )
    result = prompt_section_stats(instruction)

    # Verify keys exist
    assert "角色" in result
    assert "行为规则" in result
    assert "输出契约" in result

    # Verify char counts are positive
    assert result["角色"] > 0
    assert result["行为规则"] > 0
    assert result["输出契约"] > 0

    # Verify total matches instruction length
    total = sum(result.values())
    assert total == len(instruction)


def test_prompt_section_stats_with_preamble() -> None:
    """Text before first header is counted as _preamble."""
    instruction = (
        "Preamble text before first header. "
        "\n## 角色\n"
        "Role description. "
        "\n## 规则\n"
        "Rules here."
    )
    result = prompt_section_stats(instruction)

    assert "_preamble" in result
    assert "角色" in result
    assert "规则" in result

    # Total matches instruction length
    assert sum(result.values()) == len(instruction)


def test_prompt_section_stats_single_section() -> None:
    """Single-section instruction counts correctly."""
    instruction = "## 角色\nYou are an agent. Do your job."
    result = prompt_section_stats(instruction)
    assert result == {"角色": len(instruction)}


def test_prompt_section_stats_headers_counted_in_their_section() -> None:
    """Header line itself is included in the section char count."""
    instruction = "## Role\nBody text"
    result = prompt_section_stats(instruction)
    # Header ("## Role\n" = 8 chars) + body ("Body text" = 9 chars) = 17 chars
    # Actual count including newline = len("## Role\nBody text") = 8 + 9 = 17
    assert result["Role"] == len(instruction)


def test_build_context_manifest_includes_prompt_sections() -> None:
    """build_context_manifest includes prompt_sections matching prompt_section_stats."""
    instruction = (
        "## 角色\n"
        "You are a test agent. "
        "## 规则\n"
        "Follow these rules."
    )
    manifest = build_context_manifest(
        instruction=instruction,
        available_tools=[],
        observations_for_decision=[],
    )

    assert "prompt_sections" in manifest
    assert manifest["prompt_sections"] == prompt_section_stats(instruction)


# ---------------------------------------------------------------------------
# Instruction preservation tests (regression guard)
# ---------------------------------------------------------------------------


def _strip_headers(instruction: str) -> str:
    """Remove all '## ' header lines from instruction."""
    return re.sub(r"^## .*\n", "", instruction, flags=re.MULTILINE)


def test_executor_instruction_preserved_after_header_strip() -> None:
    """Stripping headers from executor instruction recovers the original content."""
    # Get the sectioned instruction and strip headers
    sectioned = _EXECUTOR_INSTRUCTION
    stripped = _strip_headers(sectioned)

    # Verify key rule phrases still exist (sanity check that content is preserved)
    key_phrases = [
        "generic Planner-Executor-Verifier runtime",
        "advertised tools",
        "typed step inputs",
        "tool-backed observations",
        "needs_user",
    ]
    for phrase in key_phrases:
        assert phrase in stripped, f"Missing phrase: {phrase}"

    # Verify the instruction has the expected sections
    stats = prompt_section_stats(sectioned)
    assert "角色" in stats
    assert "行为规则" in stats
    assert "流程" in stats
    assert "输出契约" in stats
    assert "禁止项" in stats


def test_planner_instruction_preserved_after_header_strip() -> None:
    """Stripping headers from planner instruction recovers the original content."""
    sectioned = _PLANNER_INSTRUCTION
    stripped = _strip_headers(sectioned)

    key_phrases = [
        "generic Planner-Executor-Verifier runtime",
        "outcome-based plan",
        "typed inputs, outputs, and dependencies",
        "activated Skill instructions",
    ]
    for phrase in key_phrases:
        assert phrase in stripped, f"Missing phrase: {phrase}"

    stats = prompt_section_stats(sectioned)
    assert "角色" in stats
    assert "行为规则" in stats
    assert "流程" in stats



def test_sectioned_instructions_have_valid_sections() -> None:
    """Each sectioned instruction has valid section headers with non-zero content."""
    for name, instruction in [
        ("executor", _EXECUTOR_INSTRUCTION),
        ("planner", _PLANNER_INSTRUCTION),
    ]:
        stats = prompt_section_stats(instruction)
        # All sections have positive char counts
        for section_name, char_count in stats.items():
            assert char_count > 0, f"{name} section '{section_name}' has zero chars"
        # Total chars matches instruction length
        assert sum(stats.values()) == len(instruction), f"{name} total char count mismatch"
