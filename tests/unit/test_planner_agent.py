"""Planner Agent behavior: sense context, decide, tool-call, then form a plan."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from backend.app.domain.agent_runtime import AgentRole, ComplexityLevel
from backend.app.services.agent_runtime.planner_agent import PlannerAgent
from backend.app.services.agent_runtime.schemas import AgentTaskRequest
from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.agent_runtime.tool_registry import ToolDefinition, ToolRegistry


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
        }
    ]
