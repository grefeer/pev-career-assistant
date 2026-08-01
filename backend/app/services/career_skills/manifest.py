"""Reviewed business-Skill metadata used to constrain PEV planning/execution."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CareerSkillManifest:
    """A product-facing Skill boundary, distinct from low-level tool names."""

    name: str
    description: str
    requires_evidence: bool
    supports_user_data: bool


CAREER_SKILL_MANIFESTS: dict[str, CareerSkillManifest] = {
    "job-discovery": CareerSkillManifest(
        name="job-discovery",
        description="Collect and normalize evidence-backed public job descriptions.",
        requires_evidence=True,
        supports_user_data=False,
    ),
    "job-matching": CareerSkillManifest(
        name="job-matching",
        description="Rank jobs against confirmed profile facts and stated preferences.",
        requires_evidence=True,
        supports_user_data=True,
    ),
    "resume-tailoring": CareerSkillManifest(
        name="resume-tailoring",
        description="Produce fact-grounded resume diff operations for one job.",
        requires_evidence=True,
        supports_user_data=True,
    ),
    "career-planning": CareerSkillManifest(
        name="career-planning",
        description="Create JD-grounded search, preparation and interview plans.",
        requires_evidence=True,
        supports_user_data=True,
    ),
}


def get_career_skill_manifest(name: str) -> CareerSkillManifest | None:
    """Return only an explicitly reviewed business Skill."""
    return CAREER_SKILL_MANIFESTS.get(name)
