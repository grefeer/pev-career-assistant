"""Autonomous Executor role for the adaptive PEV runtime."""

from __future__ import annotations

import time
from typing import Any

from backend.app.domain.agent_runtime import AgentRole
from backend.app.services.agent_runtime.model_gateway import AgentModelGateway
from backend.app.services.agent_runtime.observation_projection import (
    observation_for_decision,
    record_observation,
)
from backend.app.services.agent_runtime.schemas import (
    AgentTaskRequest,
    ExecutionPlan,
    ExecutorDecision,
    ExecutorResult,
    PlanStep,
    ToolObservation,
)
from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.agent_runtime.tool_budget import ToolCallBudget
from backend.app.services.agent_runtime.tool_registry import ToolRegistry
from backend.app.services.agent_runtime.tracing import DecisionTrace, decision_summary
from backend.app.services.agent_runtime.turn_budget import AgentTurnBudget

_EXECUTOR_INSTRUCTION = (
    "You are the Executor Agent. Work toward the current planned outcome using "
    "only its permitted Skills. Observe every tool result, including failures, "
    "and independently select the next allowed action. Do not claim an artifact "
    "that is absent from observations; ask the user if the goal cannot proceed. "
    "Do not complete a planned outcome until its stated success criteria and all "
    "user-requested deliverables assigned to this step have tool-backed results; "
    "if evidence cannot support one, state the limitation rather than silently "
    "omitting it. "
    "When context supplies candidate_urls, treat them as a finite candidate set: "
    "prefer fetch-public-job-pages to capture the set in one bounded observation; "
    "otherwise fetch each unique URL at most once, then use the observed artifact IDs to "
    "extract structured details and move to the next requested Skill. Never "
    "re-fetch a URL that is already represented by a successful observation. "
    "Once all supplied candidates have been observed, choose extraction, matching, "
    "tailoring, planning, verification, or a truthful limitation; do not keep "
    "fetching pages. "
    "When candidate_urls is non-empty, do not call public search: the candidate "
    "set is already user-provided evidence to process. "
    "When multiple observed public-page artifacts need detailed JD normalization, "
    "prefer extract-observed-job-details-batch so one evidence-bound tool result "
    "covers the finite set. "
    "When a job-discovery task has no supplied URL, you may first use the "
    "public-job search tool, then independently select a returned direct URL "
    "for evidence capture. Use the user's language and role terms when forming "
    "a search query (Chinese goals need Chinese recruitment terms). After one "
    "search observation, prefer fetching a plausible returned result; retry a "
    "search at most once only when no plausible public career URL was returned. "
    "Do not loop through search-provider or job-board domain variations without "
    "capturing evidence. A search observation with an empty results list is a "
    "verified provider limitation: do not search again; immediately ask the user "
    "for an official careers URL or relax the source constraint. If a fetched "
    "page does not contain a usable JD, state that evidence limitation and choose "
    "a different returned direct URL at most once before asking the user. "
    "When verifier_feedback is present, the Verifier found a tool-backed "
    "deliverable missing from the prior attempt for this same step. The "
    "missing deliverable is named in feedback. Call that named tool next, "
    "reusing the observed public evidence that prior_observations already "
    "captured; do not repeat a discovery tool (fetch/extract/search) whose "
    "result already appears in prior_observations, and do not re-fetch a URL "
    "that prior_observations already observed. "
    "A duplicate_tool_call observation means you just re-issued an identical "
    "tool call that already succeeded: that result is already in observations. "
    "Move to the next distinct action (extract, match, tailor, plan, verify, or "
    "complete) instead of repeating the same call."
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
        tool_budget: ToolCallBudget | None = None,
        turn_budget: AgentTurnBudget | None = None,
        deadline: float | None = None,
        prior_observations: list[ToolObservation] | None = None,
    ) -> ExecutorResult:
        """Execute a step without precomputing its tool sequence in the harness."""
        observations: list[ToolObservation] = []
        tool_context = context
        last_tool_name: str | None = None
        last_tool_input: dict[str, Any] | None = None
        last_tool_succeeded = False
        # Loop-invariant projections of the (immutable) plan/step: serialize
        # once instead of re-dumping and re-building the tool catalog every
        # turn. The catalog is also memoized inside ToolRegistry.
        plan_json = plan.model_dump(mode="json")
        step_json = step.model_dump(mode="json")
        allowed_skills = frozenset(step.allowed_skills)
        available_tools = self._tools.tool_catalog(
            role=AgentRole.executor, allowed_skills=allowed_skills
        )
        prior_observations_for_decision = [
            observation_for_decision(observation)
            for observation in (prior_observations or [])
        ]
        # Decision projections are appended once per observation in lockstep
        # with the raw list, instead of re-projecting every observation each
        # turn (O(turns^2) ``model_dump`` calls on observations that may carry
        # large page text). Each turn reads a fresh shallow copy (the gateway
        # only serializes it).
        observations_for_decision: list[dict[str, object]] = []
        for _turn in range(task.budget.max_agent_turns):
            if deadline is not None and time.monotonic() >= deadline:
                return ExecutorResult(
                    status="failed",
                    summary="Wall-clock budget exhausted before the next decision.",
                    observations=observations,
                    error_code="wall_clock_budget_exhausted",
                )
            if turn_budget is not None and not turn_budget.try_consume():
                return ExecutorResult(
                    status="failed",
                    summary="Agent-turn budget exhausted before the next decision.",
                    observations=observations,
                    error_code="agent_turn_budget_exhausted",
                )
            decision = self._gateway.decide(
                role=AgentRole.executor,
                instruction=_EXECUTOR_INSTRUCTION,
                state={
                    "goal": task.goal,
                    "context": task.context,
                    "private_context": task.private_context,
                    "remaining_tool_calls": (
                        tool_budget.remaining if tool_budget is not None
                        else task.budget.max_tool_calls - len(observations)
                    ),
                    "remaining_agent_turns": (
                        turn_budget.remaining
                        if turn_budget is not None
                        else task.budget.max_agent_turns - _turn - 1
                    ),
                    "plan": plan_json,
                    "step": step_json,
                    "available_tools": available_tools,
                    "observations": list(observations_for_decision),
                    "prior_observations": prior_observations_for_decision,
                    "verifier_feedback": task.context.get("verifier_feedback", []),
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
                if (
                    decision.tool_name == "search-public-job-pages"
                    and _has_candidate_urls(task)
                ):
                    record_observation(
                        observations,
                        observations_for_decision,
                        ToolObservation(
                            tool_name=decision.tool_name,
                            status="failed",
                            error_code="candidate_urls_already_supplied",
                        ),
                    )
                    continue
                if (
                    last_tool_succeeded
                    and decision.tool_name == last_tool_name
                    and decision.tool_input == last_tool_input
                ):
                    record_observation(
                        observations,
                        observations_for_decision,
                        ToolObservation(
                            tool_name=decision.tool_name or "",
                            status="failed",
                            error_code="duplicate_tool_call",
                        ),
                    )
                    continue
                if tool_budget is not None and not tool_budget.try_consume():
                    return ExecutorResult(
                        status="failed",
                        summary="Tool-call budget exhausted before executing the next action.",
                        observations=observations,
                        error_code="tool_budget_exhausted",
                    )
                observation = self._tools.invoke(
                    role=AgentRole.executor,
                    name=decision.tool_name or "",
                    context=tool_context,
                    payload=decision.tool_input,
                    allowed_skills=allowed_skills,
                )
                record_observation(observations, observations_for_decision, observation)
                tool_context = _with_observed_page(tool_context, observation)
                last_tool_name = decision.tool_name
                last_tool_input = decision.tool_input
                last_tool_succeeded = observation.status == "succeeded"
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


def _with_observed_page(
    context: ToolContext, observation: ToolObservation
) -> ToolContext:
    """Expose a successful page fetch to the next Executor-selected tool call."""
    output = observation.output or {}
    raw_pages = output.get("pages")
    pages = raw_pages if isinstance(raw_pages, list) else [output]
    existing = context.metadata.get("observed_public_evidence", [])
    evidence = list(existing) if isinstance(existing, list) else []
    seen_artifact_ids = {
        item.get("artifact_id") for item in evidence if isinstance(item, dict)
    }
    for page in pages:
        if not isinstance(page, dict) or not all(
            isinstance(page.get(key), str) and page[key]
            for key in ("artifact_id", "source_url", "content_hash", "visible_text")
        ):
            continue
        if page["artifact_id"] in seen_artifact_ids:
            continue
        evidence.append(
            {
                "artifact_id": page["artifact_id"],
                "source_url": page["source_url"],
                "content_hash": page["content_hash"],
                "visible_text": page["visible_text"],
                "title": page.get("title"),
            }
        )
        seen_artifact_ids.add(page["artifact_id"])
    if not evidence:
        return context
    metadata = dict(context.metadata)
    metadata["observed_public_evidence"] = evidence
    return ToolContext(user_id=context.user_id, run_id=context.run_id, metadata=metadata)


def _has_candidate_urls(task: AgentTaskRequest) -> bool:
    """Avoid redundant public search when the user already bounded the evidence set."""
    candidate_urls = task.context.get("candidate_urls")
    return isinstance(candidate_urls, list) and any(
        isinstance(url, str) and url.strip() for url in candidate_urls
    )

