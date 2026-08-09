"""Verifier Agent behavior: independently inspect evidence and route recovery."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from backend.app.domain.agent_runtime import (
    AgentRole,
    ComplexityLevel,
    VerificationDecision,
)
from backend.app.services.agent_runtime.schemas import (
    AgentTaskRequest,
    ExecutionPlan,
    ExecutorResult,
    PlanStep,
    ToolObservation,
)
from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.agent_runtime.tool_registry import ToolDefinition, ToolRegistry
from backend.app.services.agent_runtime.verifier_agent import VerifierAgent
from backend.app.services.agent_runtime.tool_budget import ToolCallBudget
from backend.app.services.agent_runtime.turn_budget import AgentTurnBudget


class EvidenceInput(BaseModel):
    artifact_uri: str


class EvidenceOutput(BaseModel):
    complete: bool
    missing_fields: list[str]


class ScriptedGateway:
    """A deterministic model boundary double; Verifier loop and tools are real."""

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
        assert role is AgentRole.verifier
        assert instruction
        self.states.append(state)
        return response_model.model_validate(self.responses.pop(0))


def test_verifier_calls_evidence_tool_then_routes_executor_retry() -> None:
    """Verifier recovery comes from independently observed evidence, not a rule."""
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="verify-job-evidence",
            skill_name="job-discovery",
            input_model=EvidenceInput,
            output_model=EvidenceOutput,
            allowed_roles=frozenset({AgentRole.verifier}),
            handler=lambda _context, _payload: {
                "complete": False,
                "missing_fields": ["岗位职责", "任职要求"],
            },
        )
    )
    gateway = ScriptedGateway(
        [
            {
                "action": "call_tool",
                "tool_name": "verify-job-evidence",
                "tool_input": {"artifact_uri": "artifact://job/1"},
            },
            {
                "action": "decide",
                "verification_decision": "RETRY_EXECUTOR",
                "feedback": "补充岗位职责与任职要求的公开证据后再返回。",
            },
        ]
    )
    task = AgentTaskRequest(goal="找岗位", allowed_skills=["job-discovery"])
    plan = ExecutionPlan(
        task=task,
        created_by=AgentRole.planner,
        complexity=ComplexityLevel.L3,
        success_criteria=["完整 JD"],
        steps=[
            PlanStep(
                step_id="discover",
                objective="提取岗位",
                allowed_skills=["job-discovery"],
                requires_verification=True,
            )
        ],
    )
    execution = ExecutorResult(
        status="succeeded",
        summary="找到岗位标题",
        artifact_refs=[{"uri": "artifact://job/1"}],
    )

    result = VerifierAgent(gateway=gateway, tools=registry).run(
        task=task,
        plan=plan,
        step=plan.steps[0],
        execution=execution,
        context=ToolContext(user_id="user-a", run_id="run-a"),
    )

    assert result.decision is VerificationDecision.RETRY_EXECUTOR
    assert result.feedback == "补充岗位职责与任职要求的公开证据后再返回。"
    assert result.observations[0].output == {
        "complete": False,
        "missing_fields": ["岗位职责", "任职要求"],
    }
    assert gateway.states[1]["my_tool_calls"][0]["tool_name"] == "verify-job-evidence"


def test_verifier_sees_executor_tool_calls_with_bounded_excerpts() -> None:
    """R002 regression: the Verifier reads execution_tool_calls, not its own list."""
    gateway = ScriptedGateway(
        [
            {
                "action": "decide",
                "verification_decision": "PASS",
                "feedback": "匹配调用可见。",
            }
        ]
    )
    task = AgentTaskRequest(goal="匹配岗位", allowed_skills=["job-matching"])
    plan = ExecutionPlan(
        task=task,
        created_by=AgentRole.planner,
        complexity=ComplexityLevel.L3,
        success_criteria=["匹配结果"],
        steps=[
            PlanStep(
                step_id="match", objective="匹配", allowed_skills=["job-matching"]
            )
        ],
    )
    execution = ExecutorResult(
        status="succeeded",
        summary="已完成匹配",
        observations=[
            ToolObservation(
                tool_name="match-observed-jobs",
                status="succeeded",
                output={
                    "matches": [{"job_id": "j1", "score": 0.92}],
                    "note": "长文本省略" * 500,
                },
            ),
            ToolObservation(
                tool_name="extract-observed-job-details-batch",
                status="failed",
                error_code="no_candidates",
            ),
        ],
    )
    context = ToolContext(user_id="user-a", run_id="run-a")

    result = VerifierAgent(gateway=gateway, tools=ToolRegistry()).run(
        task=task,
        plan=plan,
        step=plan.steps[0],
        execution=execution,
        context=context,
    )

    assert result.decision is VerificationDecision.PASS
    state = gateway.states[0]
    assert state["my_tool_calls"] == []
    calls = state["execution_tool_calls"]
    assert [call["tool_name"] for call in calls] == [
        "match-observed-jobs",
        "extract-observed-job-details-batch",
    ]
    assert calls[0]["status"] == "succeeded"
    assert calls[0]["output_excerpt"].startswith('{"matches":')
    assert len(calls[0]["output_excerpt"]) == 200
    assert calls[1]["error_code"] == "no_candidates"
    assert "output_excerpt" not in calls[1]


def test_verifier_enforces_budgets_and_reports_exhausted_tool_loop() -> None:
    task = AgentTaskRequest(goal="找岗位", allowed_skills=["job-discovery"])
    plan = ExecutionPlan(
        task=task, created_by=AgentRole.planner, complexity=ComplexityLevel.L3,
        success_criteria=["完整 JD"],
        steps=[PlanStep(step_id="discover", objective="提取", allowed_skills=["job-discovery"])],
    )
    execution = ExecutorResult(status="succeeded", summary="完成")
    context = ToolContext(user_id="user-a", run_id="run-a")
    call_tool = {"action": "call_tool", "tool_name": "missing", "tool_input": {}}
    tool_limited = VerifierAgent(gateway=ScriptedGateway([call_tool]), tools=ToolRegistry()).run(
        task=task, plan=plan, step=plan.steps[0], execution=execution, context=context,
        tool_budget=ToolCallBudget(1, used=1),
    )
    turn_limited = VerifierAgent(gateway=ScriptedGateway([]), tools=ToolRegistry()).run(
        task=task, plan=plan, step=plan.steps[0], execution=execution, context=context,
        turn_budget=AgentTurnBudget(1, used=1),
    )
    exhausted = VerifierAgent(gateway=ScriptedGateway([call_tool]), tools=ToolRegistry()).run(
        task=task.model_copy(update={"budget": task.budget.model_copy(update={"max_agent_turns": 1})}),
        plan=plan, step=plan.steps[0], execution=execution, context=context,
    )
    assert tool_limited.error_code == "tool_budget_exhausted"
    assert turn_limited.error_code == "agent_turn_budget_exhausted"
    assert VerifierAgent(gateway=ScriptedGateway([]), tools=ToolRegistry()).run(
        task=task, plan=plan, step=plan.steps[0], execution=execution,
        context=context, deadline=0,
    ).error_code == "wall_clock_budget_exhausted"
    assert exhausted.feedback.startswith("Verifier turn budget exhausted")
