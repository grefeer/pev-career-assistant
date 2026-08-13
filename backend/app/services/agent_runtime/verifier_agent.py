"""Autonomous Verifier role for the adaptive PEV runtime."""

from __future__ import annotations

import time

from backend.app.domain.agent_runtime import AgentRole, VerificationDecision
from backend.app.services.agent_runtime.model_gateway import AgentModelGateway
from backend.app.services.agent_runtime.model_budget import (
    ModelCallBudget,
    estimate_input_tokens,
)
from backend.app.services.agent_runtime.prompt_rules import (
    COMMON_RUNTIME_RULES,
    VERIFIER_RUNTIME_RULES,
)
from backend.app.services.agent_runtime.observation_projection import (
    observation_for_decision,
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
from backend.app.services.agent_runtime.skill_definition import SkillRegistry
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
    "You are the Verifier role in a generic Planner-Executor-Verifier runtime.\n"
    "## 行为规则\n"
    "Independently compare the step contract with tool observations and persisted "
    "artifact references. Use only permitted verification tools and never treat "
    "prose as evidence.\n"
    "## 输出契约\n"
    "Return exactly one machine-actionable decision: PASS only when the activated "
    "Skill contract is satisfied; otherwise choose RETRY_EXECUTOR, REPLAN, "
    "NEED_USER, or FAIL with concise feedback."
    "\n\n"
    + COMMON_RUNTIME_RULES
    + VERIFIER_RUNTIME_RULES
)


class VerifierAgent:
    """Bounded perceive–decide–act–observe verification loop."""

    def __init__(
        self,
        *,
        gateway: AgentModelGateway,
        tools: ToolRegistry,
        skills: SkillRegistry | None = None,
    ) -> None:
        self._gateway = gateway
        self._tools = tools
        self._skills = skills

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
        model_budget: ModelCallBudget | None = None,
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
        # The Executor's raw observations never reach the Verifier at full
        # width (round-3 R013/R018/R033 verifier drift): project them exactly
        # like the Executor's own decision state - bounded visible_text
        # excerpts (~1,200 chars), at most 10 pages/details per observation,
        # and the accumulated list capped at the shared 48,000-char budget
        # with older entries collapsing to identifier-only summary lines.
        execution_json["observations"] = summarize_observations(
            [
                observation_for_decision(observation)
                for observation in execution.observations
            ]
        )
        allowed_skills = frozenset(step.allowed_skills)
        available_tools = self._tools.tool_catalog(
            role=AgentRole.verifier, allowed_skills=allowed_skills
        )
        premature_need_user_retries = 0
        runtime_feedback: str | None = None
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
            decision_state = {
                "goal": task.goal,
                "plan": plan_json,
                "step": step_json,
                "skill_policy": (
                    self._skills.prompt_policy(step.allowed_skills)
                    if self._skills is not None
                    else ""
                ),
                "available_tools": available_tools,
                "execution": execution_json,
                "execution_tool_calls": summarize_tool_call_history(
                    execution.observations
                ),
                "remaining_tool_calls": (
                    tool_budget.remaining
                    if tool_budget is not None
                    else task.budget.max_tool_calls - len(observations)
                ),
                "remaining_agent_turns": (
                    turn_budget.remaining
                    if turn_budget is not None
                    else task.budget.max_agent_turns - _turn - 1
                ),
                "my_tool_calls": summarized_observations,
                "replan_state": task.replan_state.model_dump(mode="json"),
            }
            if runtime_feedback:
                decision_state["runtime_feedback"] = runtime_feedback
            if model_budget is not None and not model_budget.try_reserve(
                estimate_input_tokens(_VERIFIER_INSTRUCTION, decision_state)
            ):
                return VerifierResult(
                    decision=VerificationDecision.NEED_USER,
                    feedback="Model budget exhausted before independent verification.",
                    observations=observations,
                    error_code="model_budget_exhausted",
                )
            decision = self._gateway.decide(
                role=AgentRole.verifier,
                instruction=_VERIFIER_INSTRUCTION,
                state=decision_state,
                response_model=VerifierDecision,
            )
            if model_budget is not None and not model_budget.record(self._gateway.last_usage):
                return VerifierResult(
                    decision=VerificationDecision.NEED_USER,
                    feedback="Model budget exhausted after independent verification.",
                    observations=observations,
                    error_code="model_budget_exhausted",
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
            blocked_codes = {
                "anti_bot_challenge",
                "captcha",
                "login_required",
                "access_denied",
                "domain_temporarily_blocked",
                "source_unavailable",
            }
            has_blocked_evidence = any(
                observation.error_code in blocked_codes
                for observation in [*execution.observations, *observations]
            )
            if (
                decision.verification_decision is VerificationDecision.NEED_USER
                and available_tools
                and not has_blocked_evidence
                and premature_need_user_retries < 1
                and _turn < task.budget.max_agent_turns - 1
            ):
                premature_need_user_retries += 1
                runtime_feedback = (
                    "Policy correction: NEED_USER is premature because no "
                    "terminal access block is recorded and verifier tools remain. "
                    "Choose RETRY_EXECUTOR only when a specific permitted action "
                    "can produce new evidence; otherwise make the contract-based "
                    "decision now."
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
