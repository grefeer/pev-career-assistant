"""Autonomous Executor role for the adaptive PEV runtime."""

from __future__ import annotations

from backend.app.domain.agent_runtime import AgentRole
from backend.app.services.agent_runtime.model_gateway import AgentModelGateway
from backend.app.services.agent_runtime.schemas import (
    AgentTaskRequest,
    ExecutionPlan,
    ExecutorDecision,
    ExecutorResult,
    PlanStep,
    ToolObservation,
)
from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.agent_runtime.tool_registry import ToolRegistry
from backend.app.services.agent_runtime.tracing import DecisionTrace, decision_summary

_EXECUTOR_INSTRUCTION = (
    "You are the Executor Agent. Work toward the current planned outcome using "
    "only its permitted Skills. Observe every tool result, including failures, "
    "and independently select the next allowed action. Do not claim an artifact "
    "that is absent from observations; ask the user if the goal cannot proceed."
)


class ExecutorAgent:
    """Bounded perceive–decide–act–observe loop for a single plan step."""

    def __init__(self, *, gateway: AgentModelGateway, tools: ToolRegistry) -> None:
        self._gateway = gateway
        self._tools = tools

    def run(
        self,
        *,
        task: AgentTaskRequest,
        plan: ExecutionPlan,
        step: PlanStep,
        context: ToolContext,
        trace: DecisionTrace | None = None,
    ) -> ExecutorResult:
        """Execute a step without precomputing its tool sequence in the harness."""
        observations: list[ToolObservation] = []
        for _turn in range(task.budget.max_agent_turns):
            decision = self._gateway.decide(
                role=AgentRole.executor,
                instruction=_EXECUTOR_INSTRUCTION,
                state={
                    "goal": task.goal,
                    "context": task.context,
                    "private_context": task.private_context,
                    "plan": plan.model_dump(mode="json"),
                    "step": step.model_dump(mode="json"),
                    "available_tools": self._tools.tool_catalog(
                        role=AgentRole.executor,
                        allowed_skills=frozenset(step.allowed_skills),
                    ),
                    "observations": [
                        observation.model_dump(mode="json")
                        for observation in observations
                    ],
                },
                response_model=ExecutorDecision,
            )
            if trace is not None:
                trace(
                    AgentRole.executor,
                    decision_summary(
                        action=decision.action, tool_name=decision.tool_name
                    ),
                )
            if decision.action == "call_tool":
                observations.append(
                    self._tools.invoke(
                        role=AgentRole.executor,
                        name=decision.tool_name or "",
                        context=context,
                        payload=decision.tool_input,
                        allowed_skills=frozenset(step.allowed_skills),
                    )
                )
                continue
            if decision.action == "complete":
                return ExecutorResult(
                    status="succeeded",
                    summary=decision.summary,
                    artifact_refs=decision.artifact_refs,
                    observations=observations,
                )
            return ExecutorResult(
                status="needs_user",
                observations=observations,
                user_question=decision.user_question,
            )
        return ExecutorResult(
            status="failed",
            observations=observations,
            summary="Executor turn budget exhausted before completing the step.",
        )
