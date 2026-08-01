"""Executor Agent behavior: observe tool failure and autonomously choose recovery."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from backend.app.domain.agent_runtime import AgentRole, ComplexityLevel
from backend.app.services.agent_runtime.executor_agent import ExecutorAgent
from backend.app.services.agent_runtime.schemas import (
    AgentTaskRequest,
    ExecutionPlan,
    PlanStep,
)
from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.agent_runtime.tool_registry import ToolDefinition, ToolRegistry


class FetchInput(BaseModel):
    url: str


class FetchOutput(BaseModel):
    source_url: str
    title: str


class ScriptedGateway:
    """A deterministic model boundary double; executor and registry remain real."""

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
        assert role is AgentRole.executor
        assert instruction
        self.states.append(state)
        return response_model.model_validate(self.responses.pop(0))


def test_executor_observes_failure_and_uses_a_second_allowed_tool() -> None:
    """The recovery choice comes from Executor's next turn, not Harness routing."""
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="primary-fetch",
            skill_name="job-discovery",
            input_model=FetchInput,
            output_model=FetchOutput,
            allowed_roles=frozenset({AgentRole.executor}),
            handler=lambda _context, _payload: (_ for _ in ()).throw(RuntimeError("down")),
        )
    )
    registry.register(
        ToolDefinition(
            name="fallback-fetch",
            skill_name="job-discovery",
            input_model=FetchInput,
            output_model=FetchOutput,
            allowed_roles=frozenset({AgentRole.executor}),
            handler=lambda _context, payload: {
                "source_url": payload.url,
                "title": "AI 应用开发工程师",
            },
        )
    )
    gateway = ScriptedGateway(
        [
            {
                "action": "call_tool",
                "tool_name": "primary-fetch",
                "tool_input": {"url": "https://jobs.example/1"},
            },
            {
                "action": "call_tool",
                "tool_name": "fallback-fetch",
                "tool_input": {"url": "https://jobs.example/1"},
            },
            {
                "action": "complete",
                "summary": "已提取公开岗位 JD",
                "artifact_refs": [{"uri": "artifact://job/1"}],
            },
        ]
    )
    task = AgentTaskRequest(goal="找岗位", allowed_skills=["job-discovery"])
    plan = ExecutionPlan(
        task=task,
        created_by=AgentRole.planner,
        complexity=ComplexityLevel.L2,
        success_criteria=["获取岗位 JD"],
        steps=[
            PlanStep(
                step_id="discover",
                objective="提取公开 JD",
                allowed_skills=["job-discovery"],
            )
        ],
    )

    result = ExecutorAgent(gateway=gateway, tools=registry).run(
        task=task,
        plan=plan,
        step=plan.steps[0],
        context=ToolContext(user_id="user-a", run_id="run-a"),
    )

    assert result.status == "succeeded"
    assert [item.error_code for item in result.observations] == [
        "tool_execution_failed",
        None,
    ]
    assert result.artifact_refs == [{"uri": "artifact://job/1"}]
    assert gateway.states[1]["observations"][0]["error_code"] == "tool_execution_failed"
    assert [tool["name"] for tool in gateway.states[0]["available_tools"]] == [
        "fallback-fetch",
        "primary-fetch",
    ]
    assert all(tool["skill_name"] == "job-discovery" for tool in gateway.states[0]["available_tools"])
