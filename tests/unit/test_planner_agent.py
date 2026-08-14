"""Planner Agent behavior: sense context, decide, tool-call, then form a plan."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from backend.app.domain.agent_runtime import AgentRole, ComplexityLevel
from backend.app.services.agent_runtime.planner_agent import PlannerAgent
from backend.app.services.agent_runtime.schemas import AgentTaskRequest, ExecutionPlan
from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.agent_runtime.tool_registry import ToolDefinition, ToolRegistry
from backend.app.services.agent_runtime.tool_budget import ToolCallBudget
from backend.app.services.agent_runtime.turn_budget import AgentTurnBudget
from backend.app.services.agent_runtime.skill_definition import (
    ArtifactPort,
    SkillDefinition,
    SkillRegistry,
)


class PreferenceInput(BaseModel):
    key: str


class PreferenceOutput(BaseModel):
    target_roles: list[str]


class ScriptedGateway:
    """A deterministic model boundary double; tools and Agent loop stay real."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = responses
        self.states: list[dict[str, Any]] = []

    def decide(
        self,
        *,
        role: AgentRole,
        instruction: str,
        state: dict[str, Any],
        response_model: type[BaseModel],
    ) -> BaseModel:
        assert role is AgentRole.planner
        assert instruction
        self.states.append(state)
        return response_model.model_validate(self.responses.pop(0))


def test_planner_uses_context_tool_observation_before_creating_a_plan() -> None:
    """A Planner is autonomous only if tool evidence changes its next turn."""
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="read-preferences",
            input_model=PreferenceInput,
            output_model=PreferenceOutput,
            allowed_roles=frozenset({AgentRole.planner}),
            handler=lambda _context, _payload: {"target_roles": ["AI 应用开发"]},
        )
    )
    gateway = ScriptedGateway(
        [
            {
                "action": "call_tool",
                "tool_name": "read-preferences",
                "tool_input": {"key": "target_roles"},
            },
            {
                "action": "plan",
                "complexity": "L2",
                "success_criteria": ["返回带来源的 AI 应用开发岗位"],
                "steps": [
                    {
                        "step_id": "discover",
                        "objective": "从公开来源提取 AI 应用开发岗位",
                        "allowed_skills": ["job-discovery"],
                    }
                ],
            },
        ]
    )
    task = AgentTaskRequest(goal="帮我找适合的岗位", allowed_skills=["job-discovery"])

    result = PlannerAgent(gateway=gateway, tools=registry).run(
        task=task,
        context=ToolContext(user_id="user-a", run_id="run-a"),
    )

    assert result.status == "planned"
    assert result.plan is not None
    assert result.plan.complexity is ComplexityLevel.L2
    assert result.plan.steps[0].allowed_skills == ["job-discovery"]
    assert gateway.states[1]["observations"] == [
        {
            "tool_name": "read-preferences",
            "status": "succeeded",
            "output": {"target_roles": ["AI 应用开发"]},
            "error_code": None,
            "error_message": None,
        }
    ]


def test_planner_enforces_shared_turn_and_tool_budgets_and_reports_loop_exhaustion() -> None:
    task = AgentTaskRequest(goal="找岗位", allowed_skills=["job-discovery"])
    call_tool = {"action": "call_tool", "tool_name": "missing", "tool_input": {}}
    agent = PlannerAgent(gateway=ScriptedGateway([call_tool]), tools=ToolRegistry())
    context = ToolContext(user_id="user-a", run_id="run-a")

    assert agent.run(task=task, context=context, turn_budget=AgentTurnBudget(1, used=1)).error_code == "agent_turn_budget_exhausted"
    assert agent.run(task=task, context=context, tool_budget=ToolCallBudget(1, used=1)).error_code == "tool_budget_exhausted"
    assert agent.run(task=task, context=context, deadline=0).error_code == "wall_clock_budget_exhausted"

    exhausted = PlannerAgent(
        gateway=ScriptedGateway([call_tool]), tools=ToolRegistry()
    ).run(
        task=task.model_copy(update={"budget": task.budget.model_copy(update={"max_agent_turns": 1})}),
        context=context,
    )
    assert exhausted.status == "failed"
    assert exhausted.user_question is not None


def test_planner_can_sense_confirmed_profile_field_availability_without_fact_values() -> None:
    gateway = ScriptedGateway([{
        "action": "need_user", "user_question": "请确认目标城市。",
    }])
    task = AgentTaskRequest(
        goal="按简历匹配岗位", allowed_skills=["job-matching"],
        private_context={"confirmed_profile_facts": {"skills": ["Python"], "projects": ["秘密项目"]}},
    )

    PlannerAgent(gateway=gateway, tools=ToolRegistry()).run(
        task=task, context=ToolContext(user_id="user-a", run_id="run-a")
    )

    assert gateway.states[0]["confirmed_profile_fact_fields"] == ["projects", "skills"]
    assert "秘密项目" not in str(gateway.states[0])


