"""Unit tests for the context_manifest helper."""

from __future__ import annotations

import json

from backend.app.services.agent_runtime.context_manifest import (
    build_context_manifest,
    compute_evidence_chars,
)


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

    # Only counts/lengths in the manifest
    assert all(isinstance(v, (int, type(None), str)) for v in manifest.values())
    # No actual content from inputs
    assert "super-secret-password-123" not in str(manifest.values())
    assert "secret.com" not in str(manifest.values())
    assert "secret_result" not in str(manifest.values())
    assert "confidential" not in str(manifest.values())


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
