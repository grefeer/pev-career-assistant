"""Canonical business Skill boundaries for the personal career assistant."""

from __future__ import annotations

from backend.app.services.career_skills.manifest import (
    CAREER_SKILL_MANIFESTS,
    get_career_skill_manifest,
)


def test_manifest_exposes_exactly_four_agent_selectable_business_skills() -> None:
    """Company research and tracking cannot accidentally become extra Agents."""
    assert set(CAREER_SKILL_MANIFESTS) == {
        "job-discovery",
        "job-matching",
        "resume-tailoring",
        "career-planning",
    }
    assert get_career_skill_manifest("job-discovery").requires_evidence is True
    assert get_career_skill_manifest("resume-tailoring").requires_evidence is True


def test_unknown_business_skill_has_no_implicit_fallback() -> None:
    """An Agent can only choose a reviewed manifest, never an invented skill."""
    assert get_career_skill_manifest("company-research") is None
    assert get_career_skill_manifest("application-tracking") is None
