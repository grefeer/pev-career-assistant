"""Input/output contracts shared by the autonomous PEV roles."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.app.domain.agent_runtime import AgentRole, ComplexityLevel
from backend.app.services.agent_runtime.schemas import (
    AgentBudget,
    AgentTaskRequest,
    ExecutionPlan,
    PlanStep,
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
