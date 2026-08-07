"""External harness graph: planner -> executor -> verifier -> route.

The graph enforces every hard invariant (spec §5); the agents themselves
only ever produce decisions.  Budget counters and decisions live in channel
values so checkpoint/resume never resets them; only the wall-clock window
refreshes on resume.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Sequence

from langchain.agents.structured_output import StructuredOutputValidationError
from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.graph import END, START, StateGraph

from backend.app.domain.agent_runtime import RunStatus, VerificationDecision
from backend.app.services.agent_runtime.schemas import (
    AgentTaskRequest,
    ExecutionPlan,
    ToolObservation,
)
from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.deepagents_runtime.agents import (
    VerifierDecision,
    build_executor_agent,
    build_planner_agent,
    build_verifier_agent,
)
from backend.app.services.deepagents_runtime.budgets import (
    DeepAgentsBudgets,
    TurnBudgetExhausted,
)
from backend.app.services.deepagents_runtime.middleware import current_budgets
from backend.app.services.deepagents_runtime.state import (
    DeepAgentsState,
    build_initial_state,
)
from backend.app.services.deepagents_runtime.tools.adapters import (
    DuplicateCallTracker,
    bind_tool_context,
    build_skill_tools,
)

_OBSERVATION_EXCERPT_LIMIT = 1_200
_STALL_LIMIT = 3


class InvalidModelResponseError(RuntimeError):
    """Raised when an agent's output cannot be parsed into its schema."""


def _agent_thread(run_id: str, step_index: int, role: str) -> str:
    return f"{run_id}:{step_index}:{role}"


def _extract_structured(result: dict[str, Any], model: type[Any]) -> Any:
    """Read the structured output of a deep agent invocation.

    ``create_agent`` places the parsed structured output in the
    ``structured_response`` channel.  Raises InvalidModelResponseError when
    absent (the harness degrades to waiting_user instead of crashing).
    """
    structured = result.get("structured_response")
    if structured is None:
        raise InvalidModelResponseError("missing structured_response")
    if isinstance(structured, model):
        return structured
    return model.model_validate(structured)


