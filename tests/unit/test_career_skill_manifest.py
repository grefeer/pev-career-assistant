"""Canonical business Skill boundaries for the personal career assistant."""

from __future__ import annotations

from backend.app.services.career_skills.manifest import (
    CAREER_SKILL_MANIFESTS,
    build_career_skill_registry,
    get_career_skill_manifest,
)
from backend.app.services.career_skills.registry import build_career_tool_registry


def test_manifest_exposes_exactly_three_agent_selectable_business_skills() -> None:
    """Company research and tracking cannot accidentally become extra Agents."""
    assert set(CAREER_SKILL_MANIFESTS) == {
        "job-discovery",
        "job-matching",
        "resume-tailoring",
    }
    assert get_career_skill_manifest("job-discovery").requires_evidence is True
    assert get_career_skill_manifest("resume-tailoring").requires_evidence is True


def test_unknown_business_skill_has_no_implicit_fallback() -> None:
    """An Agent can only choose a reviewed manifest, never an invented skill."""
    assert get_career_skill_manifest("company-research") is None
    assert get_career_skill_manifest("application-tracking") is None


def test_all_runtime_career_skills_resolve_to_canonical_packages() -> None:
    registry = build_career_skill_registry(build_career_tool_registry())
    assert registry.names() == frozenset(CAREER_SKILL_MANIFESTS)
    for definition in registry.definitions():
        assert definition.package_path
        assert definition.package_instructions
        assert "canonical" in registry.prompt_policy([definition.name]).lower()
