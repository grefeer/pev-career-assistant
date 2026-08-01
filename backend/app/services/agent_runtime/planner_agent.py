"""Autonomous Planner role for the adaptive PEV runtime."""

from __future__ import annotations

from backend.app.domain.agent_runtime import AgentRole
from backend.app.services.agent_runtime.model_gateway import AgentModelGateway
from backend.app.services.agent_runtime.schemas import (
    AgentTaskRequest,
    ExecutionPlan,
    PlannerDecision,
    PlannerResult,
    ToolObservation,
)
from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.agent_runtime.tool_registry import ToolRegistry
from backend.app.services.agent_runtime.tracing import DecisionTrace, decision_summary

_PLANNER_INSTRUCTION = (
    "You are the Planner Agent. Observe only the supplied user-scoped context "
    "and prior tool observations. You may call permitted low-risk context tools "
    "when information is insufficient. Then produce an outcome-based plan with "
    "success criteria and Skill authority, or ask the user a concrete question."
)


class PlannerAgent:
    """Goal-oriented Planning loop; it is not a one-shot prompt template."""

    def __init__(self, *, gateway: AgentModelGateway, tools: ToolRegistry) -> None:
        self._gateway = gateway
        self._tools = tools

    def run(
        self,
        *,
        task: AgentTaskRequest,
        context: ToolContext,
        trace: DecisionTrace | None = None,
    ) -> PlannerResult:
        """Sense context and form a bounded execution plan for every request."""
        observations: list[ToolObservation] = []
        for _turn in range(task.budget.max_agent_turns):
            decision = self._gateway.decide(
                role=AgentRole.planner,
                instruction=_PLANNER_INSTRUCTION,
                state={
                    "goal": task.goal,
                    "allowed_skills": task.allowed_skills,
                    "available_tools": self._tools.tool_catalog(
                        role=AgentRole.planner,
                        allowed_skills=frozenset(task.allowed_skills),
                    ),
                    "context": task.context,
                    "observations": [
                        observation.model_dump(mode="json")
                        for observation in observations
                    ],
                },
                response_model=PlannerDecision,
            )
            if trace is not None:
                trace(
                    AgentRole.planner,
                    decision_summary(
                        action=decision.action, tool_name=decision.tool_name
                    ),
                )
            if decision.action == "call_tool":
                observations.append(
                    self._tools.invoke(
                        role=AgentRole.planner,
                        name=decision.tool_name or "",
                        context=context,
                        payload=decision.tool_input,
                    )
                )
                continue
            if decision.action == "plan":
                plan = ExecutionPlan(
                    task=task,
                    created_by=AgentRole.planner,
                    complexity=decision.complexity,
                    success_criteria=decision.success_criteria,
                    steps=decision.steps,
                )
                return PlannerResult(
                    status="planned", plan=plan, observations=observations
                )
            return PlannerResult(
                status="needs_user",
                observations=observations,
                user_question=decision.user_question,
            )
        return PlannerResult(
            status="failed",
            observations=observations,
            user_question="Planner turn budget exhausted before a safe plan was formed.",
        )
