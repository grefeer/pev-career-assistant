"""Tests for the runtime-neutral SkillDefinition boundary."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.app.services.agent_runtime.schemas import (
    ExecutionPlan,
    PlanStep,
    StepInputRef,
    StepOutputRef,
    ToolObservation,
)
from backend.app.services.agent_runtime.skill_definition import (
    ArtifactPort,
    CompletionContract,
    SkillDefinition,
    SkillRegistry,
    _instruction_excerpt,
)
from backend.app.services.agent_runtime.skill_package import discover_skill_packages
from backend.app.domain.agent_runtime import AgentRole, ComplexityLevel
from backend.app.services.agent_runtime.schemas import AgentTaskRequest


def test_canonical_skill_packages_are_discoverable() -> None:
    root = Path(__file__).resolve().parents[2] / "skill"
    packages = discover_skill_packages(root)

    names = {package.name for package in packages}
    assert {"job-discovery", "resume-tailoring"} <= names
    job_discovery = next(package for package in packages if package.name == "job-discovery")
    assert job_discovery.description
    assert job_discovery.path.name == "job-discovery"


def test_completion_contract_rejects_prose_without_a_deliverable() -> None:
    registry = SkillRegistry(
        [
            SkillDefinition(
                name="example",
                completion_contract=CompletionContract(frozenset({"produce-report"})),
            )
        ]
    )
    step = PlanStep(
        step_id="report",
        objective="Produce a report",
        allowed_skills=["example"],
    )

    assert registry.has_completion_contract(step) is True
    assert registry.completion_evidence_gate(step, [], summary="made up") is False
    assert registry.completion_evidence_gate(
        step,
        [
            ToolObservation(
                tool_name="produce-report",
                status="succeeded",
                output={"ok": True},
            )
        ],
        summary="tool-backed",
    ) is True


def test_private_context_is_projected_by_skill() -> None:
    registry = SkillRegistry(
        [
            SkillDefinition(
                name="profile-aware",
                context_keys=frozenset({"confirmed_profile_facts"}),
            )
        ]
    )

    assert registry.project_private_context(
        ["profile-aware"],
        {
            "confirmed_profile_facts": {"skills": ["Python"]},
            "access_token": "must-not-leak",
        },
    ) == {"confirmed_profile_facts": {"skills": ["Python"]}}


def test_prompt_policy_keeps_every_activated_skill_visible() -> None:
    registry = SkillRegistry(
        [
            SkillDefinition(
                name=name,
                description=f"{name} description",
                package_instructions=(f"{name} head " + ("middle " * 900) + f" {name} tail"),
            )
            for name in ("one", "two", "three", "four")
        ]
    )

    policy = registry.prompt_policy(["one", "two", "three", "four"])

    assert len(policy) <= 4_000
    for name in ("one", "two", "three", "four"):
        assert f"Skill: {name}" in policy
        assert f"{name} tail" in policy


def test_instruction_excerpt_preserves_package_tail() -> None:
    excerpt = _instruction_excerpt("head " + ("middle " * 100) + " PEV boundary", max_chars=40)

    assert "head" in excerpt
    assert "PEV boundary" in excerpt
    assert "canonical Skill middle omitted" in excerpt


def test_artifact_inputs_must_be_explicit_dependencies() -> None:
    task = AgentTaskRequest(goal="workflow", allowed_skills=["one", "two"])
    with pytest.raises(ValueError, match="artifact input source"):
        ExecutionPlan(
            task=task,
            created_by=AgentRole.planner,
            complexity=ComplexityLevel.L2,
            success_criteria=["done"],
            steps=[
                PlanStep(
                    step_id="one",
                    objective="create",
                    allowed_skills=["one"],
                    outputs=[StepOutputRef(name="artifact")],
                ),
                PlanStep(
                    step_id="two",
                    objective="consume",
                    allowed_skills=["two"],
                    inputs=[
                        StepInputRef(
                            kind="artifact", name="artifact", from_step="one"
                        )
                    ],
                ),
            ],
        )


def test_skill_registry_compiles_artifact_port_contracts() -> None:
    registry = SkillRegistry(
        [
            SkillDefinition(
                name="matcher",
                input_ports=(ArtifactPort("job", frozenset({"structured_job_details"})),),
                output_ports=(ArtifactPort("report", frozenset({"job_matching_report"})),),
            )
        ]
    )
    valid = PlanStep(
        step_id="match",
        objective="match",
        allowed_skills=["matcher"],
        inputs=[
            StepInputRef(
                kind="artifact",
                name="jd",
                from_step="discover",
                artifact_type="structured_job_details",
            )
        ],
        outputs=[StepOutputRef(name="report", artifact_type="job_matching_report")],
    )
    invalid = valid.model_copy(
        update={
            "outputs": [StepOutputRef(name="report", artifact_type="public_job_page")]
        }
    )

    assert registry.validate_step_ports(valid) is None
    assert "produce only" in (registry.validate_step_ports(invalid) or "")


def test_skill_registry_normalizes_only_the_known_matching_alias() -> None:
    registry = SkillRegistry()
    step = PlanStep(
        step_id="match",
        objective="match",
        allowed_skills=["job-matching"],
        outputs=[StepOutputRef(name="best", artifact_type="match_result")],
    )

    normalized = registry.normalize_step_ports(step)

    assert normalized.outputs[0].artifact_type == "job_matching_report"
    assert step.outputs[0].artifact_type == "match_result"
