"""Input/output contracts shared by the autonomous PEV roles."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.app.domain.agent_runtime import AgentRole, ComplexityLevel
from backend.app.services.agent_runtime.schemas import (
    AgentBudget,
    AgentTaskRequest,
    ExecutionPlan,
    ExecutorDecision,
    ExecutorResult,
    PlanStep,
    PlannerDecision,
    PlannerResult,
    ToolObservation,
    VerifierDecision,
    VerifierResult,
)


def test_task_request_normalizes_goal_and_rejects_duplicate_skill_authority() -> None:
    """Duplicate Skill names would obscure the Planner's actual authority."""
    with pytest.raises(ValidationError, match="unique"):
        AgentTaskRequest(
            goal="  找 AI Agent 岗位  ",
            allowed_skills=["job-discovery", "job-discovery"],
        )


def test_task_request_requires_a_real_goal_and_at_least_one_allowed_skill() -> None:
    """The runtime must never open an unconstrained agent run."""
    with pytest.raises(ValidationError, match="goal"):
        AgentTaskRequest(goal="   ", allowed_skills=["job-discovery"])
    with pytest.raises(ValidationError, match="allowed_skills"):
        AgentTaskRequest(goal="找岗位", allowed_skills=[])


def test_budget_rejects_a_zero_turn_agent_loop() -> None:
    """A budget of zero would make an Agent role a decorative node."""
    with pytest.raises(ValidationError):
        AgentBudget(max_agent_turns=0, max_tool_calls=1, max_replans=0)


def test_execution_plan_cannot_grant_executor_an_unapproved_skill() -> None:
    """Planner output cannot expand the user/runtime-provided tool authority."""
    task = AgentTaskRequest(goal="找岗位", allowed_skills=["job-discovery"])
    with pytest.raises(ValidationError, match="not allowed"):
        ExecutionPlan(
            task=task,
            created_by=AgentRole.planner,
            complexity=ComplexityLevel.L2,
            success_criteria=["返回含来源的岗位"],
            steps=[
                PlanStep(
                    step_id="discover",
                    objective="提取岗位",
                    allowed_skills=["resume-tailoring"],
                )
            ],
        )


def test_execution_plan_keeps_a_simple_request_as_a_real_one_step_plan() -> None:
    """Adaptive L1 work is planned rather than bypassing the Planner."""
    task = AgentTaskRequest(goal="列出岗位", allowed_skills=["job-discovery"])
    plan = ExecutionPlan(
        task=task,
        created_by=AgentRole.planner,
        complexity=ComplexityLevel.L1,
        success_criteria=["给出有来源的结果"],
        steps=[
            PlanStep(
                step_id="discover",
                objective="从公开来源提取岗位",
                allowed_skills=["job-discovery"],
            )
        ],
    )

    assert plan.steps[0].objective == "从公开来源提取岗位"
    assert plan.complexity is ComplexityLevel.L1


def test_task_private_context_is_excluded_from_persistable_dumps() -> None:
    """Confirmed resume facts must never be copied into run or plan audit JSON."""
    task = AgentTaskRequest(
        goal="根据岗位修改简历",
        allowed_skills=["resume-tailoring"],
        private_context={"confirmed_profile_facts": {"skills": ["Python"]}},
    )

    assert task.private_context == {"confirmed_profile_facts": {"skills": ["Python"]}}
    assert "private_context" not in task.model_dump(mode="json")


@pytest.mark.parametrize("allowed_skills", [[" "], ["Not-valid"], ["job_discovery"]])
def test_task_skill_authority_rejects_blank_or_invalid_names(allowed_skills: list[str]) -> None:
    with pytest.raises(ValidationError):
        AgentTaskRequest(goal="找岗位", allowed_skills=allowed_skills)


def test_plan_step_normalizes_text_and_rejects_invalid_shape() -> None:
    step = PlanStep(
        step_id=" discover ", objective=" 提取 ", allowed_skills=["job-discovery"],
        success_criteria=["有来源"],
    )
    assert (step.step_id, step.objective) == ("discover", "提取")
    for payload in (
        {"step_id": " ", "objective": "x", "allowed_skills": ["job-discovery"]},
        {"step_id": "x", "objective": "x", "allowed_skills": ["job_discovery"]},
        {"step_id": "x", "objective": "x", "allowed_skills": ["job-discovery", "job-discovery"]},
        {"step_id": "x", "objective": "x", "allowed_skills": ["job-discovery"], "success_criteria": [" "]},
    ):
        with pytest.raises(ValidationError):
            PlanStep(**payload)


def test_execution_plan_rejects_non_planner_duplicate_steps_and_blank_criteria() -> None:
    task = AgentTaskRequest(goal="找岗位", allowed_skills=["job-discovery"])
    step = PlanStep(step_id="discover", objective="提取", allowed_skills=["job-discovery"])
    with pytest.raises(ValidationError, match="created by the planner"):
        ExecutionPlan(
            task=task, created_by=AgentRole.executor, complexity=ComplexityLevel.L2,
            success_criteria=["完成"], steps=[step],
        )
    with pytest.raises(ValidationError, match="unique"):
        ExecutionPlan(
            task=task, created_by=AgentRole.planner, complexity=ComplexityLevel.L2,
            success_criteria=["完成"], steps=[step, step],
        )
    with pytest.raises(ValidationError, match="success criteria"):
        ExecutionPlan(
            task=task, created_by=AgentRole.planner, complexity=ComplexityLevel.L2,
            success_criteria=[" "], steps=[step],
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"tool_name": "tool", "status": "succeeded"},
        {"tool_name": "tool", "status": "failed"},
        {"tool_name": "tool", "status": "unknown", "output": {}},
    ],
)
def test_tool_observation_rejects_incomplete_or_unknown_outcomes(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ToolObservation(**payload)


def test_agent_decision_and_result_contracts_reject_missing_handoff_data() -> None:
    invalid_models = [
        (PlannerDecision, {"action": "call_tool"}),
        (PlannerDecision, {"action": "plan"}),
        (PlannerDecision, {"action": "need_user", "user_question": " "}),
        (PlannerDecision, {"action": "other"}),
        (PlannerResult, {"status": "planned"}),
        (PlannerResult, {"status": "needs_user"}),
        (PlannerResult, {"status": "other"}),
        (ExecutorDecision, {"action": "call_tool"}),
        (ExecutorDecision, {"action": "complete"}),
        (ExecutorDecision, {"action": "need_user"}),
        (ExecutorDecision, {"action": "other"}),
        (ExecutorResult, {"status": "succeeded"}),
        (ExecutorResult, {"status": "needs_user"}),
        (ExecutorResult, {"status": "other"}),
        (VerifierDecision, {"action": "call_tool"}),
        (VerifierDecision, {"action": "decide"}),
        (VerifierDecision, {"action": "decide", "verification_decision": "REPLAN"}),
        (VerifierDecision, {"action": "other"}),
        (VerifierResult, {"decision": "REPLAN"}),
    ]
    for model, payload in invalid_models:
        with pytest.raises(ValidationError):
            model(**payload)
