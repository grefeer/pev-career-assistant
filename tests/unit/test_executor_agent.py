"""Executor Agent behavior: observe tool failure and autonomously choose recovery."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from backend.app.domain.agent_runtime import AgentRole, ComplexityLevel
from backend.app.services.agent_runtime.executor_agent import (
    ExecutorAgent,
    _observation_for_decision,
)
from backend.app.services.agent_runtime.schemas import (
    AgentTaskRequest,
    ExecutionPlan,
    PlanStep,
    ToolObservation,
)
from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.agent_runtime.tool_registry import ToolDefinition, ToolRegistry
from backend.app.services.agent_runtime.tool_budget import ToolCallBudget
from backend.app.services.agent_runtime.turn_budget import AgentTurnBudget


class FetchInput(BaseModel):
    url: str


class FetchOutput(BaseModel):
    source_url: str
    title: str


class EvidenceOutput(BaseModel):
    artifact_id: str
    source_url: str
    title: str
    visible_text: str
    content_hash: str


class BatchEvidenceOutput(BaseModel):
    pages: list[EvidenceOutput]
    failures: list[dict[str, str]] = []


class DetailsOutput(BaseModel):
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


def test_executor_makes_a_fresh_public_page_observation_available_to_its_next_tool_call() -> None:
    """The extract tool must receive real fetched evidence, not a model-repeated page body."""
    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="fetch-page", skill_name="job-discovery", input_model=FetchInput,
        output_model=EvidenceOutput, allowed_roles=frozenset({AgentRole.executor}),
        handler=lambda _context, payload: {
            "artifact_id": "observed:a", "source_url": payload.url, "title": "AI Agent 开发工程师",
            "visible_text": "岗位职责：负责 Agent 开发。", "content_hash": "a" * 64,
        },
    ))
    registry.register(ToolDefinition(
        name="extract-page", skill_name="job-discovery", input_model=FetchInput,
        output_model=DetailsOutput, allowed_roles=frozenset({AgentRole.executor}),
        handler=lambda context, _payload: {
            "title": context.metadata["observed_public_evidence"][0]["title"]
        },
    ))
    gateway = ScriptedGateway([
        {"action": "call_tool", "tool_name": "fetch-page", "tool_input": {"url": "https://jobs.example/1"}},
        {"action": "call_tool", "tool_name": "extract-page", "tool_input": {"url": "https://jobs.example/1"}},
        {"action": "complete", "summary": "已提取完整 JD"},
    ])
    task = AgentTaskRequest(goal="提取 JD", allowed_skills=["job-discovery"])
    plan = ExecutionPlan(
        task=task, created_by=AgentRole.planner, complexity=ComplexityLevel.L2,
        success_criteria=["完整 JD"],
        steps=[PlanStep(step_id="discover", objective="抓取并提取", allowed_skills=["job-discovery"])],
    )

    result = ExecutorAgent(gateway=gateway, tools=registry).run(
        task=task, plan=plan, step=plan.steps[0],
        context=ToolContext(user_id="user-a", run_id="run-a"),
    )

    assert result.status == "succeeded"
    assert result.observations[1].output == {"title": "AI Agent 开发工程师"}


def test_executor_exposes_every_page_from_a_batch_observation_to_the_next_tool() -> None:
    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="fetch-pages", skill_name="job-discovery", input_model=FetchInput,
        output_model=BatchEvidenceOutput, allowed_roles=frozenset({AgentRole.executor}),
        handler=lambda _context, _payload: {"pages": [
            {
                "artifact_id": "observed:a", "source_url": "https://jobs.example/a",
                "title": "岗位 A", "visible_text": "x" * 1_201, "content_hash": "a" * 64,
            },
            {
                "artifact_id": "observed:b", "source_url": "https://jobs.example/b",
                "title": "岗位 B", "visible_text": "JD B", "content_hash": "b" * 64,
            },
            {
                "artifact_id": "observed:a", "source_url": "https://jobs.example/a",
                "title": "岗位 A", "visible_text": "JD A", "content_hash": "a" * 64,
            },
        ]},
    ))
    registry.register(ToolDefinition(
        name="inspect-pages", skill_name="job-discovery", input_model=FetchInput,
        output_model=DetailsOutput, allowed_roles=frozenset({AgentRole.executor}),
        handler=lambda context, _payload: {
            "title": ",".join(item["title"] for item in context.metadata["observed_public_evidence"])
        },
    ))
    gateway = ScriptedGateway([
        {"action": "call_tool", "tool_name": "fetch-pages", "tool_input": {"url": "unused"}},
        {"action": "call_tool", "tool_name": "inspect-pages", "tool_input": {"url": "unused"}},
        {"action": "complete", "summary": "已检查批量 JD"},
    ])
    task = AgentTaskRequest(goal="提取 JD", allowed_skills=["job-discovery"])
    plan = ExecutionPlan(
        task=task, created_by=AgentRole.planner, complexity=ComplexityLevel.L2,
        success_criteria=["完整 JD"],
        steps=[PlanStep(step_id="discover", objective="批量抓取", allowed_skills=["job-discovery"])],
    )

    result = ExecutorAgent(gateway=gateway, tools=registry).run(
        task=task, plan=plan, step=plan.steps[0],
        context=ToolContext(user_id="user-a", run_id="run-a"),
    )

    assert result.observations[1].output == {"title": "岗位 A,岗位 B"}
    assert len(gateway.states[1]["observations"][0]["output"]["pages"][0]["visible_text"]) == 1_200


def test_executor_projects_batch_details_to_identifiers_and_titles_only() -> None:
    projected = _observation_for_decision(ToolObservation(
        tool_name="extract-observed-job-details-batch", status="succeeded",
        output={"details": [{
            "source_artifact_id": "observed:a", "source_url": "https://jobs.example/a",
            "content_hash": "a" * 64,
            "candidates": [{"title": "岗位 A", "responsibilities": "x" * 5_000}],
        }]},
    ))

    assert projected["output"]["details"] == [{
        "source_artifact_id": "observed:a", "source_url": "https://jobs.example/a",
        "content_hash": "a" * 64, "candidate_titles": ["岗位 A"],
    }]


def test_executor_returns_need_user_and_honors_hard_budgets() -> None:
    task = AgentTaskRequest(goal="提取 JD", allowed_skills=["job-discovery"])
    plan = ExecutionPlan(
        task=task, created_by=AgentRole.planner, complexity=ComplexityLevel.L2,
        success_criteria=["完整 JD"],
        steps=[PlanStep(step_id="discover", objective="抓取", allowed_skills=["job-discovery"])],
    )
    context = ToolContext(user_id="user-a", run_id="run-a")
    need_user = ExecutorAgent(
        gateway=ScriptedGateway([{"action": "need_user", "user_question": "请给 URL"}]),
        tools=ToolRegistry(),
    ).run(task=task, plan=plan, step=plan.steps[0], context=context)
    assert need_user.status == "needs_user"
    call_tool = {"action": "call_tool", "tool_name": "missing", "tool_input": {}}
    tool_limited = ExecutorAgent(gateway=ScriptedGateway([call_tool]), tools=ToolRegistry()).run(
        task=task, plan=plan, step=plan.steps[0], context=context, tool_budget=ToolCallBudget(1, used=1),
    )
    turn_limited = ExecutorAgent(gateway=ScriptedGateway([]), tools=ToolRegistry()).run(
        task=task, plan=plan, step=plan.steps[0], context=context, turn_budget=AgentTurnBudget(1, used=1),
    )
    exhausted = ExecutorAgent(gateway=ScriptedGateway([call_tool]), tools=ToolRegistry()).run(
        task=task.model_copy(update={"budget": task.budget.model_copy(update={"max_agent_turns": 1})}),
        plan=plan, step=plan.steps[0], context=context,
    )
    assert tool_limited.error_code == "tool_budget_exhausted"
    assert turn_limited.error_code == "agent_turn_budget_exhausted"
    assert ExecutorAgent(gateway=ScriptedGateway([]), tools=ToolRegistry()).run(
        task=task, plan=plan, step=plan.steps[0], context=context, deadline=0,
    ).error_code == "wall_clock_budget_exhausted"
    assert exhausted.status == "failed"
