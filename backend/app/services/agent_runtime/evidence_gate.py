"""Compatibility facade for deterministic skill contracts.

Pure rules for the adaptive PEV harness - no database, no model calls:

* ``step_contract_met`` - whether a step's skill deliverable is already
  tool-backed from its observation set (the evidence gate behind the
  verifier/executor termination rescues).
* ``blocked_error_codes`` / ``is_blocked_error`` - whether an error code is a
  human-gated (login/captcha/anti-bot/OCR-off) or deterministic-per-URL
  failure whose retry cannot change the outcome, versus a transient network
  failure where a retry within budget is legitimate.
* ``has_blocked_evidence`` - whether an observation set carries any blocked
  signal (failed error_code, nested batch failures, or a WeChat output marked
  ``needs_manual_review`` even though the tool observation itself succeeded).

Security stance: blocked evidence always ends human-in-the-loop. The rescues
never fire when blocked evidence exists; the retry downgrade converts only the
machine retry loop into a single clean human hand-off.
"""

from __future__ import annotations

from collections.abc import Sequence

from backend.app.services.agent_runtime.schemas import PlanStep, ToolObservation
from backend.app.services.agent_runtime.skill_definition import SkillRegistry

def _legacy_registry() -> SkillRegistry:
    """Load the old career contract only for callers of this compatibility API."""
    from backend.app.services.career_skills.manifest import build_career_skill_registry

    return build_career_skill_registry()


def has_known_deliverable_attempt(
    observations: Sequence[ToolObservation],
    *,
    skill_registry: SkillRegistry | None = None,
) -> bool:
    """Return whether a registered deliverable tool was attempted."""
    registry = skill_registry or _legacy_registry()
    deliverables = frozenset(
        tool
        for definition in registry.definitions()
        if definition.completion_contract
        for tool in definition.completion_contract.deliverable_tools
    )
    return any(observation.tool_name in deliverables for observation in observations)


def blocked_error_codes() -> frozenset[str]:
    """Return the compatibility policy's explicitly blocked codes."""
    policy = _legacy_registry().error_policy
    return policy.blocked_codes | policy.human_required_codes


def is_blocked_error(
    error_code: str,
    *,
    skill_registry: SkillRegistry | None = None,
) -> bool:
    """Return whether a configured policy classifies an error as blocked."""
    registry = skill_registry or _legacy_registry()
    return registry.error_policy.is_blocked(error_code)


def step_contract_met(
    step: PlanStep,
    artifacts: Sequence[ToolObservation],
    *,
    skill_registry: SkillRegistry | None = None,
) -> bool:
    """Evaluate the registered skill contract for a step."""
    return (skill_registry or _legacy_registry()).step_contract_met(step, artifacts)


def completion_evidence_gate(
    step: PlanStep,
    artifacts: Sequence[ToolObservation],
    *,
    summary: str | None = None,
    skill_registry: SkillRegistry | None = None,
) -> bool:
    """Apply the skill contract without making the runtime know its domain."""
    return (skill_registry or _legacy_registry()).completion_evidence_gate(
        step, artifacts, summary=summary
    )


def has_blocked_evidence(
    observations: Sequence[ToolObservation],
    *,
    skill_registry: SkillRegistry | None = None,
) -> bool:
    """True when any observation carries a blocked signal.

    Blocked signals appear in three shapes: a failed observation's
    ``error_code``, a batch fetch's nested ``output.failures[*].error_code``,
    and a WeChat direct-tool output marked ``needs_manual_review`` (reason
    ``ocr_disabled``) even though the tool observation itself succeeded.
    """
    return (skill_registry or _legacy_registry()).has_blocked_evidence(observations)
