"""Autonomous Verifier role for the adaptive PEV runtime."""

from __future__ import annotations

import time

from backend.app.domain.agent_runtime import AgentRole, VerificationDecision
from backend.app.services.agent_runtime.model_gateway import AgentModelGateway
from backend.app.services.agent_runtime.observation_projection import (
    record_observation,
    summarize_observations,
    summarize_tool_call_history,
)
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
from backend.app.services.agent_runtime.tool_budget import ToolCallBudget
from backend.app.services.agent_runtime.tool_registry import ToolRegistry
from backend.app.services.agent_runtime.context_manifest import (
    build_context_manifest,
    compute_evidence_chars,
)
from backend.app.services.agent_runtime.tracing import DecisionTrace, decision_summary
from backend.app.services.agent_runtime.turn_budget import AgentTurnBudget

_VERIFIER_INSTRUCTION = (
    "## 角色\n"
    "You are the Verifier Agent. Independently inspect the planned success "
    "criteria, execution observations and artifact references. Use permitted "
    "verification tools when needed, then return PASS, RETRY_EXECUTOR, REPLAN, "
    "NEED_USER or FAIL. Do not treat an Executor claim as evidence. "
    "\n## 行为规则\n"
    "For a current outcome that promises a ranked recommendation, best treatment, "
    "or best-fit role, do not return PASS unless execution_tool_calls include "
    "match-observed-jobs. For a promised grounded resume change or preparation plan, "
    "require build-resume-tailoring-brief or build-preparation-plan respectively. "
    "Return RETRY_EXECUTOR with the missing tool-backed deliverable as feedback; "
    "never accept a prose claim in place of that observation. "
    "For a job-discovery step, the evidence is the captured page text artifact "
    "itself; structured extraction (extract-observed-job-details/-batch) is an "
    "optional enhancement, not a requirement. A list or card page that yields "
    "no structured candidates is still valid evidence: do not RETRY_EXECUTOR "
    "a discovery step solely because structured extraction produced nothing, "
    "as long as raw page text was captured. Return PASS and let the "
    "deliverable steps operate on the raw observed text. "
    "A sheet-backed step (query-career-sheet-records) has the same contract: "
    "the evidence is the persisted records artifact carrying content_hash and "
    "source_url, not page text. Do not RETRY_EXECUTOR a sheet-backed step "
    "solely because no page text was captured, as long as the sheet records "
    "were returned with their evidence binding."
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
        tool_budget: ToolCallBudget | None = None,
        turn_budget: AgentTurnBudget | None = None,
        deadline: float | None = None,
    ) -> VerifierResult:
        """Verify a completed step through independent Agent-selected actions."""
        observations: list[ToolObservation] = []
        observations_for_decision: list[dict[str, object]] = []
        # Loop-invariant projections of the (immutable) plan/step/execution:
        # serialize once instead of re-dumping and re-building the tool catalog
        # every turn. The catalog is also memoized inside ToolRegistry.
        plan_json = plan.model_dump(mode="json")
        step_json = step.model_dump(mode="json")
        execution_json = execution.model_dump(mode="json")
        allowed_skills = frozenset(step.allowed_skills)
        available_tools = self._tools.tool_catalog(
            role=AgentRole.verifier, allowed_skills=allowed_skills
        )
        for _turn in range(task.budget.max_agent_turns):
            if deadline is not None and time.monotonic() >= deadline:
                return VerifierResult(
                    decision=VerificationDecision.FAIL,
                    feedback="Wall-clock budget exhausted before verification.",
                    observations=observations,
                    error_code="wall_clock_budget_exhausted",
                )
            if turn_budget is not None and not turn_budget.try_consume():
                return VerifierResult(
                    decision=VerificationDecision.FAIL,
                    feedback="Agent-turn budget exhausted before verification.",
                    observations=observations,
                    error_code="agent_turn_budget_exhausted",
                )
            # Bound the observation list the model sees: keep the most-recent
            # projections full and collapse older ones to identifier-only summary
            # lines when the accumulated list exceeds the character budget.
            summarized_observations = summarize_observations(observations_for_decision)
            decision = self._gateway.decide(
                role=AgentRole.verifier,
                instruction=_VERIFIER_INSTRUCTION,
                state={
                    "goal": task.goal,
                    "plan": plan_json,
                    "step": step_json,
                    "available_tools": available_tools,
                    "execution": execution_json,
                    "execution_tool_calls": summarize_tool_call_history(
                        execution.observations
                    ),
                    "remaining_tool_calls": (
                        tool_budget.remaining if tool_budget is not None
                        else task.budget.max_tool_calls - len(observations)
                    ),
                    "remaining_agent_turns": (
                        turn_budget.remaining
                        if turn_budget is not None
                        else task.budget.max_agent_turns - _turn - 1
                    ),
                    # The Verifier's own tool calls; the Executor's history is
                    # exposed separately as execution_tool_calls so the model
                    # cannot mistake its own (initially empty) list for the
                    # executed step's evidence (R002 "observations 数组为空").
                    "my_tool_calls": summarized_observations,
                },
                response_model=VerifierDecision,
            )
            if trace is not None:
                usage = self._gateway.last_usage
                if isinstance(usage, dict):
                    usage["context_manifest"] = build_context_manifest(
                        instruction=_VERIFIER_INSTRUCTION,
                        available_tools=available_tools,
                        observations_for_decision=summarized_observations,
                        evidence_chars=compute_evidence_chars(
                            context.metadata.get("observed_public_evidence")
                        ),
                        model_name=usage.get("model_name"),
                    )
                trace(
                    AgentRole.verifier,
                    decision_summary(
                        action=decision.action,
                        tool_name=decision.tool_name,
                        verification_decision=(
                            decision.verification_decision.value
                            if decision.verification_decision is not None
                            else None
                        ),
                    ),
                    usage,
                )
            if decision.action == "call_tool":
                if tool_budget is not None and not tool_budget.try_consume():
                    return VerifierResult(
                        decision=VerificationDecision.FAIL,
                        feedback="Tool-call budget exhausted before verification.",
                        observations=observations,
                        error_code="tool_budget_exhausted",
                    )
                record_observation(
                    observations,
                    observations_for_decision,
                    self._tools.invoke(
                        role=AgentRole.verifier,
                        name=decision.tool_name or "",
                        context=context,
                        payload=decision.tool_input,
                        allowed_skills=allowed_skills,
                    ),
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
