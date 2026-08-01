"""Autonomous Verifier role for the adaptive PEV runtime."""

from __future__ import annotations

from backend.app.domain.agent_runtime import AgentRole, VerificationDecision
from backend.app.services.agent_runtime.model_gateway import AgentModelGateway
from backend.app.services.agent_runtime.schemas import (
    AgentTaskRequest,
    ExecutionPlan,
    ExecutorResult,
    PlanStep,
    ToolObservation,
    VerifierDecision,
    VerifierResult,
)
from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.agent_runtime.tool_registry import ToolRegistry
from backend.app.services.agent_runtime.tracing import DecisionTrace, decision_summary

_VERIFIER_INSTRUCTION = (
    "You are the Verifier Agent. Independently inspect the planned success "
    "criteria, execution observations and artifact references. Use permitted "
    "verification tools when needed, then return PASS, RETRY_EXECUTOR, REPLAN, "
    "NEED_USER or FAIL. Do not treat an Executor claim as evidence."
)


class VerifierAgent:
    """Bounded perceive–decide–act–observe verification loop."""

    def __init__(self, *, gateway: AgentModelGateway, tools: ToolRegistry) -> None:
        self._gateway = gateway
        self._tools = tools

    def run(
        self,
        *,
        task: AgentTaskRequest,
        plan: ExecutionPlan,
        step: PlanStep,
        execution: ExecutorResult,
        context: ToolContext,
        trace: DecisionTrace | None = None,
    ) -> VerifierResult:
        """Verify a completed step through independent Agent-selected actions."""
        observations: list[ToolObservation] = []
        for _turn in range(task.budget.max_agent_turns):
            decision = self._gateway.decide(
                role=AgentRole.verifier,
                instruction=_VERIFIER_INSTRUCTION,
                state={
                    "goal": task.goal,
                    "plan": plan.model_dump(mode="json"),
                    "step": step.model_dump(mode="json"),
                    "execution": execution.model_dump(mode="json"),
                    "observations": [
                        observation.model_dump(mode="json")
                        for observation in observations
                    ],
                },
                response_model=VerifierDecision,
            )
            if trace is not None:
                trace(
                    AgentRole.verifier,
                    decision_summary(
                        action=decision.action, tool_name=decision.tool_name
                    ),
                )
            if decision.action == "call_tool":
                observations.append(
                    self._tools.invoke(
                        role=AgentRole.verifier,
                        name=decision.tool_name or "",
                        context=context,
                        payload=decision.tool_input,
                        allowed_skills=frozenset(step.allowed_skills),
                    )
                )
                continue
            return VerifierResult(
                decision=decision.verification_decision or VerificationDecision.FAIL,
                feedback=decision.feedback,
                observations=observations,
            )
        return VerifierResult(
            decision=VerificationDecision.FAIL,
            feedback="Verifier turn budget exhausted before a safe decision.",
            observations=observations,
        )
