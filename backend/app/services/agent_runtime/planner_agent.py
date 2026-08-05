"""Autonomous Planner role for the adaptive PEV runtime."""

from __future__ import annotations

import time

from backend.app.domain.agent_runtime import AgentRole
from backend.app.services.agent_runtime.model_gateway import AgentModelGateway
from backend.app.services.agent_runtime.observation_projection import record_observation
from backend.app.services.agent_runtime.schemas import (
    AgentTaskRequest,
    ExecutionPlan,
    PlannerDecision,
    PlannerResult,
    ToolObservation,
)
from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.agent_runtime.tool_budget import ToolCallBudget
from backend.app.services.agent_runtime.tool_registry import ToolRegistry
from backend.app.services.agent_runtime.tracing import DecisionTrace, decision_summary
from backend.app.services.agent_runtime.turn_budget import AgentTurnBudget

_PLANNER_INSTRUCTION = (
    "You are the Planner Agent. Observe only the supplied user-scoped context "
    "and prior tool observations. You may call permitted low-risk context tools "
    "when information is insufficient. Then produce an outcome-based plan with "
    "success criteria and Skill authority, or ask the user a concrete question. "
    "For a public job-discovery goal, an absent user-supplied URL is not by "
    "itself missing context: the Executor can safely search public pages before "
    "capturing evidence. Ask only for genuinely personal constraints or facts "
    "that cannot be observed from public sources. "
    "Treat every explicitly requested user deliverable as a mandatory plan "
    "outcome, not an optional suggestion. Decompose a multi-deliverable request "
    "into one separate step per requested deliverable, and scope each step's "
    "allowed_skills to the single Skill that produces that deliverable: a "
    "job-discovery step (allowed_skills=[\"job-discovery\"]) captures public JD "
    "evidence; a job-matching step (allowed_skills=[\"job-matching\"]) ranks "
    "observed jobs against the profile; a resume-tailoring step "
    "(allowed_skills=[\"resume-tailoring\"]) produces grounded resume changes; "
    "a career-planning step (allowed_skills=[\"career-planning\"]) produces an "
    "interview or preparation plan. Do not combine multiple Skills in one step, "
    "because the Executor only sees tools for the current step's allowed_skills: "
    "a discovery step that also promises a recommendation hides the matching "
    "tool from the Executor and the deliverable is never produced. A request for "
    "a ranked job recommendation must include a separate job-matching step after "
    "discovery; a request for grounded resume changes must include a separate "
    "resume-tailoring step; and a request for an interview or preparation plan "
    "must include a separate career-planning step. Do not mark the plan "
    "successful if an explicitly requested deliverable is omitted. "
    "When confirmed profile fact fields are supplied, those facts already exist "
    "on the server: plan the matching/tailoring work instead of asking the user "
    "to upload the same resume again. The Executor can inspect the fact values "
    "through its private, scoped context. "
    "When context.candidate_urls is a non-empty list, those URLs are "
    "already-collected candidate job pages supplied by the user. Plan an "
    "evidence-capture step scoped to exactly those URLs (the Executor must not "
    "search the open web; search is hard-blocked while candidate URLs exist) "
    "followed by one separate step per requested deliverable (job-matching, "
    "resume-tailoring, career-planning). Never emit a plan whose only step is "
    "job-discovery when the goal asks for ranking, matching, tailoring, or "
    "planning: every requested deliverable must have its own step, or the "
    "Executor has no tool with which to produce it. Copy every candidate URL "
    "character-for-character into the step objective exactly as supplied; "
    "never abbreviate, re-type, or guess URL characters, because a single "
    "altered character turns a valid page into a 404 that blocks the run. "
    "A chain_context note that mentions a previous session's captured evidence "
    "refers to artifacts that do NOT exist in the current session: when "
    "chain_context is present and candidate_urls are supplied, always plan a "
    "job-discovery evidence-capture step over candidate_urls first, then the "
    "deliverable step(s), so the Executor re-captures the pages itself. Never "
    "plan a deliverable step that assumes pre-existing evidence artifacts "
    "when the current context carries only candidate URLs."
    "When a resume-tailoring, career-planning, or job-matching goal names a "
    "role or skill set but supplies no JD text, no URL, and no candidate list, "
    "that is not missing context: plan a preliminary job-discovery step whose "
    "objective names the role and instructs the Executor to search public pages "
    "and capture ONE representative public JD for that role, followed by the "
    "deliverable step (tailoring, planning, or matching) grounded on that "
    "captured evidence. The deliverable tools require an observed JD artifact "
    "(target_artifact_id); without captured evidence the Executor cannot "
    "produce the deliverable and will ask the user instead. Never ask the user "
    "for a JD that can be obtained from public sources. Ask the user only when "
    "the goal references a specific document that only the user can supply "
    "(e.g. 'this JD', 'my pasted link', 'my resume text') or a genuinely "
    "personal fact not in the confirmed profile. A goal that references "
    "session entities such as 'these positions' without a corresponding "
    "candidate list is likewise not missing context: plan a discovery step for "
    "the named role and a job-matching step over the captured JDs. "
    "When the goal names the recruitment data source (校招内推汇总表/内推台账/"
    "招聘数据源/就业信息网), that source is an internal always-available "
    "dataset, not a file the user must upload: plan a job-discovery step whose "
    "objective instructs the Executor to query the data source for companies "
    "updated within the goal's stated recency window, then verify each "
    "company's jobs at its apply links. Never ask the user to provide or "
    "upload that data source."
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
        tool_budget: ToolCallBudget | None = None,
        turn_budget: AgentTurnBudget | None = None,
        deadline: float | None = None,
    ) -> PlannerResult:
        """Sense context and form a bounded execution plan for every request."""
        observations: list[ToolObservation] = []
        observations_for_decision: list[dict[str, object]] = []
        confirmed_facts = task.private_context.get("confirmed_profile_facts")
        fact_fields = (
            sorted(field for field in confirmed_facts if isinstance(field, str))
            if isinstance(confirmed_facts, dict)
            else []
        )
        # The plan/allowed-skills are immutable for this run, so the tool
        # catalog is a loop-invariant projection (also memoized in ToolRegistry).
        allowed_skills = frozenset(task.allowed_skills)
        available_tools = self._tools.tool_catalog(
            role=AgentRole.planner, allowed_skills=allowed_skills
        )
        for _turn in range(task.budget.max_agent_turns):
            if deadline is not None and time.monotonic() >= deadline:
                return PlannerResult(
                    status="failed",
                    observations=observations,
                    error_code="wall_clock_budget_exhausted",
                )
            if turn_budget is not None and not turn_budget.try_consume():
                return PlannerResult(
                    status="failed",
                    observations=observations,
                    error_code="agent_turn_budget_exhausted",
                )
            decision = self._gateway.decide(
                role=AgentRole.planner,
                instruction=_PLANNER_INSTRUCTION,
                state={
                    "goal": task.goal,
                    "allowed_skills": task.allowed_skills,
                    "available_tools": available_tools,
                    "context": task.context,
                    "confirmed_profile_fact_fields": fact_fields,
                    "remaining_tool_calls": (
                        tool_budget.remaining if tool_budget is not None
                        else task.budget.max_tool_calls - len(observations)
                    ),
                    "remaining_agent_turns": (
                        turn_budget.remaining
                        if turn_budget is not None
                        else task.budget.max_agent_turns - _turn - 1
                    ),
                    "observations": list(observations_for_decision),
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
                if tool_budget is not None and not tool_budget.try_consume():
                    return PlannerResult(
                        status="failed",
                        observations=observations,
                        error_code="tool_budget_exhausted",
                    )
                record_observation(
                    observations,
                    observations_for_decision,
                    self._tools.invoke(
                        role=AgentRole.planner,
                        name=decision.tool_name or "",
                        context=context,
                        payload=decision.tool_input,
                    ),
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
