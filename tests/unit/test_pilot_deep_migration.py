"""Stage 1.2 migration pilot: legacy executor tests on the Deep path."""
from __future__ import annotations

from pydantic import BaseModel

from backend.app.domain.agent_runtime import AgentRole, ComplexityLevel
from backend.app.services.agent_runtime.executor_agent import ExecutorAgent
from backend.app.services.agent_runtime.schemas import (
    AgentTaskRequest,
    ExecutionPlan,
    PlanStep,
)
from backend.app.services.agent_runtime.skill_definition import SkillRegistry
from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.agent_runtime.tool_registry import ToolDefinition, ToolRegistry
from backend.app.services.agent_runtime.tool_budget import ToolCallBudget
from tests.unit.deepagents_testkit import DeepGateway, scripted_executor_model


class FetchInput(BaseModel):
    url: str


class DetailsOutput(BaseModel):
    title: str


def _task(goal: str, allowed_skills: list[str]) -> AgentTaskRequest:
    return AgentTaskRequest(goal=goal, allowed_skills=allowed_skills)


def _plan(task: AgentTaskRequest, objective: str) -> ExecutionPlan:
    return ExecutionPlan(
        task=task,
        created_by=AgentRole.planner,
        complexity=ComplexityLevel.L2,
        success_criteria=["完成"],
        steps=[PlanStep(step_id="discover", objective=objective, allowed_skills=task.allowed_skills)],
    )


def test_pilot_deduplicates_consecutive_identical_tool_calls_without_consuming_budget() -> None:
    invocations = {"count": 0}

    def handler(_context, _payload):
        invocations["count"] += 1
        return {"title": "AI 应用开发工程师"}

    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="extract-page", skill_name="job-discovery", input_model=FetchInput,
        output_model=DetailsOutput, allowed_roles=frozenset({AgentRole.executor}),
        handler=handler,
    ))
    gateway = DeepGateway(scripted_executor_model([
        {"action": "call_tool", "tool_name": "extract-page", "tool_input": {"url": "https://jobs.example/1"}},
        {"action": "call_tool", "tool_name": "extract-page", "tool_input": {"url": "https://jobs.example/1"}},
        {"action": "complete", "summary": "已提取 JD"},
    ]))
    task = _task("提取 JD", ["job-discovery"])
    plan = _plan(task, "提取")

    result = ExecutorAgent(gateway=gateway, tools=registry, skills=SkillRegistry()).run(
        task=task, plan=plan, step=plan.steps[0],
        context=ToolContext(user_id="user-a", run_id="run-a"),
        tool_budget=ToolCallBudget(1),
    )

    assert result.status == "succeeded"
    assert invocations["count"] == 1
    assert [obs.error_code for obs in result.observations] == [None, "duplicate_tool_call"]
    assert result.observations[1].tool_name == "extract-page"


def test_pilot_allows_repeated_tool_call_when_input_differs() -> None:
    titles = iter(["岗位一", "岗位二"])

    def handler(_context, _payload):
        return {"title": next(titles)}

    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="fetch-page", skill_name="job-discovery", input_model=FetchInput,
        output_model=DetailsOutput, allowed_roles=frozenset({AgentRole.executor}),
        handler=handler,
    ))
    gateway = DeepGateway(scripted_executor_model([
        {"action": "call_tool", "tool_name": "fetch-page", "tool_input": {"url": "https://jobs.example/1"}},
        {"action": "call_tool", "tool_name": "fetch-page", "tool_input": {"url": "https://jobs.example/2"}},
        {"action": "complete", "summary": "已抓取两个页面"},
    ]))
    task = _task("抓取 JD", ["job-discovery"])
    plan = _plan(task, "抓取")

    result = ExecutorAgent(gateway=gateway, tools=registry, skills=SkillRegistry()).run(
        task=task, plan=plan, step=plan.steps[0],
        context=ToolContext(user_id="user-a", run_id="run-a"),
        tool_budget=ToolCallBudget(2),
    )

    assert result.status == "succeeded"
    assert [obs.output for obs in result.observations] == [
        {"title": "岗位一"}, {"title": "岗位二"},
    ]
    assert all(obs.error_code is None for obs in result.observations)


def test_pilot_retries_an_identical_call_after_the_prior_one_failed() -> None:
    attempts = {"count": 0}

    def flaky(_context, _payload):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("transient failure")
        return {"title": "AI Agent 开发工程师"}

    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="fetch-page", skill_name="job-discovery", input_model=FetchInput,
        output_model=DetailsOutput, allowed_roles=frozenset({AgentRole.executor}),
        handler=flaky,
    ))
    gateway = DeepGateway(scripted_executor_model([
        {"action": "call_tool", "tool_name": "fetch-page", "tool_input": {"url": "https://jobs.example/1"}},
        {"action": "call_tool", "tool_name": "fetch-page", "tool_input": {"url": "https://jobs.example/1"}},
        {"action": "complete", "summary": "重试后抓取成功"},
    ]))
    task = _task("抓取 JD", ["job-discovery"])
    plan = _plan(task, "抓取")

    result = ExecutorAgent(gateway=gateway, tools=registry, skills=SkillRegistry()).run(
        task=task, plan=plan, step=plan.steps[0],
        context=ToolContext(user_id="user-a", run_id="run-a"),
        tool_budget=ToolCallBudget(2),
    )

    assert result.status == "succeeded"
    assert attempts["count"] == 2
    assert [obs.error_code for obs in result.observations] == ["tool_execution_failed", None]


def test_pilot_need_user_and_hard_budgets() -> None:
    task = _task("提取 JD", ["job-discovery"])
    plan = _plan(task, "抓取")
    context = ToolContext(user_id="user-a", run_id="run-a")
    need_user = ExecutorAgent(
        gateway=DeepGateway(scripted_executor_model([
            {"action": "need_user", "user_question": "请给 URL"},
        ])),
        tools=ToolRegistry(),
        skills=SkillRegistry(),
    ).run(task=task, plan=plan, step=plan.steps[0], context=context)
    assert need_user.status == "needs_user"

    budget_registry = ToolRegistry()
    budget_registry.register(ToolDefinition(
        name="fetch-page", skill_name="job-discovery", input_model=FetchInput,
        output_model=DetailsOutput, allowed_roles=frozenset({AgentRole.executor}),
        handler=lambda _context, _payload: {"title": "x"},
    ))
    tool_limited = ExecutorAgent(
        gateway=DeepGateway(scripted_executor_model([
            {"action": "call_tool", "tool_name": "fetch-page", "tool_input": {"url": "https://jobs.example/1"}},
        ])),
        tools=budget_registry,
        skills=SkillRegistry(),
    ).run(task=task, plan=plan, step=plan.steps[0], context=context, tool_budget=ToolCallBudget(1, used=1))
    assert tool_limited.error_code == "tool_budget_exhausted"