def test_planner_trims_unrequested_trailing_resume_step() -> None:
    task = AgentTaskRequest(
        goal="请按匹配度排序这些前端开发工程师岗位",
        allowed_skills=["job-discovery", "job-matching", "resume-tailoring"],
    )
    plan = ExecutionPlan.model_validate(
        {
            "task": task.model_dump(),
            "created_by": "planner",
            "complexity": "L3",
            "success_criteria": ["完成岗位匹配"],
            "steps": [
                {
                    "step_id": "match",
                    "objective": "匹配岗位",
                    "allowed_skills": ["job-matching"],
                },
                {
                    "step_id": "tailor",
                    "objective": "生成简历建议",
                    "allowed_skills": ["resume-tailoring"],
                    "depends_on": ["match"],
                },
            ],
        }
    )

    trimmed = PlannerAgent._trim_unrequested_trailing_steps(task, plan)

    assert [step.step_id for step in trimmed.steps] == ["match"]


def test_planner_downgrades_invalid_execution_plan_to_recoverable_wait() -> None:
    gateway = ScriptedGateway(
        [
            {
                "action": "plan",
                "complexity": "L1",
                "success_criteria": ["完成"],
                "steps": [
                    {
                        "step_id": "duplicate",
                        "objective": "第一步",
                        "allowed_skills": ["job-discovery"],
                    },
                    {
                        "step_id": "duplicate",
                        "objective": "第二步",
                        "allowed_skills": ["job-discovery"],
                    },
                ],
            },
            {
                "action": "plan",
                "complexity": "L1",
                "success_criteria": ["完成"],
                "steps": [
                    {
                        "step_id": "duplicate",
                        "objective": "仍然非法的计划",
                        "allowed_skills": ["job-discovery"],
                    },
                    {
                        "step_id": "duplicate",
                        "objective": "重复步骤",
                        "allowed_skills": ["job-discovery"],
                    },
                ],
            },
            {
                "action": "plan",
                "complexity": "L1",
                "success_criteria": ["完成"],
                "steps": [
                    {
                        "step_id": "duplicate",
                        "objective": "第三次仍然非法",
                        "allowed_skills": ["job-discovery"],
                    },
                    {
                        "step_id": "duplicate",
                        "objective": "重复步骤",
                        "allowed_skills": ["job-discovery"],
                    },
                ],
            },
            {
                "action": "plan",
                "complexity": "L1",
                "success_criteria": ["完成"],
                "steps": [
                    {
                        "step_id": "duplicate",
                        "objective": "第四次仍然非法",
                        "allowed_skills": ["job-discovery"],
                    },
                    {
                        "step_id": "duplicate",
                        "objective": "重复步骤",
                        "allowed_skills": ["job-discovery"],
                    },
                ],
            },
        ]
    )

    result = PlannerAgent(gateway=gateway, tools=ToolRegistry()).run(
        task=AgentTaskRequest(goal="找岗位", allowed_skills=["job-discovery"]),
        context=ToolContext(user_id="user-a", run_id="run-a"),
    )

    assert result.status == "needs_user"
    assert result.error_code == "invalid_execution_plan"
    assert result.user_question
    assert len(gateway.states) == 4
    assert "ExecutionPlan 校验失败" in gateway.states[3]["runtime_feedback"]


def test_planner_normalizes_known_model_artifact_port_alias() -> None:
    gateway = ScriptedGateway(
        [
            {
                "action": "plan",
                "complexity": "L1",
                "success_criteria": ["完成匹配"],
                "steps": [
                    {
                        "step_id": "match",
                        "objective": "匹配岗位",
                        "allowed_skills": ["job-matching"],
                        "outputs": [
                            {"name": "best_match", "artifact_type": "match_result"}
                        ],
                    }
                ],
            }
        ]
    )
    skills = SkillRegistry(
        [
            SkillDefinition(
                name="job-matching",
                output_ports=(ArtifactPort("report", frozenset({"job_matching_report"})),),
            )
        ]
    )

    result = PlannerAgent(
        gateway=gateway, tools=ToolRegistry(), skills=skills
    ).run(
        task=AgentTaskRequest(goal="匹配岗位", allowed_skills=["job-matching"]),
        context=ToolContext(user_id="user-a", run_id="run-a"),
    )

    assert result.status == "planned"
    assert result.plan is not None
    assert result.plan.steps[0].outputs[0].artifact_type == "job_matching_report"
    assert len(gateway.states) == 1