def _project_tool_observations(
    messages: Sequence[Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Project tool results into decisions + evidence (incremental, bounded).

    Only tool-produced dicts carrying both ``source_url`` and ``content_hash``
    become evidence (evidence-bound tools invariant).
    """
    decisions: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, ToolMessage):
            continue
        try:
            obs = ToolObservation.model_validate(json.loads(message.content))
        except (ValueError, json.JSONDecodeError):
            continue
        decisions.append(
            {
                "tool": obs.tool_name,
                "status": obs.status,
                "error_code": obs.error_code,
            }
        )
        if obs.status != "succeeded" or obs.output is None:
            continue
        stack = [obs.output]
        seen: set[tuple[str, str]] = set()
        while stack:
            value = stack.pop()
            if isinstance(value, dict):
                if (
                    isinstance(value.get("source_url"), str)
                    and isinstance(value.get("content_hash"), str)
                ):
                    key = (value["source_url"], value["content_hash"])
                    if key not in seen:
                        seen.add(key)
                        evidence.append(value)
                stack.extend(value.values())
            elif isinstance(value, list):
                stack.extend(value)
    return decisions, evidence


def _is_non_progress(decision: dict[str, Any]) -> bool:
    if decision.get("status") != "succeeded":
        return True
    return decision.get("error_code") in {"duplicate_tool_call", "blocked"}


def _sole_skill(allowed_skills: list[str]) -> str:
    if len(allowed_skills) != 1:
        raise ValueError("each plan step must allow exactly one skill")
    return allowed_skills[0]


def _degrade(state: DeepAgentsState, error_code: str) -> dict[str, Any]:
    return {
        "run_status": RunStatus.waiting_user.value,
        "error_code": error_code,
        "final_summary": None,
    }


def _verifier_input(state: DeepAgentsState) -> str:
    plan = ExecutionPlan.model_validate(state["plan_json"])
    step = plan.steps[state["step_index"]]
    evidence_lines = []
    for item in state["evidence_store"][-10:]:
        text = json.dumps(item, ensure_ascii=False)[:_OBSERVATION_EXCERPT_LIMIT]
        evidence_lines.append(text)
    return json.dumps(
        {
            "step_objective": step.objective,
            "success_criteria": step.success_criteria,
            "evidence": evidence_lines,
        },
        ensure_ascii=False,
    )


class DeepAgentsHarness:
    """The deterministic lifecycle around the three deep agents."""

    def __init__(
        self,
        *,
        model_factory: Callable[[str], Any],
        tool_factory: Callable[[str], Sequence[Any]] | None = None,
        checkpointer: Any = None,
    ) -> None:
        self._model_factory = model_factory
        self._tool_factory = tool_factory
        self._checkpointer = checkpointer
        self._tracker = DuplicateCallTracker()
        self._graph = self._build_graph()

    def _build_graph(self) -> Any:
        graph = StateGraph(DeepAgentsState)
        graph.add_node("planner", self._planner_node)
        graph.add_node("executor", self._executor_node)
        graph.add_node("verifier", self._verifier_node)
        graph.add_node("finalize", self._finalize_node)
        graph.add_edge(START, "planner")
        graph.add_conditional_edges(
            "planner",
            lambda state: "finalize" if state["run_status"] else "executor",
            {"executor": "executor", "finalize": "finalize"},
        )
        graph.add_conditional_edges(
            "executor",
            lambda state: "finalize" if state["run_status"] else "verifier",
            {"verifier": "verifier", "finalize": "finalize"},
        )
        graph.add_conditional_edges(
            "verifier",
            self._route,
            {
                "next_step": "executor",
                "replan": "planner",
                "waiting_user": "finalize",
                "failed": "finalize",
                "succeeded": "finalize",
                # _route's first branch returns "finalize" whenever the node
                # wrote a run_status (e.g. a verifier-side degrade); that
                # destination must exist in the branch map.
                "finalize": "finalize",
            },
        )
        graph.add_edge("finalize", END)
        return graph.compile(checkpointer=self._checkpointer)

    def run(
        self,
        request: AgentTaskRequest,
        *,
        run_id: str,
        budgets: DeepAgentsBudgets | None = None,
    ) -> dict[str, Any]:
        budgets = budgets or DeepAgentsBudgets.from_agent_budget(request.budget)
        budgets.start_window()
        initial = build_initial_state(
            run_id=run_id,
            user_id="",
            goal=request.goal,
            allowed_skills=request.allowed_skills,
            context=request.context,
            budgets=budgets,
        )
        final = self._graph.invoke(initial, {"configurable": {"thread_id": run_id}})
        return dict(final)

    def resume(self, run_id: str) -> dict[str, Any]:
        snapshot = self._graph.get_state({"configurable": {"thread_id": run_id}})
        budgets = DeepAgentsBudgets.from_dict(snapshot.values["budget"])
        budgets.refresh_window()
        final = self._graph.invoke(
            {"budget": budgets.to_dict()},
            {"configurable": {"thread_id": run_id}},
        )
        return dict(final)

    # -- nodes -------------------------------------------------------------

    def _planner_node(self, state: DeepAgentsState) -> dict[str, Any]:
        budgets = DeepAgentsBudgets.from_dict(state["budget"])
        if budgets.window_exhausted():
            return _degrade(state, "wall_clock_budget_exhausted")
        previous_status = state["run_status"]
        if (
            previous_status is not None
            and previous_status != RunStatus.waiting_user.value
        ):
            # terminal run resumed -> no-op (only waiting_user is recoverable)
            return {"finished_at": time.time()}
        if state["plan_json"] is not None and not budgets.try_consume_replan():
            return _degrade(state, "max_replans_exceeded")
        planner = build_planner_agent(
            model=self._model_factory("planner"), checkpointer=self._checkpointer
        )
        task_payload = {
            "goal": state["goal"],
            "allowed_skills": state["allowed_skills"],
            "context": state["context"],
        }
        try:
            with current_budgets(budgets):
                result = planner.invoke(
                    {"messages": [HumanMessage(json.dumps(task_payload, ensure_ascii=False))]},
                    {"configurable": {"thread_id": _agent_thread(state["run_id"], 0, "planner")}},
                )
            plan = _extract_structured(result, ExecutionPlan)
            plan.validate_plan_authority()
            for step in plan.steps:
                _sole_skill(step.allowed_skills)
        except (
            TurnBudgetExhausted,
            InvalidModelResponseError,
            ValueError,
            StructuredOutputValidationError,
        ) as exc:
            if isinstance(exc, TurnBudgetExhausted):
                return _degrade(state, str(exc))
            return _degrade(state, "invalid_model_response")
        return {
            "plan_json": plan.model_dump(mode="json"),
            "retry_count": 0,
            "run_status": None,  # clears waiting_user on resume (resume = re-entry)
            "error_code": None,
            "budget": budgets.to_dict(),
            "decisions": [
                {"role": "planner", "decision": "PLANNED", "steps": len(plan.steps)}
            ],
        }

    def _executor_node(self, state: DeepAgentsState) -> dict[str, Any]:
        budgets = DeepAgentsBudgets.from_dict(state["budget"])
        if budgets.window_exhausted():
            return _degrade(state, "wall_clock_budget_exhausted")
        last_decision = state["decisions"][-1] if state["decisions"] else None
        is_retry = bool(
            last_decision
            and last_decision.get("role") == "verifier"
            and last_decision.get("decision") == VerificationDecision.RETRY_EXECUTOR.value
        )
        if is_retry and not budgets.try_consume_replan():
            return _degrade(state, "retries_exceeded")
        plan = ExecutionPlan.model_validate(state["plan_json"])
        step = plan.steps[state["step_index"]]
        skill = _sole_skill(step.allowed_skills)
        if self._tool_factory is not None:
            tools = self._tool_factory(skill)
        else:
            tools = build_skill_tools(
                skill_name=skill,
                budgets=budgets,
                tracker=self._tracker,
            )
        agent = build_executor_agent(
            model=self._model_factory("executor"),
            tools=tools,
            checkpointer=self._checkpointer,
        )
        tool_ctx = ToolContext(
            user_id=state["user_id"],
            run_id=state["run_id"],
            metadata={
                "observed_public_evidence": state["evidence_store"],
                "context": state["context"],
            },
        )
        try:
            with bind_tool_context(tool_ctx):
                with current_budgets(budgets):
                    result = agent.invoke(
                        {"messages": [HumanMessage(step.objective)]},
                        {"configurable": {"thread_id": _agent_thread(state["run_id"], state["step_index"], "executor")}},
                    )
        except TurnBudgetExhausted as exc:
            return _degrade(state, str(exc))
        decisions, evidence = _project_tool_observations(result["messages"])
        decisions = [{"role": "executor", **d} for d in decisions]
        progress_made = any(not _is_non_progress(d) for d in decisions)
        stalled = 0 if progress_made else state["stalled_decisions"] + 1
        if stalled >= _STALL_LIMIT:
            return _degrade(state, "stalled_no_progress")
        return {
            "decisions": decisions,
            "evidence_store": evidence,
            "stalled_decisions": stalled,
            "retry_count": state["retry_count"] + 1 if is_retry else 0,
            "budget": budgets.to_dict(),
        }

    def _verifier_node(self, state: DeepAgentsState) -> dict[str, Any]:
        budgets = DeepAgentsBudgets.from_dict(state["budget"])
        if budgets.window_exhausted():
            return _degrade(state, "wall_clock_budget_exhausted")
        verifier = build_verifier_agent(
            model=self._model_factory("verifier"), checkpointer=self._checkpointer
        )
        try:
            with current_budgets(budgets):
                result = verifier.invoke(
                    {"messages": [HumanMessage(_verifier_input(state))]},
                    {"configurable": {"thread_id": _agent_thread(state["run_id"], state["step_index"], "verifier")}},
                )
            decision = _extract_structured(result, VerifierDecision)
        except (
            TurnBudgetExhausted,
            InvalidModelResponseError,
            StructuredOutputValidationError,
        ) as exc:
            if isinstance(exc, TurnBudgetExhausted):
                return _degrade(state, str(exc))
            return _degrade(state, "invalid_model_response")
        update: dict[str, Any] = {
            "decisions": [
                {
                    "role": "verifier",
                    "decision": decision.decision.value,
                    "rationale": decision.rationale,
                }
            ],
            "budget": budgets.to_dict(),
        }
        if decision.decision == VerificationDecision.PASS:
            update["step_index"] = state["step_index"] + 1
            update["stalled_decisions"] = 0  # fresh step starts a fresh stall count
        return update

    def _finalize_node(self, state: DeepAgentsState) -> dict[str, Any]:
        if state["run_status"] is not None:
            return {"finished_at": time.time()}
        last = state["decisions"][-1] if state["decisions"] else None
        decision = last.get("decision") if last else None
        if decision == VerificationDecision.PASS.value:
            return {
                "run_status": RunStatus.succeeded.value,
                "final_summary": "所有步骤已通过验证",
                "finished_at": time.time(),
            }
        if decision == VerificationDecision.NEED_USER.value:
            return {
                "run_status": RunStatus.waiting_user.value,
                "error_code": "needs_user",
                "finished_at": time.time(),
            }
        return {
            "run_status": RunStatus.failed.value,
            "error_code": "verification_failed",
            "finished_at": time.time(),
        }

    # -- routing -----------------------------------------------------------

    def _route(self, state: DeepAgentsState) -> str:
        if state["run_status"] is not None:
            return "finalize"
        last = state["decisions"][-1] if state["decisions"] else None
        decision = last.get("decision") if last else None
        if decision == VerificationDecision.PASS.value:
            plan = ExecutionPlan.model_validate(state["plan_json"])
            if state["step_index"] >= len(plan.steps):
                return "succeeded"
            return "next_step"
        if decision == VerificationDecision.RETRY_EXECUTOR.value:
            return "next_step"  # same step re-executes; replan budget checked at executor entry
        if decision == VerificationDecision.REPLAN.value:
            return "replan"
        if decision == VerificationDecision.NEED_USER.value:
            return "waiting_user"
        return "failed"
