"""Thin lifecycle harness for autonomous Planner–Executor–Verifier runs."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.db.models import AgentEvent, AgentRun, AgentStep
from backend.app.domain.agent_runtime import (
    AgentRole,
    RunStatus,
    StepStatus,
    VerificationDecision,
)
from backend.app.repositories import agent_runtime as run_repository
from backend.app.services.agent_runtime.error_policy import (
    TerminalContract,
    build_terminal_contract,
)
from backend.app.services.agent_runtime.executor.execution_state import (
    MAX_CONSECUTIVE_STALLS as _MAX_CONSECUTIVE_STALLS,
)
from backend.app.services.agent_runtime.executor_agent import ExecutorAgent
from backend.app.services.agent_runtime.model_gateway import AgentModelGatewayError
from backend.app.services.agent_runtime.completion_gate import evaluate_completion_gate
from backend.app.services.agent_runtime.model_budget import ModelCallBudget
from backend.app.services.agent_runtime.planner_agent import PlannerAgent
from backend.app.services.agent_runtime.schemas import (
    AgentBudget,
    AgentTaskRequest,
    ExecutionPlan,
    ExecutorResult,
    PlanStep,
    PlannerResult,
    ReplanReason,
    ToolObservation,
)
from backend.app.services.agent_runtime.skill_definition import SkillRegistry
from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.agent_runtime.tool_budget import ToolCallBudget
from backend.app.services.agent_runtime.turn_budget import AgentTurnBudget
from backend.app.services.career_skills.manifest import (
    skill_observation_is_semantically_valid,
)
from backend.app.services.career_skills.registry import TOOL_ARTIFACT_TYPE
from backend.app.services.career_skills.discovery_policy import (
    DERIVED_COMPANY_KEYWORDS,
    DERIVED_LOCATION_KEYWORDS,
    DERIVED_ROLE_KEYWORDS,
    job_search_results_are_routable,
)
from backend.app.services.career_skills.discovery_recovery import (
    auto_extract_jd_details as _skill_auto_extract,
    recover_discovery_evidence as _skill_recover,
)
from backend.app.services.career_skills.matching_recovery import (
    build_matching_deliverable as _skill_build_matching,
)
from backend.app.services.career_skills.matching_recovery import (
    matching_report_target_artifact_id as _skill_matching_target,
)
from backend.app.services.career_skills.tailoring_recovery import (
    build_tailoring_deliverable as _skill_build_tailoring,
)

#: Per-candidate section truncation when projecting extract outputs into the
#: tool context: keyword scoring needs representative text, not full JD text.
_STRUCTURED_SECTION_CHARS = 600

#: Marker appended to the replan feedback when a NEED_USER decision over a
#: satisfied deterministic contract is converted to a bounded REPLAN. The run
#: loop appends the outcome summary to verifier_feedback, so the marker's
#: presence there makes the conversion once-per-run: a second identical
#: NEED_USER after the replan keeps the human hand-off instead of looping.
_NEEDS_USER_REPLAN_MARKER = "<needs_user_replan>"

#: Marker appended to the replan feedback when a RETRY-cap exhaustion over a
#: satisfied deterministic contract is converted to a bounded REPLAN (N3).
#: Mirrors ``_NEEDS_USER_REPLAN_MARKER``: the run loop appends the outcome
#: summary to verifier_feedback, so a second identical exhaustion after the
#: replan keeps the human hand-off instead of looping.
_RETRY_REPLAN_MARKER = "<retry_replan>"

#: Terminal reason codes a waiting_user pause may be auto-recovered from:
#: verifier/model-decision hand-offs plus the executor's own model-decision
#: hand-offs (stalled route, supplied candidate set, missing/mismatched target
#: evidence). Source-access blocks (login/captcha/anti-bot), repeated-plan
#: oscillation guards, and hard budget failures never auto-recover: blocked
#: evidence is a policy hand-off and the other two are provably futile or
#: terminal.
_AUTO_RECOVERY_ELIGIBLE_REASONS = frozenset({
    "need_user",
    "verification_failed",
    "no_progress_duplicate",
    "invalid_model_response",
    "wall_clock_budget_exhausted",
    "route_already_consumed",
    "candidate_urls_already_supplied",
    "target_evidence_not_found",
    "target_role_mismatch",
    "target_source_mismatch",
})

#: Pause event types whose payload carries a terminal.v1 contract. The latest
#: one decides auto-recovery eligibility, whichever agent produced the pause.
_AUTO_RECOVERY_PAUSE_EVENT_TYPES = frozenset({
    "run_needs_user",
    "planner_needs_user",
    "planner_budget_exhausted",
})

# A discovery question can be answered conclusively by a verified zero-match
# result. Keep this vocabulary deliberately narrow: the rescue below also
# requires a satisfied deterministic contract, unblocked evidence, and a
# persisted job-page/detail artifact.
_DISCOVERY_EXISTENCE_QUERY_MARKERS = ("吗", "是否", "有没有", "有无")
_VERIFIED_ZERO_MATCH_MARKERS = (
    "未发现",
    "没有符合",
    "没有满足",
    "无符合",
    "无一",
    "不存在符合",
    "均为社招",
    "零匹配",
    "0个",
    "0 个",
)
_DISCOVERY_EVIDENCE_ARTIFACT_TYPES = frozenset(
    {"public_job_page", "structured_job_details"}
)
_TERMINAL_NEGATIVE_DISCOVERY_CODE = "terminal_negative_discovery_complete"

#: Per-candidate cap for the full JD text preserved alongside the bounded
#: sections (``full_text``). Extraction outputs are already bounded by the
#: page's visible text (itself capped at 32k), so this engages defensively
#: only for unusually large pages.
_STRUCTURED_FULL_TEXT_CHARS = 32_000


def _is_external_runtime_code(value: object) -> bool:
    if not isinstance(value, str):
        return False
    return value in {
        "anti_bot",
        "anti_bot_challenge",
        "captcha",
        "login_required",
        "access_denied",
        "domain_temporarily_blocked",
        "adapter:empty_result",
        "adapter:adapter_invalid",
        "adapter:adapter_error",
    } or value.startswith("adapter:http_error:")


class StepDependencyError(ValueError):
    """A typed PlanStep input could not be resolved from prior run state."""


def _context_value(context: dict[str, Any], name: str) -> Any:
    """Read a dotted context path without allowing arbitrary object access."""
    value: Any = context
    for segment in name.split("."):
        if not isinstance(value, dict) or segment not in value:
            return None
        value = value[segment]
    return value


def _task_input_value(task: AgentTaskRequest, name: str) -> Any:
    """Resolve a typed context input from task roots or user context.

    ``goal`` is a first-class task field rather than an entry in the free-form
    context map. Supporting it here keeps PlanStep inputs structured without
    forcing the Planner to duplicate the goal into mutable context.
    """
    if name == "goal":
        return task.goal
    if name == "allowed_skills":
        return list(task.allowed_skills)
    if name == "confirmed_profile_fact_fields":
        facts = task.private_context.get("confirmed_profile_facts")
        if isinstance(facts, dict):
            return sorted(key for key in facts if isinstance(key, str))
        return None
    if name == "confirmed_profile_facts":
        return task.private_context.get("confirmed_profile_facts")
    if name.startswith("private_context."):
        return _context_value(task.private_context, name.removeprefix("private_context."))
    if name.startswith("context."):
        name = name.removeprefix("context.")
    return _context_value(task.context, name)




def _compile_task_context(task: AgentTaskRequest) -> AgentTaskRequest:
    """Fill only explicit, deterministic query fields absent from task context.

    Planner outputs often reference the career-sheet query ports directly. A
    missing port should not force a replan when the same value is plainly
    stated in the user's goal. This compiler never invents a default time
    window or preference: it derives a value only from an explicit phrase and
    preserves all caller-provided keys unchanged.
    """
    context = dict(task.context)
    goal = task.goal
    lowered = goal.lower()
    if "role_keywords" not in context:
        role_keywords = [
            term
            for term in DERIVED_ROLE_KEYWORDS
            if term.lower() in lowered
        ]
        if role_keywords:
            context["role_keywords"] = list(dict.fromkeys(role_keywords))[:5]
    if "company_keywords" not in context:
        company_keywords = [
            term for term in DERIVED_COMPANY_KEYWORDS if term.lower() in lowered
        ]
        if company_keywords:
            context["company_keywords"] = list(dict.fromkeys(company_keywords))[:5]
    if "location_keywords" not in context:
        location_keywords = [term for term in DERIVED_LOCATION_KEYWORDS if term in goal]
        if location_keywords:
            context["location_keywords"] = list(dict.fromkeys(location_keywords))[:5]
    if "recent_days" not in context:
        window_text = " ".join(
            str(value)
            for value in (goal, context.get("time_window_text"), context.get("time_window"))
            if value is not None
        )
        match = re.search(r"(?:最近|近|过去|过去的)\s*(\d+)\s*(?:天|日)", window_text)
        if match:
            context["recent_days"] = int(match.group(1))
    if context == task.context:
        return task
    return task.model_copy(update={"context": context})


def _is_private_task_input(name: str) -> bool:
    """Identify input names that must never be copied into public model context."""
    return name == "confirmed_profile_facts" or name.startswith("private_context.")


@dataclass(frozen=True)
class AgentRunResult:
    """Safe terminal or waiting summary returned by the application service."""

    run_id: str
    status: RunStatus
    summary: str | None
    error_code: str | None = None
    replan_reason: ReplanReason | None = None


class _AgentRecoveryContext:
    """Runtime-owned capabilities injected into skill-layer discovery recovery."""

    def __init__(
        self,
        *,
        runtime: "AgentRuntime",
        db: Session,
        run_id: str,
        task: AgentTaskRequest,
        plan_step: PlanStep,
        persisted_step: AgentStep,
        context: ToolContext,
        tool_budget: ToolCallBudget,
    ) -> None:
        self._runtime = runtime
        self._db = db
        self._run_id = run_id
        self._task = task
        self._plan_step = plan_step
        self._persisted_step = persisted_step
        self._context = context
        self._tool_budget = tool_budget

    @property
    def user_id(self) -> str:
        return self._context.user_id

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def task_goal(self) -> str:
        return self._task.goal

    @property
    def step_id(self) -> str:
        return self._plan_step.step_id

    @property
    def task_context(self) -> dict[str, Any]:
        return self._task.context

    @property
    def metadata(self) -> dict[str, Any]:
        return self._context.metadata

    def has_registered_tool(self, name: str) -> bool:
        return self._runtime._executor.has_registered_tool(name)

    def invoke_tool(
        self,
        name: str,
        payload: dict[str, Any],
        *,
        metadata: dict[str, Any] | None = None,
    ) -> ToolObservation:
        if metadata is None:
            tool_context = self._runtime._tool_context(
                user_id=self.user_id,
                run_id=self._run_id,
                task=self._task,
                db=self._db,
            )
        else:
            tool_context = ToolContext(
                user_id=self.user_id,
                run_id=self._run_id,
                metadata=metadata,
            )
        return self._runtime._executor.invoke_registered_tool(
            name=name, context=tool_context, payload=payload
        )

    def persist(
        self, execution: ExecutorResult, *, mark: str | None = None
    ) -> list[dict[str, str]]:
        refs = self._runtime._persist_observed_evidence(
            self._db, self._run_id, self._persisted_step, execution
        )
        if mark:
            for ref in refs:
                ref[mark] = "true"
        return refs

    def consume_tool_budget(self) -> bool:
        return self._tool_budget.try_consume()

    def append_event(self, event_type: str, payload: dict[str, Any]) -> None:
        run_repository.append_event(
            self._db,
            run_id=self._run_id,
            event_type=event_type,
            payload_json=payload,
        )

    def child(self, metadata: dict[str, Any]) -> "_AgentRecoveryContext":
        return _AgentRecoveryContext(
            runtime=self._runtime,
            db=self._db,
            run_id=self._run_id,
            task=self._task,
            plan_step=self._plan_step,
            persisted_step=self._persisted_step,
            context=ToolContext(
                user_id=self.user_id,
                run_id=self._run_id,
                metadata=metadata,
            ),
            tool_budget=self._tool_budget,
        )

    @property
    def task_private_context(self) -> dict[str, Any]:
        return self._task.private_context

    def structured_job_candidates(self) -> list[dict[str, Any]]:
        return _structured_job_candidates(self._db, self._run_id)

    def list_evidence_artifacts(self) -> list[Any]:
        return run_repository.list_evidence_artifacts(self._db, self._run_id)

    def matching_report_target_artifact_id(self) -> str | None:
        return _skill_matching_target(
            self.list_evidence_artifacts(), self.structured_job_candidates()
        )

    def persisted_job_search_observations(
        self, artifact_refs: list[dict[str, Any]]
    ) -> list[ToolObservation]:
        return _persisted_job_search_observations(
            self._db, self._run_id, artifact_refs
        )


class AgentRuntime:
    """Schedules agent-produced decisions while enforcing only hard lifecycle bounds."""

    def __init__(
        self,
        *,
        planner: PlannerAgent,
        executor: ExecutorAgent,
        verifier: object | None = None,
        agent_version: str,
        skills: SkillRegistry,
    ) -> None:
        # Stage 1.3b: the legacy ``skills: SkillRegistry | None = None``
        # migration path was removed. The application composition root
        # (``main.py:113-119``) always injects a real registry; the four
        # gate helpers (``_step_contract_met`` / ``_has_blocked_evidence`` /
        # ``_step_has_blocked_evidence`` / ``_completion_gate_rejected``)
        # and ``_requires_verification`` now call ``self._skills`` directly.
        assert skills is not None, "AgentRuntime requires an injected SkillRegistry"
        self._planner = planner
        self._executor = executor
        self._verifier = verifier
        self._agent_version = agent_version
        self._skills = skills

    def run(
        self,
        db: Session,
        *,
        user_id: str,
        task: AgentTaskRequest,
        existing_run: AgentRun | None = None,
    ) -> AgentRunResult:
        """Run bounded PEV lifecycle; agents retain all semantic tool decisions.

        A run that pauses as ``waiting_user`` for a verifier/model-decision
        reason (never a source-access block) is automatically resumed by the
        harness itself, with a step-up budget and a relaxed stall breaker, up
        to ``task.budget.max_auto_recoveries`` times before the human is asked.
        """
        result = self._run_once(
            db, user_id=user_id, task=task, existing_run=existing_run
        )
        return self._auto_recover(db, user_id=user_id, task=task, result=result)

    @staticmethod
    def _last_needs_user_contract(db: Session, run_id: str) -> dict[str, Any] | None:
        """The terminal contract of the most recent needs-user pause event.

        Covers executor/verifier pauses (``run_needs_user``) and planner-level
        pauses (``planner_needs_user`` / ``planner_budget_exhausted``), so a
        model-decision hand-off is auto-recoverable whichever agent produced it.
        """
        event = db.scalars(
            select(AgentEvent)
            .where(
                AgentEvent.run_id == run_id,
                AgentEvent.event_type.in_(_AUTO_RECOVERY_PAUSE_EVENT_TYPES),
            )
            .order_by(AgentEvent.sequence.desc())
            .limit(1)
        ).first()
        if event is None:
            return None
        payload = event.payload_json or {}
        return payload if payload.get("contract_version") == "terminal.v1" else None

    @staticmethod
    def _auto_recovery_eligible(contract: dict[str, Any]) -> bool:
        """Auto-recovery is for model/verifier pauses, never blocked sources."""
        if contract.get("resumable") is False:
            return False
        evidence = contract.get("evidence")
        if isinstance(evidence, dict) and evidence.get("blocked") is True:
            return False
        return contract.get("reason_code") in _AUTO_RECOVERY_ELIGIBLE_REASONS

    @staticmethod
    def _upgraded_budget(budget: AgentBudget, attempts_used: int) -> AgentBudget:
        """Step-up budget per auto-recovery attempt (x1.5, then x2.0, capped)."""
        factor = 1.0 + 0.5 * (attempts_used + 1)

        def scale(value: int, cap: int) -> int:
            return min(cap, max(1, int(value * factor)))

        return AgentBudget(
            max_agent_turns=scale(budget.max_agent_turns, 100),
            max_tool_calls=scale(budget.max_tool_calls, 200),
            max_replans=scale(budget.max_replans, 10),
            max_wall_clock_seconds=scale(budget.max_wall_clock_seconds, 3_600),
            max_model_requests=scale(budget.max_model_requests, 500),
            max_input_tokens=scale(budget.max_input_tokens, 2_000_000),
            max_output_tokens=scale(budget.max_output_tokens, 500_000),
            max_auto_recoveries=budget.max_auto_recoveries,
        )

    def _auto_recover(
        self,
        db: Session,
        *,
        user_id: str,
        task: AgentTaskRequest,
        result: AgentRunResult,
    ) -> AgentRunResult:
        """Bounded self-resume of a recoverable waiting_user pause.

        Each attempt carries a step-up budget (turn/tool/replan/model/wall
        ceilings scale 1.5x then 2x) and a relaxed consecutive-stall breaker
        (4 then 5), so a verifier-confirmed retry has room to finish instead
        of re-tripping the same cap. Source-access blocks and oscillation
        guards never auto-recover; the pause goes to the human unchanged.
        """
        if result.status is not RunStatus.waiting_user:
            return result
        raw_attempts = task.context.get("auto_recovery_attempts")
        try:
            attempts_used = int(raw_attempts)
        except (TypeError, ValueError):
            attempts_used = 0
        attempts_used = max(0, attempts_used)
        if attempts_used >= task.budget.max_auto_recoveries:
            return result
        run = db.get(AgentRun, result.run_id)
        if run is None:  # defensive: foreign-key integrity normally prevents this.
            return result
        contract = self._last_needs_user_contract(db, run.id)
        if contract is None or not self._auto_recovery_eligible(contract):
            return result
        feedback = (run.final_summary or "").strip()[:2000]
        next_context = dict(task.context)
        next_context["auto_recovery_attempts"] = attempts_used + 1
        if feedback:
            next_context["auto_recovery_feedback"] = feedback
        next_context["max_consecutive_stalls"] = _MAX_CONSECUTIVE_STALLS + attempts_used + 1
        upgraded_task = task.model_copy(
            update={
                "context": next_context,
                "budget": self._upgraded_budget(task.budget, attempts_used),
            }
        )
        run_repository.start_run(db, run)
        run_repository.append_event(
            db,
            run_id=run.id,
            event_type="run_auto_recovered",
            payload_json={
                "attempt": attempts_used + 1,
                "reason_code": contract.get("reason_code"),
                "feedback": feedback[:1000],
            },
        )
        return self.run(
            db, user_id=user_id, task=upgraded_task, existing_run=run
        )

    def _run_once(
        self,
        db: Session,
        *,
        user_id: str,
        task: AgentTaskRequest,
        existing_run: AgentRun | None = None,
    ) -> AgentRunResult:
        """One bounded PEV pass (plan -> execute -> verify loop)."""
        task = _compile_task_context(task)
        run = existing_run
        if run is None:
            run = run_repository.create_run(
                db,
                user_id=user_id,
                goal=task.goal,
                allowed_skills=task.allowed_skills,
                context_summary=task.context,
                budget_json=task.budget.model_dump(mode="json"),
                agent_version=self._agent_version,
            )
        if run.status is RunStatus.queued:
            run_repository.start_run(db, run)
            run_repository.append_event(
                db,
                run_id=run.id,
                event_type="run_started",
                payload_json={"agent_version": self._agent_version},
            )
            self._checkpoint(db)
            revision = 0
            consumed_turns = 0
            consumed_tool_calls = 0
        else:
            revision = run_repository.count_plans(db, run.id)
            consumed_turns = run_repository.count_turns(db, run.id)
            consumed_tool_calls = run_repository.count_tool_decisions(db, run.id)
            task = self._with_observed_public_evidence(db, task, run.id)
        context = self._tool_context(user_id=user_id, run_id=run.id, task=task, db=db)
        tool_budget = ToolCallBudget(
            task.budget.max_tool_calls, used=consumed_tool_calls
        )
        consumed_model_requests, consumed_input_tokens, consumed_output_tokens = (
            run_repository.model_usage_totals(db, run.id)
        )
        model_budget = ModelCallBudget(
            task.budget.max_model_requests,
            task.budget.max_input_tokens,
            task.budget.max_output_tokens,
            requests_used=consumed_model_requests,
            input_tokens_used=consumed_input_tokens,
            output_tokens_used=consumed_output_tokens,
        )
        turn_budget = AgentTurnBudget(
            task.budget.max_agent_turns, used=consumed_turns
        )
        deadline = time.monotonic() + task.budget.max_wall_clock_seconds
        planning_task = task
        # `revision` tracks the number of plans persisted for this run: 0 for a
        # fresh run, or count_plans on resume (revision == 1 after the initial
        # plan). replans already consumed = plans beyond the first, i.e.
        # max(0, revision - 1). Recovering a crashed run must NOT reset this to
        # zero, or a budget already spent on replanning becomes spendable again.
        replans = max(0, revision - 1, planning_task.replan_state.count)
        accepted_plan_fingerprints: dict[str, int] = {}
        for persisted in run_repository.list_plans(db, run.id):
            fingerprint = self._plan_fingerprint_json(persisted.plan_json)
            accepted_plan_fingerprints[fingerprint] = (
                accepted_plan_fingerprints.get(fingerprint, 0) + 1
            )
        trace = self._build_decision_trace(
            db,
            run.id,
            initial_turn_indices=run_repository.turn_indices_by_role(db, run.id),
        )
        # N4: the plan a replan was requested from, paired with its feedback.
        # Set in the replan branch below and consumed at the next planner
        # output, so the isomorphic-replan guard can compare structures
        # before a repeated plan is re-executed.
        replan_source: tuple[ExecutionPlan, str] | None = None
        while True:
            try:
                planner_result = self._planner.run(
                    task=planning_task,
                    context=context,
                    trace=trace,
                    tool_budget=tool_budget,
                    turn_budget=turn_budget,
                    model_budget=model_budget,
                    deadline=deadline,
                )
            except AgentModelGatewayError as error:
                if error.code == "invalid_model_response":
                    fallback_builder = getattr(
                        self._planner, "build_seeded_fallback", None
                    )
                    fallback = (
                        fallback_builder(planning_task)
                        if callable(fallback_builder)
                        else None
                    )
                    if isinstance(fallback, ExecutionPlan):
                        run_repository.append_event(
                            db,
                            run_id=run.id,
                            event_type="planner_seeded_fallback",
                            payload_json={
                                "reason": "invalid_model_response",
                                "step_count": len(fallback.steps),
                            },
                        )
                        planner_result = PlannerResult(
                            status="planned",
                            plan=fallback,
                            observations=[],
                        )
                    else:
                        return self._finish_planner_invalid_model(db, run.id, run)
                else:
                    return self._fail_run(db, run.id, error.code)
            if planner_result.status != "planned" or planner_result.plan is None:
                return self._finish_planner_non_plan(db, run.id, run, planner_result)

            plan = planner_result.plan
            if replan_source is not None:
                source_plan, source_feedback = replan_source
                replan_source = None
                if (
                    task.budget.max_replans >= 2
                    and replans >= task.budget.max_replans
                    and self._plans_isomorphic(source_plan, plan)
                ):
                    # N4 isomorphic-replan guard: the replanned plan repeats
                    # the exact step sequence (normalized skill + objective)
                    # that just failed to converge, and this replan exhausts
                    # the allowed budget. Re-executing the identical structure
                    # can only burn the remaining turn budget in a C008-style
                    # oscillation, so the run terminates honestly as
                    # waiting_user instead. The verifier's rejection right is
                    # unchanged -- the guard only short-circuits a provably
                    # repeated plan; a structurally different plan always
                    # executes. max_replans >= 2 keeps the guard off at a
                    # budget of 1, where the loop is already bounded to two
                    # planner passes.
                    question = re.sub(r"<[a-z_]+>", "", source_feedback).strip()
                    run_repository.finish_run(
                        db,
                        run,
                        status=RunStatus.waiting_user,
                        final_summary=question,
                    )
                    run_repository.append_event(
                        db,
                        run_id=run.id,
                        event_type="replan_isomorphic_guard",
                        payload_json={
                            "feedback": source_feedback,
                            **build_terminal_contract(
                                error_code="repeated_plan_fingerprint",
                                source_role="planner",
                                phase="planning",
                            ).as_payload(),
                        },
                    )
                    return AgentRunResult(run.id, RunStatus.waiting_user, question, None)
            plan_fingerprint = self._plan_fingerprint(plan)
            if accepted_plan_fingerprints.get(plan_fingerprint, 0) >= 2:
                question = (
                    "Planner 重复生成了已经执行过的计划，继续执行不会产生新证据。"
                    "请补充信息或提供其他公开岗位来源。"
                )
                run_repository.finish_run(
                    db,
                    run,
                    status=RunStatus.waiting_user,
                    final_summary=question,
                )
                run_repository.append_event(
                    db,
                    run_id=run.id,
                    event_type="replan_duplicate_guard",
                    payload_json={
                        "reason_code": "repeated_plan_fingerprint",
                        **build_terminal_contract(
                            error_code="repeated_plan_fingerprint",
                            source_role="planner",
                            phase="planning",
                        ).as_payload(),
                    },
                )
                return AgentRunResult(run.id, RunStatus.waiting_user, question, None)
            accepted_plan_fingerprints[plan_fingerprint] = (
                accepted_plan_fingerprints.get(plan_fingerprint, 0) + 1
            )
            revision += 1
            run_repository.set_run_complexity(db, run, plan.complexity)
            persisted_plan = run_repository.create_plan(
                db,
                run_id=run.id,
                revision=revision,
                complexity=plan.complexity,
                plan_json=plan.model_dump(mode="json"),
            )
            run_repository.append_event(
                db,
                run_id=run.id,
                event_type="plan_created",
                payload_json={
                    "revision": revision,
                    "complexity": plan.complexity.value,
                    "step_count": len(plan.steps),
                },
            )
            self._checkpoint(db)
            final_summary: str | None = None
            replan_feedback: str | None = None
            step_outputs: dict[str, list[dict[str, str]]] = {}
            for sequence, plan_step in enumerate(plan.steps, start=1):
                step = run_repository.create_step(
                    db,
                    run_id=run.id,
                    plan_id=persisted_plan.id,
                    sequence=sequence,
                    objective=plan_step.objective,
                    allowed_skills=plan_step.allowed_skills,
                )
                self._checkpoint(db)
                if sequence == 1 and revision == 1:
                    self._persist_seeded_public_evidence(
                        db=db,
                        run_id=run.id,
                        step=step,
                        task=planning_task,
                    )
                try:
                    port_error = self._skills.validate_step_ports(plan_step)
                    if port_error:
                        raise StepDependencyError(port_error)
                    step_task, step_context = self._prepare_step_inputs(
                        task=planning_task,
                        context=context,
                        plan_step=plan_step,
                        step_outputs=step_outputs,
                    )
                except StepDependencyError as error:
                    run_repository.append_event(
                        db,
                        run_id=run.id,
                        event_type="step_dependency_gate_failed",
                        payload_json={
                            "sequence": sequence,
                            "step_id": plan_step.step_id,
                            "depends_on": plan_step.depends_on,
                            "inputs": [
                                input_ref.model_dump(mode="json")
                                for input_ref in plan_step.inputs
                            ],
                            "error": str(error),
                        },
                    )
                    outcome = self._request_replan(
                        db,
                        run.id,
                        step,
                        feedback=str(error),
                        summary=str(error),
                        output_artifact_refs=[],
                        reason=ReplanReason.DEPENDENCY_UNAVAILABLE,
                    )
                else:
                    outcome = self._run_step(
                        db=db,
                        run_id=run.id,
                        task=step_task,
                        plan=plan,
                        plan_step=plan_step,
                        persisted_step=step,
                        context=step_context,
                        trace=trace,
                        tool_budget=tool_budget,
                        turn_budget=turn_budget,
                        model_budget=model_budget,
                        deadline=deadline,
                        replans=replans,
                    )
                if outcome.error_code == _TERMINAL_NEGATIVE_DISCOVERY_CODE:
                    tagged_outputs = self._tag_step_outputs(
                        plan_step, list(step.output_artifact_refs_json or [])
                    )
                    step.output_artifact_refs_json = tagged_outputs
                    final_summary = outcome.summary
                    run_repository.finish_run(
                        db,
                        run,
                        status=RunStatus.succeeded,
                        final_summary=final_summary,
                    )
                    run_repository.append_event(
                        db,
                        run_id=run.id,
                        event_type="run_succeeded",
                        payload_json={
                            "summary": final_summary,
                            "reason": "terminal_negative_discovery_succeeded",
                        },
                    )
                    return AgentRunResult(
                        run.id, RunStatus.succeeded, final_summary
                    )
                if outcome.error_code == "replan_required":
                    replan_feedback = outcome.summary
                    replan_reason = outcome.replan_reason or ReplanReason.VERIFIER_REPLAN
                    break
                if outcome.status is not RunStatus.running:
                    return outcome
                tagged_outputs = self._tag_step_outputs(
                    plan_step, list(step.output_artifact_refs_json or [])
                )
                step.output_artifact_refs_json = tagged_outputs
                step_outputs[plan_step.step_id] = tagged_outputs
                db.flush()
                final_summary = outcome.summary or final_summary
                blocked_domains = context.metadata.get("blocked_public_domains", [])
                search_hashes = context.metadata.get("public_search_query_hashes", [])
                if (
                    isinstance(blocked_domains, list) and blocked_domains
                ) or (
                    isinstance(search_hashes, list) and search_hashes
                ):
                    planning_context = dict(planning_task.context)
                    if isinstance(blocked_domains, list) and blocked_domains:
                        planning_context["blocked_public_domains"] = sorted(
                            domain for domain in blocked_domains if isinstance(domain, str)
                        )
                    if isinstance(search_hashes, list) and search_hashes:
                        planning_context["public_search_query_hashes"] = sorted(
                            value for value in search_hashes if isinstance(value, str)
                        )
                    planning_task = planning_task.model_copy(
                        update={"context": planning_context}
                    )
                planning_task = self._with_observed_public_evidence(
                    db, planning_task, run.id
                )
                context = self._tool_context(
                    user_id=user_id, run_id=run.id, task=planning_task, db=db
                )
            if replan_feedback is not None:
                replans += 1
                if replans > task.budget.max_replans:
                    question = (
                        replan_feedback
                        or "自动重规划次数已用尽，当前结果仍缺少可核验证据。"
                        "请补充新的公开岗位来源或缺失条件后重试。"
                    )
                    terminal_contract = build_terminal_contract(
                        error_code="replan_budget_exhausted",
                        source_role="runtime",
                        phase="planning",
                    )
                    run_repository.finish_run(
                        db,
                        run,
                        status=RunStatus.waiting_user,
                        final_summary=question,
                        error_code="replan_budget_exhausted",
                    )
                    run_repository.append_event(
                        db,
                        run_id=run.id,
                        event_type="run_needs_user",
                        payload_json={
                            "question": question,
                            **terminal_contract.as_payload(),
                        },
                    )
                    return AgentRunResult(
                        run.id,
                        RunStatus.waiting_user,
                        question,
                        "replan_budget_exhausted",
                    )
                feedback_context = dict(planning_task.context)
                feedback = list(feedback_context.get("verifier_feedback", []))
                feedback.append(replan_feedback)
                feedback_context["verifier_feedback"] = feedback
                next_replan_state = planning_task.replan_state.requested(
                    reason=replan_reason,
                    feedback=replan_feedback,
                    source_revision=revision,
                    count=replans,
                )
                feedback_context["replan_state"] = next_replan_state.model_dump(
                    mode="json"
                )
                planning_task = task.model_copy(
                    update={"context": feedback_context, "replan_state": next_replan_state}
                )
                replan_source = (plan, replan_feedback)
                continue
            run_repository.finish_run(
                db, run, status=RunStatus.succeeded, final_summary=final_summary
            )
            run_repository.append_event(
                db,
                run_id=run.id,
                event_type="run_succeeded",
                payload_json={"summary": final_summary},
            )
            return AgentRunResult(run.id, RunStatus.succeeded, final_summary)

    def create_queued_run(
        self, db: Session, *, user_id: str, task: AgentTaskRequest
    ) -> AgentRun:
        """Persist a run before scheduling it, so SSE has a durable target immediately."""
        return run_repository.create_run(
            db,
            user_id=user_id,
            goal=task.goal,
            allowed_skills=task.allowed_skills,
            context_summary=task.context,
            budget_json=task.budget.model_dump(mode="json"),
            agent_version=self._agent_version,
        )

    def resume(
        self,
        db: Session,
        *,
        user_id: str,
        run_id: str,
        task: AgentTaskRequest,
    ) -> AgentRunResult:
        """Continue a paused Run without resetting its durable operational budget."""
        run = run_repository.get_run_for_owner(
            db, run_id, user_id, for_update=True
        )
        if run is None:
            raise ValueError("agent_run_not_found")
        if run.status is not RunStatus.waiting_user:
            raise ValueError("agent_run_not_waiting_user")
        run_repository.start_run(db, run)
        run_repository.append_event(
            db,
            run_id=run.id,
            event_type="run_resumed",
            payload_json={"user_response_received": True},
        )
        return self.run(db, user_id=user_id, task=task, existing_run=run)

    def recover(
        self,
        db: Session,
        *,
        user_id: str,
        run_id: str,
        task: AgentTaskRequest,
    ) -> AgentRunResult:
        """Replan a process-interrupted running Run from committed evidence only."""
        run = run_repository.get_run_for_owner(
            db, run_id, user_id, for_update=True
        )
        if run is None:
            raise ValueError("agent_run_not_found")
        if run.status is not RunStatus.running:
            raise ValueError("agent_run_not_running")
        run_repository.append_event(
            db,
            run_id=run.id,
            event_type="run_recovery_started",
            payload_json={"strategy": "replan_from_durable_evidence"},
        )
        self._checkpoint(db)
        return self.run(db, user_id=user_id, task=task, existing_run=run)

    def _run_step(
        self,
        *,
        db: Session,
        run_id: str,
        task: AgentTaskRequest,
        plan: ExecutionPlan,
        plan_step: PlanStep,
        persisted_step: AgentStep,
        context: ToolContext,
        trace,
        tool_budget: ToolCallBudget,
        turn_budget: AgentTurnBudget,
        model_budget: ModelCallBudget | None = None,
        deadline: float | None = None,
        replans: int = 0,
    ) -> AgentRunResult:
        """Execute and conditionally verify one agent-defined planned outcome."""
        retries = 0
        if model_budget is None:
            model_budget = ModelCallBudget(
                task.budget.max_model_requests,
                task.budget.max_input_tokens,
                task.budget.max_output_tokens,
            )
        execution_task = task
        prior_observations = []
        prior_artifact_refs: list[dict[str, str]] = []
        last_retry_progress_fingerprint: str | None = None
        auto_extract_attempted = False
        auto_discovery_attempted = False
        seeded_artifact_refs = [
            *self._step_seeded_artifact_refs(
                db=db, run_id=run_id, step=persisted_step
            ),
            *self._resolved_step_artifact_refs(context),
        ]
        while True:
            try:
                execution = self._executor.run(
                    task=execution_task,
                    plan=plan,
                    step=plan_step,
                    context=context,
                    trace=trace,
                    tool_budget=tool_budget,
                    turn_budget=turn_budget,
                    model_budget=model_budget,
                    deadline=deadline,
                    prior_observations=prior_observations,
                )
            except AgentModelGatewayError as error:
                if error.code == "invalid_model_response":
                    # A persistently invalid model completion is a transport
                    # degradation, not a business failure: wait for a human
                    # retry instead of failing the whole run.
                    return self._wait_for_user(
                        db,
                        run_id,
                        persisted_step,
                        "模型输出格式异常，无法继续执行该步骤。请补充信息或重试。",
                    )
                return self._fail_step(db, run_id, persisted_step, error.code)
            self._record_failed_executor_observations(
                db, run_id, persisted_step, execution
            )
            observed_artifact_refs = [
                *seeded_artifact_refs,
                *self._persist_observed_evidence(
                    db, run_id, persisted_step, execution
                ),
            ]
            auto_discovery_observations: list[Any] = []
            auto_discovery_refs: list[dict[str, str]] = []
            if not auto_discovery_attempted:
                auto_discovery_observations, auto_discovery_refs = (
                    self._auto_recover_discovery_evidence(
                        db=db,
                        run_id=run_id,
                        task=task,
                        plan_step=plan_step,
                        persisted_step=persisted_step,
                        context=context,
                        observations=execution.observations,
                        artifact_refs=observed_artifact_refs,
                        tool_budget=tool_budget,
                    )
                )
                if auto_discovery_observations or auto_discovery_refs:
                    auto_discovery_attempted = True
            if auto_extract_attempted:
                auto_observations, auto_artifact_refs = [], []
            else:
                auto_observations, auto_artifact_refs = self._auto_extract_jd_details(
                    db=db,
                    run_id=run_id,
                    task=task,
                    plan_step=plan_step,
                    persisted_step=persisted_step,
                    context=context,
                    artifact_refs=[
                        *prior_artifact_refs,
                        *observed_artifact_refs,
                        *auto_discovery_refs,
                    ],
                    tool_budget=tool_budget,
                )
                if auto_observations or auto_artifact_refs:
                    auto_extract_attempted = True
            auto_observations = [
                *auto_discovery_observations,
                *auto_observations,
            ]
            auto_artifact_refs = [
                *auto_discovery_refs,
                *auto_artifact_refs,
            ]
            deliverable_observations, deliverable_refs = (
                self._auto_build_role_deliverable(
                    db=db,
                    run_id=run_id,
                    task=task,
                    plan_step=plan_step,
                    persisted_step=persisted_step,
                    context=context,
                    artifact_refs=[
                        *prior_artifact_refs,
                        *observed_artifact_refs,
                        *auto_artifact_refs,
                    ],
                    tool_budget=tool_budget,
                )
            )
            auto_observations.extend(deliverable_observations)
            auto_artifact_refs.extend(deliverable_refs)
            if auto_observations:
                execution = execution.model_copy(
                    update={
                        "observations": [*execution.observations, *auto_observations],
                    }
                )
            observed_artifact_refs.extend(auto_artifact_refs)
            # A verifier retry continues the same planned outcome. Keep prior
            # tool-backed observations for independent verification, but persist
            # only the new observation set from this Executor invocation.
            execution = execution.model_copy(
                update={
                    "observations": [*prior_observations, *execution.observations],
                    "artifact_refs": [*prior_artifact_refs, *observed_artifact_refs],
                }
            )
            if self._is_complete_official_negative_discovery(
                task, plan, plan_step, execution
            ) and not self._step_has_blocked_evidence(
                plan_step, execution.observations, execution.artifact_refs
            ):
                summary = self._official_negative_discovery_summary(
                    task, execution
                )
                completed_execution = execution.model_copy(
                    update={
                        "status": "succeeded",
                        "error_code": None,
                        "summary": summary,
                    }
                )
                self._rescue_step_succeeded(
                    db,
                    run_id,
                    persisted_step,
                    completed_execution,
                    event_type="terminal_negative_discovery_succeeded",
                    reason="complete_official_source_scan_zero_match",
                )
                return AgentRunResult(
                    run_id,
                    RunStatus.running,
                    summary,
                    _TERMINAL_NEGATIVE_DISCOVERY_CODE,
                )
            if (
                execution.status == "failed"
                and auto_artifact_refs
                and self._step_contract_met(
                    plan_step, execution.observations, execution.artifact_refs
                )
                and not self._step_has_blocked_evidence(
                    plan_step, execution.observations, execution.artifact_refs
                )
            ):
                # A deterministic runtime recovery may finish the declared
                # contract even when the model's terminal response exhausted
                # its budget. Preserve the tool-backed deliverable and avoid
                # discarding it as a model transport failure.
                execution = execution.model_copy(
                    update={
                        "status": "succeeded",
                        "error_code": None,
                        "summary": execution.summary
                        or "已由确定性工具恢复并完成步骤交付。",
                    }
                )
            if any(
                observation.status == "failed"
                and observation.error_code == "tool_skill_forbidden"
                for observation in execution.observations
            ):
                return self._request_replan(
                    db,
                    run_id,
                    persisted_step,
                    feedback=(
                        "当前步骤的 Skill 范围无法执行模型选择的工具；"
                        "请将计划拆分为每步一个 Skill 后重试。"
                    ),
                    summary="步骤 Skill 范围冲突，已停止重复工具调用并请求重规划。",
                    output_artifact_refs=execution.artifact_refs,
                    reason=ReplanReason.TOOL_SCOPE_CONFLICT,
                )
            if execution.status == "needs_user":
                if (
                    self._intermediate_routing_contract_met(
                        plan, plan_step, execution.artifact_refs
                    )
                ):
                    # The current step owns only the company/source routing
                    # artifact. A model may overreach into the downstream JD
                    # fetch and then ask the human about that later step. Once
                    # the declared routing output is tool-backed, preserve it
                    # and let the planned fetch step own any access block.
                    return self._rescue_step_succeeded(
                        db,
                        run_id,
                        persisted_step,
                        execution,
                        event_type="executor_rescue_succeeded",
                        reason="intermediate_routing_contract_met",
                    )
                if (
                    any(
                        observation.error_code == "target_role_mismatch"
                        for observation in execution.observations
                    )
                    and replans < task.budget.max_replans
                ):
                    return self._request_replan(
                        db,
                        run_id,
                        persisted_step,
                        feedback=(
                            "目标 JD 与用户请求的岗位角色不匹配；请先通过已处理候选 URL 或公开搜索"
                            "发现角色匹配的 JD，再执行后续职业交付。"
                        ),
                        summary="目标岗位角色不匹配，已请求重新发现匹配证据。",
                        output_artifact_refs=execution.artifact_refs,
                        reason=ReplanReason.DEPENDENCY_UNAVAILABLE,
                    )
                if (
                    self._step_contract_met(
                        plan_step, execution.observations, execution.artifact_refs
                    )
                    and not self._step_has_blocked_evidence(
                        plan_step, execution.observations, execution.artifact_refs
                    )
                ):
                    # The executor asked the human even though the step's
                    # deliverable is already tool-backed (post-deliverable
                    # stall pattern). The request is a hand-off the agents
                    # already finished; terminate the step as succeeded with
                    # an explicit rescue event instead of a spurious
                    # waiting_user. Blocked evidence (login/captcha/anti-bot/
                    # OCR-off) always keeps the human hand-off.
                    return self._rescue_step_succeeded(
                        db,
                        run_id,
                        persisted_step,
                        execution,
                        event_type="executor_rescue_succeeded",
                        reason="needs_user_deliverable_persisted",
                    )
                if (
                    execution.error_code == "deep_executor_invalid_response"
                    and not self._step_has_blocked_evidence(
                        plan_step, execution.observations, execution.artifact_refs
                    )
                    and replans < task.budget.max_replans
                    and not task.replan_state.conversion_used(
                        ReplanReason.RETRY_CONTRACT_EXHAUSTED
                    )
                ):
                    # The Deep Executor produced no parseable terminal and the
                    # step's deliverable contract is still unmet. Convert to a
                    # bounded REPLAN (once per run, guarded by the replan
                    # budget and the shared retry-exhaustion marker) so the
                    # planner can restructure the step instead of ending at a
                    # human hand-off for a purely mechanical parse failure.
                    return self._request_replan(
                        db,
                        run_id,
                        persisted_step,
                        feedback=(
                            "执行器未输出可解析的完成终态且交付契约未满足；"
                            "请重组该步骤、缩小范围或更换交付路线。"
                        ),
                        summary=f"执行器终态不可解析 {_RETRY_REPLAN_MARKER}",
                        output_artifact_refs=execution.artifact_refs,
                        reason=ReplanReason.RETRY_CONTRACT_EXHAUSTED,
                    )
                return self._wait_for_user(
                    db,
                    run_id,
                    persisted_step,
                    execution.user_question,
                    output_artifact_refs=observed_artifact_refs,
                    terminal_contract=build_terminal_contract(
                        observations=execution.observations,
                        source_role="executor",
                        phase="execution",
                        artifact_count=len(observed_artifact_refs),
                    ),
                )
            if execution.error_code == "wall_clock_budget_exhausted":
                if self._intermediate_routing_contract_met(
                    plan, plan_step, execution.artifact_refs
                ):
                    # The model timed out only after persisting the current
                    # step's bounded routing list.  That list is the complete
                    # contract for this non-final step; preserve it and let
                    # the next planned step own JD fetching instead of asking
                    # the user to resume for an unnecessary completion token.
                    return self._rescue_step_succeeded(
                        db,
                        run_id,
                        persisted_step,
                        execution,
                        event_type="executor_rescue_succeeded",
                        reason="wall_clock_intermediate_routing_contract_met",
                        output_artifact_refs=observed_artifact_refs,
                    )
                # Wall-clock exhaustion is a transport/resource pause, not a
                # business failure: resume() recomputes a fresh deadline and
                # the executor continues the remaining turns. Route to a
                # recoverable waiting_user instead of failing the run, and
                # surface the stable error_code for observability.
                return self._wait_for_user(
                    db,
                    run_id,
                    persisted_step,
                    "运行时间预算耗尽（模型响应偏慢），该步骤尚未完成。恢复运行将获得新的时间窗口继续。",
                    output_artifact_refs=observed_artifact_refs,
                    error_code="wall_clock_budget_exhausted",
                )
            if (
                execution.status == "failed"
                and execution.error_code == "deep_executor_invalid_response"
                and self._step_contract_met(
                    plan_step, execution.observations, execution.artifact_refs
                )
                and not self._step_has_blocked_evidence(
                    plan_step, execution.observations, execution.artifact_refs
                )
            ):
                return self._rescue_step_succeeded(
                    db,
                    run_id,
                    persisted_step,
                    execution,
                    event_type="executor_rescue_succeeded",
                    reason="invalid_terminal_deliverable_persisted",
                    output_artifact_refs=observed_artifact_refs,
                )
            if (
                execution.status == "succeeded"
                and self._intermediate_routing_contract_met(
                    plan, plan_step, execution.artifact_refs
                )
            ):
                # A non-final routing list is consumed and independently
                # checked by the next planned discovery step. Once the
                # deterministic source/URL artifact exists, another model
                # verification turn adds latency but no new evidence.
                return self._rescue_step_succeeded(
                    db,
                    run_id,
                    persisted_step,
                    execution,
                    event_type="executor_rescue_succeeded",
                    reason="intermediate_routing_contract_met",
                    output_artifact_refs=observed_artifact_refs,
                )
            if execution.status != "succeeded":
                return self._fail_step(
                    db,
                    run_id,
                    persisted_step,
                    execution.error_code or "executor_failed",
                    output_artifact_refs=observed_artifact_refs,
                )
            if not self._requires_verification(plan, plan_step):
                if self._completion_gate_rejected(
                    plan_step,
                    execution.observations,
                    summary=execution.summary,
                    artifact_refs=execution.artifact_refs,
                ):
                    if (
                        not self._step_has_blocked_evidence(
                            plan_step, execution.observations, execution.artifact_refs
                        )
                        and replans < task.budget.max_replans
                        and not task.replan_state.conversion_used(
                            ReplanReason.RETRY_CONTRACT_EXHAUSTED
                        )
                    ):
                        # The executor declared success but the deterministic
                        # completion gate rejected the deliverable. Without
                        # blocked evidence the human cannot fix what a
                        # restructured step can: convert to a bounded REPLAN
                        # (once per run, guarded by the replan budget); a
                        # repeated rejection or any blocked evidence keeps
                        # the human hand-off.
                        return self._request_replan(
                            db,
                            run_id,
                            persisted_step,
                            feedback=(
                                "交付物未通过完成门禁：工具未产生可核验的交付物，"
                                "请重组该步骤以产生满足契约的主 artifact。"
                            ),
                            summary=(
                                f"交付物未通过完成门禁 {_RETRY_REPLAN_MARKER}"
                            ),
                            output_artifact_refs=execution.artifact_refs,
                            reason=ReplanReason.RETRY_CONTRACT_EXHAUSTED,
                        )
                    return self._wait_for_user(
                        db,
                        run_id,
                        persisted_step,
                        "工具未产生可核验的交付物，当前总结不能视为完成。"
                        "请提供可公开访问的岗位页面或补充必要信息后重试。",
                        output_artifact_refs=observed_artifact_refs,
                    )
                run_repository.finish_step(
                    db,
                    persisted_step,
                    status=StepStatus.succeeded,
                    output_artifact_refs=execution.artifact_refs,
                )
                run_repository.append_event(
                    db,
                    run_id=run_id,
                    event_type="step_succeeded",
                    payload_json={"sequence": persisted_step.sequence},
                )
                return AgentRunResult(run_id, RunStatus.running, execution.summary)
            seeded_refs = self._step_seeded_artifact_refs(
                db=db, run_id=run_id, step=persisted_step
            )
            verdict_refs = list(
                {
                    (ref.get("artifact_id"), ref.get("content_hash")): ref
                    for ref in [*execution.artifact_refs, *seeded_refs]
                    if isinstance(ref, dict)
                }.values()
            )
            verification = evaluate_completion_gate(
                skills=self._skills,
                step=plan_step,
                observations=execution.observations,
                artifact_refs=verdict_refs,
                summary=execution.summary,
                deadline=deadline,
            )
            if verification.error_code == "wall_clock_budget_exhausted":
                if (
                    self._step_contract_met(
                        plan_step, execution.observations, execution.artifact_refs
                    )
                    and not self._step_has_blocked_evidence(
                        plan_step, execution.observations, execution.artifact_refs
                    )
                ):
                    # The deadline expired only at the model verification
                    # boundary. If the same deterministic evidence contract
                    # is already satisfied, preserve the completed artifact
                    # instead of asking the user to resume solely for a PASS
                    # token. Any blocked or incomplete evidence still pauses.
                    return self._rescue_step_succeeded(
                        db,
                        run_id,
                        persisted_step,
                        execution,
                        event_type="verifier_rescue_succeeded",
                        reason="wall_clock_budget_contract_met",
                    )
                # Wall-clock exhaustion before a verification decision is a
                # transport/resource pause: the step's executor work is already
                # persisted, and resume() recomputes a fresh deadline so the
                # verifier can run its remaining calls. Route to a recoverable
                # waiting_user instead of failing the run, surfacing the stable
                # error_code for observability.
                return self._wait_for_user(
                    db,
                    run_id,
                    persisted_step,
                    "运行时间预算耗尽（模型响应偏慢），步骤产出待核验。恢复运行将获得新的时间窗口完成核验。",
                    output_artifact_refs=execution.artifact_refs,
                    error_code="wall_clock_budget_exhausted",
                )
            if verification.error_code:
                return self._fail_step(
                    db,
                    run_id,
                    persisted_step,
                    verification.error_code,
                    output_artifact_refs=execution.artifact_refs,
                )
            if verification.decision is VerificationDecision.PASS:
                diagnostics = self._skills.completion_evidence_diagnostics(
                    plan_step,
                    execution.observations,
                    summary=execution.summary,
                    artifact_refs=execution.artifact_refs,
                )

                if not diagnostics["gate_passed"]:
                    durable_refs = self._step_seeded_artifact_refs(
                        db=db, run_id=run_id, step=persisted_step
                    )
                    if durable_refs:
                        merged_refs = list(
                            {
                                (ref.get("artifact_id"), ref.get("content_hash")): ref
                                for ref in [*execution.artifact_refs, *durable_refs]
                            }.values()
                        )
                        durable_diagnostics = self._skills.completion_evidence_diagnostics(
                            plan_step,
                            execution.observations,
                            summary=execution.summary,
                            artifact_refs=merged_refs,
                        )
                        if durable_diagnostics["gate_passed"]:
                            execution = execution.model_copy(
                                update={"artifact_refs": merged_refs}
                            )
                            return self._rescue_step_succeeded(
                                db,
                                run_id,
                                persisted_step,
                                execution,
                                event_type="runtime_durable_contract_rescue",
                                reason="persisted_evidence_contract_met",
                                output_artifact_refs=merged_refs,
                            )
                    run_repository.append_event(
                        db,
                        run_id=run_id,
                        event_type="verification_pass_rejected_by_contract",
                        payload_json={
                            "sequence": persisted_step.sequence,
                            "reason": "deterministic_contract_not_satisfied",
                            "evidence_diagnostics": diagnostics,
                        },
                    )
                    return self._wait_for_user(
                        db,
                        run_id,
                        persisted_step,
                        "Verifier 的 PASS 未通过 Skill 的确定性交付契约，不能将该步骤标记为完成。"
                        "请补充工具产出或人工确认后重试。",
                        output_artifact_refs=execution.artifact_refs,
                        terminal_contract=build_terminal_contract(
                            error_code=(
                                "domain_temporarily_blocked"
                                if self._run_has_external_fetch_block(db, run_id)
                                else "verification_failed"
                            ),
                            observations=execution.observations,
                            source_role="runtime",
                            phase="verification",
                            contract_met=bool(diagnostics["contract_met"]),
                            artifact_count=len(execution.artifact_refs),
                            evidence_diagnostics=diagnostics,
                        ),
                    )
                run_repository.finish_step(
                    db,
                    persisted_step,
                    status=StepStatus.succeeded,
                    output_artifact_refs=execution.artifact_refs,
                )
                run_repository.append_event(
                    db,
                    run_id=run_id,
                    event_type="step_succeeded",
                    payload_json={"sequence": persisted_step.sequence},
                )
                run_repository.append_event(
                    db,
                    run_id=run_id,
                    event_type="verification_passed",
                    payload_json={"sequence": persisted_step.sequence},
                )
                return AgentRunResult(run_id, RunStatus.running, execution.summary)
            if verification.decision is VerificationDecision.RETRY_EXECUTOR:
                progress_fingerprint = _execution_progress_fingerprint(execution)
                if (
                    last_retry_progress_fingerprint is not None
                    and progress_fingerprint == last_retry_progress_fingerprint
                ):
                    feedback = verification.feedback or ""
                    return self._wait_for_user(
                        db,
                        run_id,
                        persisted_step,
                        "Verifier 重试没有产生新的工具证据，继续调用将重复当前失败路径。"
                        + (f"当前反馈：{feedback}。" if feedback else "")
                        + "请提供新的岗位来源或补充缺失字段后重试。",
                        output_artifact_refs=execution.artifact_refs,
                        error_code="no_progress_duplicate",
                        terminal_contract=build_terminal_contract(
                            error_code="no_progress_duplicate",
                            source_role="verifier",
                            phase="verification",
                            artifact_count=len(execution.artifact_refs),
                        ),
                    )
                last_retry_progress_fingerprint = progress_fingerprint
                retries += 1
                run_repository.append_event(
                    db,
                    run_id=run_id,
                    event_type="verification_retry_executor",
                    payload_json={
                        "sequence": persisted_step.sequence,
                        "feedback": verification.feedback,
                    },
                )
                if (
                    self._step_has_blocked_evidence(
                        plan_step, execution.observations, execution.artifact_refs
                    )
                    and not self._step_contract_met(
                        plan_step, execution.observations, execution.artifact_refs
                    )
                ):
                    # The step's evidence is blocked (login/captcha/anti-bot/
                    # OCR-off or a deterministic per-URL failure) and the
                    # deliverable cannot be satisfied from unblocked evidence.
                    # Re-invoking the executor would only re-burn turns on the
                    # same blocked calls, so downgrade the retry loop to one
                    # clean human hand-off - the human stays in the loop and
                    # resume() remains available after they act.
                    run_repository.append_event(
                        db,
                        run_id=run_id,
                        event_type="verification_retry_downgraded",
                        payload_json={
                            "sequence": persisted_step.sequence,
                            "reason": "blocked_evidence",
                            "feedback": verification.feedback,
                        },
                    )
                    return self._wait_for_user(
                        db,
                        run_id,
                        persisted_step,
                        "步骤证据被访问限制阻断（登录/验证码/反爬/OCR 未启用等），"
                        "自动重试无法取得新证据："
                        + verification.feedback
                        + "。请人工确认该来源的岗位信息，或提供可公开访问的页面后重试。",
                        output_artifact_refs=execution.artifact_refs,
                        terminal_contract=build_terminal_contract(
                            observations=execution.observations,
                            source_role="verifier",
                            phase="verification",
                            artifact_count=len(execution.artifact_refs),
                        ),
                    )
                if any(
                    observation.status == "failed"
                    and observation.error_code
                    in {"tool_skill_forbidden", "unknown_tool"}
                    for observation in execution.observations
                ):
                    # R013: the verifier RETRY demands a tool-backed deliverable
                    # that this step's skill scope permanently excludes (the
                    # call was rejected as tool_skill_forbidden / unknown_tool).
                    # A same-step re-invocation is provably unsatisfiable - the
                    # executor cannot call the scoped-out tool - so it would
                    # only re-burn turns on the same rejected call. Route to the
                    # replan path so the planner can restructure the step
                    # instead of looping on an impossible retry.
                    return self._request_replan(
                        db,
                        run_id,
                        persisted_step,
                        feedback=verification.feedback,
                        summary=verification.feedback,
                        output_artifact_refs=execution.artifact_refs,
                        reason=ReplanReason.TOOL_SCOPE_CONFLICT,
                    )
                if retries <= task.budget.max_replans:
                    prior_observations = execution.observations
                    prior_artifact_refs = execution.artifact_refs
                    retry_context = dict(execution_task.context)
                    feedback = list(retry_context.get("verifier_feedback", []))
                    # The gate always emits non-empty feedback, so the False
                    # branch here is unreachable.
                    if verification.feedback:  # pragma: no cover
                        feedback.append(verification.feedback)
                    retry_context["verifier_feedback"] = feedback
                    execution_task = execution_task.model_copy(
                        update={
                            "context": retry_context,
                            "execution_state": execution.execution_state,
                        }
                    )
                    execution_task = self._with_observed_public_evidence(
                        db, execution_task, run_id
                    )
                    context = self._tool_context(
                        user_id=context.user_id,
                        run_id=run_id,
                        task=execution_task,
                        db=db,
                    )
                    continue
                # Same-step retries cannot satisfy the verifier: more retries
                # would only burn turns on a stuck loop. When the step's
                # deterministic contract is already tool-backed and unblocked
                # and the replan budget still allows a restructure, route to a
                # bounded REPLAN (N3) so the planner can rebuild the step
                # instead of ending the run at a human hand-off the agents had
                # already produced. Guarded by the replan budget and a
                # once-per-run marker exactly like the NEED_USER conversion
                # (R009/R018/R033): a second identical exhaustion after the
                # replan keeps the human hand-off. Blocked evidence always
                # keeps the human hand-off.
                if (
                    self._step_contract_met(
                        plan_step, execution.observations, execution.artifact_refs
                    )
                    and not self._step_has_blocked_evidence(
                        plan_step, execution.observations, execution.artifact_refs
                    )
                    and replans < task.budget.max_replans
                    and not task.replan_state.conversion_used(
                        ReplanReason.RETRY_CONTRACT_EXHAUSTED
                    )
                ):
                    return self._request_replan(
                        db,
                        run_id,
                        persisted_step,
                        feedback=verification.feedback,
                        summary=f"{verification.feedback} {_RETRY_REPLAN_MARKER}",
                        output_artifact_refs=execution.artifact_refs,
                        reason=ReplanReason.RETRY_CONTRACT_EXHAUSTED,
                    )
                return self._wait_for_user(
                    db,
                    run_id,
                    persisted_step,
                    "多次重试后仍未通过核验：" + verification.feedback + "。请人工确认产出，或补充缺失信息后重试。",
                    output_artifact_refs=execution.artifact_refs,
                )
            if verification.decision is VerificationDecision.NEED_USER:
                if (
                    self._intermediate_routing_contract_met(
                        plan, plan_step, execution.artifact_refs
                    )
                ):
                    return self._rescue_step_succeeded(
                        db,
                        run_id,
                        persisted_step,
                        execution,
                        event_type="verifier_rescue_succeeded",
                        reason="intermediate_routing_contract_met",
                    )

                if (
                    self._step_contract_met(
                        plan_step, execution.observations, execution.artifact_refs
                    )
                    and not self._step_has_blocked_evidence(
                        plan_step, execution.observations, execution.artifact_refs
                    )
                    and replans < task.budget.max_replans
                    and not task.replan_state.conversion_used(
                        ReplanReason.NEED_USER_CONTRACT
                    )
                ):
                    # A NEED_USER over an already complete, unblocked Skill
                    # deliverable cannot be repaired by waiting for the human
                    # alone: the evidence is tool-backed and the contract is
                    # satisfied. Route to a bounded REPLAN so the planner can
                    # restructure the step instead of ending the run at a
                    # hand-off the agents already produced (R009/R018/R033/
                    # R013). Guarded by the replan budget and the once-per-run
                    # conversion marker: a second identical NEED_USER after
                    # the replan keeps the human hand-off. Blocked evidence
                    # and unmet contracts never convert.
                    return self._request_replan(
                        db,
                        run_id,
                        persisted_step,
                        feedback=verification.feedback,
                        summary=f"{verification.feedback} {_NEEDS_USER_REPLAN_MARKER}",
                        output_artifact_refs=execution.artifact_refs,
                        reason=ReplanReason.NEED_USER_CONTRACT,
                    )
                if (
                    self._step_contract_met(
                        plan_step, execution.observations, execution.artifact_refs
                    )
                    and not self._step_has_blocked_evidence(
                        plan_step, execution.observations, execution.artifact_refs
                    )
                    and self._is_verified_zero_match_discovery(
                        task, plan_step, execution
                    )
                ):
                    # A user asking whether a matching job exists has already
                    # received a complete answer when exhaustive, persisted
                    # public evidence proves that the match set is empty.
                    # This rescue runs only after the bounded NEED_USER replan
                    # path above is unavailable/used, so it cannot suppress a
                    # useful structural repair on the first verifier objection.
                    return self._rescue_step_succeeded(
                        db,
                        run_id,
                        persisted_step,
                        execution,
                        event_type="verifier_rescue_succeeded",
                        reason="verified_negative_discovery_contract_met",
                    )
                return self._wait_for_user(
                    db,
                    run_id,
                    persisted_step,
                    verification.feedback,
                    output_artifact_refs=execution.artifact_refs,
                    terminal_contract=build_terminal_contract(
                        observations=execution.observations,
                        source_role="verifier",
                        phase="verification",
                        artifact_count=len(execution.artifact_refs),
                    ),
                )
            if verification.decision is VerificationDecision.FAIL:
                return self._wait_for_user(
                    db,
                    run_id,
                    persisted_step,
                    "Verifier 判定当前产出不满足任务要求："
                    + (verification.feedback or "请补充岗位证据后重试。"),
                    output_artifact_refs=execution.artifact_refs,
                    error_code="verification_failed",
                    terminal_contract=build_terminal_contract(
                        error_code="verification_failed",
                        source_role="verifier",
                        phase="verification",
                        artifact_count=len(execution.artifact_refs),
                    ),
                )
            return self._request_replan(
                db,
                run_id,
                persisted_step,
                feedback=verification.feedback,
                summary=verification.feedback,
                output_artifact_refs=execution.artifact_refs,
                reason=ReplanReason.VERIFIER_REPLAN,
            )

    def _requires_verification(self, plan: ExecutionPlan, plan_step: PlanStep) -> bool:
        # Stage 1.3b: ``self._skills`` is asserted non-None in ``__init__``;
        # the legacy None branch is unreachable and was removed.
        return self._skills.requires_verification(plan_step, plan.complexity.value)

    @staticmethod
    def _is_intermediate_routing_step(
        plan: ExecutionPlan, plan_step: PlanStep
    ) -> bool:
        """True only for a non-final step whose sole output is source routing."""
        try:
            step_index = next(
                index
                for index, candidate in enumerate(plan.steps)
                if candidate.step_id == plan_step.step_id
            )
        except StopIteration:
            return False
        output_types = {
            output.artifact_type
            for output in plan_step.outputs
            if output.artifact_type
        }
        return (
            step_index < len(plan.steps) - 1
            and bool(plan_step.outputs)
            and output_types == {"job_search_results"}
        )

    @classmethod
    def _intermediate_routing_contract_met(
        cls,
        plan: ExecutionPlan,
        plan_step: PlanStep,
        artifact_refs: list[dict[str, Any]],
    ) -> bool:
        """Accept only a non-empty, URL-bearing routing artifact mid-plan.

        The job-discovery Skill's normal completion contract intentionally
        requires a captured JD page.  A non-final step that explicitly emits
        only ``job_search_results`` has a narrower responsibility: hand a
        finite set of public URLs to the following fetch step.  The runtime
        marks such refs semantic-valid while persisting the registered search
        or sheet observation; model-proposed URLs never reach this path.
        """
        if not cls._is_intermediate_routing_step(plan, plan_step):
            return False
        return any(
            ref.get("artifact_type") == "job_search_results"
            and ref.get("tool")
            in {"query-career-sheet-records", "search-public-job-pages"}
            and ref.get("semantic_valid") == "true"
            for ref in artifact_refs
            if isinstance(ref, dict)
        )

    @staticmethod
    def _prepare_step_inputs(
        *,
        task: AgentTaskRequest,
        context: ToolContext,
        plan_step: PlanStep,
        step_outputs: dict[str, list[dict[str, str]]],
    ) -> tuple[AgentTaskRequest, ToolContext]:
        """Resolve typed context/artifact refs before the Executor is called."""
        resolved_context: dict[str, Any] = {}
        resolved_artifacts: dict[str, list[dict[str, str]]] = {}
        resolved_private_inputs: set[str] = set()
        for input_ref in plan_step.inputs:
            if input_ref.kind == "context":
                value = _task_input_value(task, input_ref.name)
                if value is None:
                    raise StepDependencyError(
                        f"step {plan_step.step_id} requires context input '{input_ref.name}'"
                    )
                if _is_private_task_input(input_ref.name):
                    # The private projection is already supplied separately to
                    # the Executor. Record only that the declared input was
                    # resolved; never echo its value into task.context, which
                    # is visible in the generic model decision state.
                    resolved_private_inputs.add(input_ref.name)
                else:
                    resolved_context[input_ref.name] = value
                continue
            source = step_outputs.get(input_ref.from_step or "", [])
            if not source:
                raise StepDependencyError(
                    f"step {plan_step.step_id} requires artifact '{input_ref.name}' "
                    f"from step '{input_ref.from_step}'"
                )
            matches = [
                item
                for item in source
                if input_ref.name
                in {
                    item.get("output_name"),
                    item.get("artifact_type"),
                    item.get("tool"),
                    item.get("artifact_id"),
                }
                and (
                    input_ref.artifact_type is None
                    or item.get("artifact_type") == input_ref.artifact_type
                )
            ]
            if not matches and len(source) == 1 and input_ref.artifact_type is None:
                matches = source
            if (
                input_ref.artifact_type == "job_search_results"
                and "job-discovery" in plan_step.allowed_skills
            ):
                # A Planner may call the output of a discovery step
                # ``job_search_results`` even after the Executor has already
                # upgraded that source into public pages or structured JDs.
                # Reuse only those same-run trusted discovery artifacts; do
                # not widen arbitrary Skill or cross-domain ports.
                compatible = [
                    item
                    for item in source
                    if item.get("artifact_type")
                    in {"public_job_page", "structured_job_details"}
                ]
                if compatible:
                    matches = [*matches, *compatible]
            if not matches:
                raise StepDependencyError(
                    f"step {plan_step.step_id} cannot resolve artifact '{input_ref.name}' "
                    f"from step '{input_ref.from_step}'"
                )
            resolved_artifacts[input_ref.name] = matches
        if not resolved_context and not resolved_artifacts:
            return task, context
        input_payload = {
            "context": resolved_context,
            "artifacts": resolved_artifacts,
            "private_inputs": sorted(resolved_private_inputs),
        }
        step_task = task.model_copy(
            update={
                "context": {
                    **task.context,
                    "resolved_step_inputs": input_payload,
                }
            }
        )
        step_metadata = dict(context.metadata)
        step_metadata["resolved_step_inputs"] = input_payload
        return step_task, ToolContext(
            user_id=context.user_id,
            run_id=context.run_id,
            metadata=step_metadata,
        )

    @staticmethod
    def _tag_step_outputs(
        plan_step: PlanStep, refs: list[dict[str, Any]]
    ) -> list[dict[str, str]]:
        """Persist semantic output names alongside real artifact pointers."""
        if not plan_step.outputs:
            return refs
        tagged: list[dict[str, str]] = []
        for index, ref in enumerate(refs):
            item = {key: str(value) for key, value in ref.items() if value is not None}
            selected = next(
                (
                    output
                    for output in plan_step.outputs
                    if output.artifact_type
                    and output.artifact_type == item.get("artifact_type")
                ),
                None,
            )
            if selected is None:
                selected = plan_step.outputs[0] if len(plan_step.outputs) == 1 else plan_step.outputs[index % len(plan_step.outputs)]
            item["output_name"] = selected.name
            if selected.artifact_type and "artifact_type" not in item:
                item["artifact_type"] = selected.artifact_type
            tagged.append(item)
        return tagged

    def _step_contract_met(
        self,
        step: PlanStep,
        observations: list,
        artifact_refs: list[dict[str, Any]] | None = None,
    ) -> bool:
        # Runtime rescue/replan decisions must use the same semantic gate as
        # the final completion gate. The legacy observation-only check is
        # intentionally retained on SkillRegistry for compatibility tests,
        # but it can treat an empty report-shaped output as a deliverable.
        return bool(
            self._skills.completion_evidence_diagnostics(
                step,
                observations,
                summary="contract-check",
                artifact_refs=artifact_refs or [],
            )["contract_met"]
        )

    @staticmethod
    def _is_verified_zero_match_discovery(
        task: AgentTaskRequest,
        step: PlanStep,
        execution: ExecutorResult,
    ) -> bool:
        """Recognize an evidenced negative answer to an existence question."""
        if set(step.allowed_skills) != {"job-discovery"}:
            return False
        compact_goal = re.sub(r"\s+", "", task.goal).lower()
        if not any(
            marker in compact_goal for marker in _DISCOVERY_EXISTENCE_QUERY_MARKERS
        ):
            return False
        compact_summary = re.sub(r"\s+", "", execution.summary or "").lower()
        if not any(marker in compact_summary for marker in _VERIFIED_ZERO_MATCH_MARKERS):
            return False
        return any(
            ref.get("artifact_type") in _DISCOVERY_EVIDENCE_ARTIFACT_TYPES
            for ref in execution.artifact_refs
            if isinstance(ref, dict)
        )

    @staticmethod
    def _is_complete_official_negative_discovery(
        task: AgentTaskRequest,
        plan: ExecutionPlan,
        step: PlanStep,
        execution: ExecutorResult,
    ) -> bool:
        """Close a pure discovery plan when an official scoped scan proves zero.

        This is narrower than a generic ``search_empty``: every planned step
        must belong only to job-discovery, the task must ask an existence
        question, and the successful observation must pass the reviewed
        source-specific semantic checker. Downstream fetch/extract steps have
        no input after a verified empty set and are therefore intentionally
        not executed.
        """
        if set(step.allowed_skills) != {"job-discovery"}:
            return False
        if not plan.steps or any(
            set(candidate.allowed_skills) != {"job-discovery"}
            for candidate in plan.steps
        ):
            return False
        compact_goal = re.sub(r"\s+", "", task.goal).lower()
        if not any(
            marker in compact_goal for marker in _DISCOVERY_EXISTENCE_QUERY_MARKERS
        ):
            return False
        return any(
            observation.tool_name == "search-public-job-pages"
            and observation.status == "succeeded"
            and skill_observation_is_semantically_valid(
                observation.tool_name, observation.output
            )
            for observation in execution.observations
        )

    @staticmethod
    def _official_negative_discovery_summary(
        task: AgentTaskRequest, execution: ExecutorResult
    ) -> str:
        output = next(
            (
                observation.output
                for observation in execution.observations
                if observation.tool_name == "search-public-job-pages"
                and observation.status == "succeeded"
                and skill_observation_is_semantically_valid(
                    observation.tool_name, observation.output
                )
            ),
            {},
        )
        recent_days = output.get("time_window_days") if isinstance(output, dict) else None
        window_text = f"最近{recent_days}天" if isinstance(recent_days, int) else "指定时间窗"
        source_name = (
            "稀土掘金"
            if isinstance(output, dict) and output.get("source_scope") == "juejin.cn"
            else "指定公开来源"
        )
        return (
            f"已完成{source_name}{window_text}官方公开搜索的完整分页核验；"
            f"未找到满足用户硬约束的招聘帖。任务原始范围：{task.goal}"
        )

    def _has_blocked_evidence(self, observations: list) -> bool:
        return self._skills.has_blocked_evidence(observations)

    def _step_has_blocked_evidence(
        self,
        step: PlanStep,
        observations: list,
        artifact_refs: list[dict[str, Any]],
    ) -> bool:
        return bool(
            self._skills.completion_evidence_diagnostics(
                step,
                observations,
                summary="contract-check",
                artifact_refs=artifact_refs,
            )["blocked"]
        )

    @staticmethod
    def _run_has_external_fetch_block(db: Session, run_id: str) -> bool:
        """Preserve an earlier source-access block across a bounded replan."""
        events = db.scalars(
            select(AgentEvent).where(AgentEvent.run_id == run_id)
        )
        for event in events:
            payload = event.payload_json or {}
            if isinstance(payload, dict) and _is_external_runtime_code(
                payload.get("error_code")
            ):
                return True
        return False

    def _completion_gate_rejected(
        self,
        step: PlanStep,
        observations: list,
        *,
        summary: str | None,
        artifact_refs: list[dict[str, Any]] | None = None,
    ) -> bool:
        return self._skills.has_completion_contract(step) and not self._skills.completion_evidence_gate(
            step,
            observations,
            summary=summary,
            artifact_refs=artifact_refs or (),
        )

    @staticmethod
    def _plans_isomorphic(plan_a: ExecutionPlan, plan_b: ExecutionPlan) -> bool:
        """True when two plans carry the same normalized step sequence.

        Steps are compared by their sorted allowed-skill sets and their
        whitespace-normalized objectives, so formatting differences never
        mask a structurally repeated plan (N4 guard).
        """
        return _normalize_plan_steps(plan_a) == _normalize_plan_steps(plan_b)

    @staticmethod
    def _plan_fingerprint(plan: ExecutionPlan) -> str:
        """Hash the semantic plan shape before it is persisted or executed."""
        payload = {
            "complexity": plan.complexity.value,
            "success_criteria": sorted(" ".join(item.split()) for item in plan.success_criteria),
            "steps": [
                {
                    "objective": " ".join(step.objective.split()),
                    "allowed_skills": sorted(step.allowed_skills),
                    "requires_verification": step.requires_verification,
                    "depends_on": sorted(step.depends_on),
                    "inputs": [item.model_dump(mode="json") for item in step.inputs],
                    "outputs": [item.model_dump(mode="json") for item in step.outputs],
                }
                for step in plan.steps
            ],
        }
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    @staticmethod
    def _plan_fingerprint_json(plan_json: object) -> str:
        """Hash an already persisted plan without trusting mutable model text."""
        if not isinstance(plan_json, dict):
            return hashlib.sha256(b"invalid-plan-json").hexdigest()
        raw_steps = plan_json.get("steps")
        payload = {
            "complexity": plan_json.get("complexity"),
            "success_criteria": sorted(
                " ".join(str(item).split())
                for item in plan_json.get("success_criteria", [])
                if isinstance(item, str)
            ),
            "steps": [
                {
                    "objective": " ".join(str(step.get("objective", "")).split()),
                    "allowed_skills": sorted(step.get("allowed_skills", [])),
                    "requires_verification": bool(step.get("requires_verification", False)),
                    "depends_on": sorted(step.get("depends_on", [])),
                    "inputs": step.get("inputs", []),
                    "outputs": step.get("outputs", []),
                }
                for step in raw_steps
                if isinstance(step, dict)
            ] if isinstance(raw_steps, list) else [],
        }
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    @staticmethod
    def _build_decision_trace(
        db: Session,
        run_id: str,
        *,
        initial_turn_indices: dict[AgentRole, int] | None = None,
    ):
        """Return a run-local, role-indexed callback for safe decision summaries."""
        turn_indices = dict(initial_turn_indices or {role: 0 for role in AgentRole})

        def trace(
            role: AgentRole,
            decision_json: dict[str, object],
            turn_metadata: dict[str, object] | None = None,
        ) -> None:
            turn_indices[role] += 1
            model_name = None
            input_tokens = None
            output_tokens = None
            context_manifest = None
            if turn_metadata is not None and isinstance(turn_metadata, dict):
                model_name = turn_metadata.get("model_name")
                input_tokens = turn_metadata.get("input_tokens")
                output_tokens = turn_metadata.get("output_tokens")
                context_manifest = turn_metadata.get("context_manifest")
                # Preserve bounded, non-sensitive DeepExecutor diagnostics in
                # the durable turn JSON. Provider prompts and raw tool output
                # remain excluded from the trace.
                for key in ("deep_executor", "internal_model_calls"):
                    if key in turn_metadata:
                        decision_json = {
                            **decision_json,
                            key: turn_metadata[key],
                        }
            run_repository.create_turn(
                db,
                run_id=run_id,
                role=role,
                turn_index=turn_indices[role],
                decision_json=decision_json,
                model_name=model_name,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                context_manifest=context_manifest,
            )
            # A completed model decision is a recovery checkpoint. Tool output
            # remains evidence-bound and is replay-safe if a process stops before
            # the next decision persists its outcome.
            db.commit()

        return trace

    @staticmethod
    def _checkpoint(db: Session) -> None:
        """Commit a lifecycle boundary before entering an external model/tool turn."""
        db.commit()

    @staticmethod
    def _with_observed_public_evidence(
        db: Session, task: AgentTaskRequest, run_id: str
    ) -> AgentTaskRequest:
        """Expose only traceable JD pointers to later Agent turns.

        Full page bodies remain in the database and are hydrated by
        :meth:`_tool_context` immediately before a deterministic tool runs.
        Planner/Executor therefore carry stable identifiers and URLs across
        steps instead of copying the same JD text into every model request.
        """
        candidates: list[dict[str, Any]] = []
        for artifact in run_repository.list_evidence_artifacts(db, run_id):
            visible_text = artifact.content_json.get("visible_text")
            if artifact.artifact_type == "structured_job_details":
                raw_candidates = artifact.content_json.get("candidates")
                if not isinstance(raw_candidates, list) or not any(
                    isinstance(candidate, dict)
                    and not isinstance(candidate.get("evidence_refs"), list)
                    for candidate in raw_candidates
                ):
                    # A structured artifact explicitly linked to a source
                    # page is already represented by that page. Standalone
                    # structured output remains a bounded evidence item so a
                    # target JD does not disappear behind the page budget.
                    continue
                visible_text = _structured_artifact_visible_text(
                    artifact.content_json
                )
            if not isinstance(visible_text, str) or not visible_text:
                continue
            item: dict[str, Any] = {
                "artifact_id": artifact.id,
                "source_url": artifact.source_url,
                "content_hash": artifact.content_hash,
            }
            effective_url = artifact.content_json.get("effective_url")
            if isinstance(effective_url, str):
                item["effective_url"] = effective_url
            redirect_chain = artifact.content_json.get("redirect_chain")
            if isinstance(redirect_chain, list) and redirect_chain:
                item["redirect_chain"] = redirect_chain
            http_status = artifact.content_json.get("http_status")
            if isinstance(http_status, int):
                item["http_status"] = http_status
            title = artifact.content_json.get("title")
            if isinstance(title, str):
                item["title"] = title
            quality = artifact.content_json.get("quality")
            if isinstance(quality, str):
                item["quality"] = quality
            quality_signal = artifact.content_json.get("quality_signal")
            if isinstance(quality_signal, str):
                item["quality_signal"] = quality_signal
            candidates.append(item)
        context = dict(task.context)
        context["observed_public_evidence"] = candidates
        return task.model_copy(update={"context": context})

    @staticmethod
    def _tool_context(
        *, user_id: str, run_id: str, task: AgentTaskRequest, db: Session
    ) -> ToolContext:
        """Project only verified public evidence into deterministic tool authority.

        Raw page evidence flows through the bounded ``observed_public_evidence``
        context; structured job candidates are read from the run's persisted
        ``structured_job_details`` artifacts (extract outputs) so skill tools
        (e.g. ``match-observed-jobs``) rank real per-job units instead of one
        aggregated page. Candidates are tool-side authority only: they never
        enter ``task.context``, so model prompts stay unchanged.
        """
        evidence = _full_observed_public_evidence(db, run_id)
        if not evidence:
            # Compatibility for an initial in-memory tool call before its
            # first evidence artifact has been persisted.
            evidence = task.context.get("observed_public_evidence", [])
        profile_facts = task.private_context.get("confirmed_profile_facts", {})
        blocked_domains = task.context.get("blocked_public_domains", [])
        if not isinstance(blocked_domains, list):
            blocked_domains = []
        state_domains = task.execution_state.get("blocked_public_domains", [])
        if isinstance(state_domains, list):
            blocked_domains = sorted(
                {domain for domain in [*blocked_domains, *state_domains] if isinstance(domain, str)}
            )
        search_hashes = task.context.get("public_search_query_hashes", [])
        if not isinstance(search_hashes, list):
            search_hashes = []
        state_search_hashes = task.execution_state.get("public_search_query_hashes", [])
        if isinstance(state_search_hashes, list):
            search_hashes = sorted(
                {value for value in [*search_hashes, *state_search_hashes] if isinstance(value, str)}
            )
        return ToolContext(
            user_id=user_id,
            run_id=run_id,
            metadata={
                "observed_public_evidence": evidence if isinstance(evidence, list) else [],
                "structured_job_candidates": _structured_job_candidates(db, run_id),
                "confirmed_profile_facts": profile_facts
                if isinstance(profile_facts, dict)
                else {},
                "task_goal": task.goal,
                "resolved_step_inputs": task.context.get(
                    "resolved_step_inputs", {"context": {}, "artifacts": {}}
                ),
                "blocked_public_domains": blocked_domains,
                "public_search_query_hashes": search_hashes,
            },
        )

    @staticmethod
    def _persist_observed_evidence(
        db: Session,
        run_id: str,
        step: AgentStep,
        execution: ExecutorResult,
    ) -> list[dict[str, str]]:
        """Persist only tool-derived public evidence, never model-proposed URIs."""
        artifact_refs: list[dict[str, str]] = []
        for observation in execution.observations:
            output = observation.output or {}
            raw_pages = output.get("pages")
            pages = raw_pages if isinstance(raw_pages, list) else [output]
            for page in pages:
                if not isinstance(page, dict):
                    continue
                source_url = page.get("source_url")
                content_hash = page.get("content_hash")
                visible_text = page.get("visible_text")
                if not all(
                    isinstance(value, str)
                    for value in (source_url, content_hash, visible_text)
                ):
                    continue
                artifact = run_repository.create_evidence_artifact(
                    db,
                    run_id=run_id,
                    step_id=step.id,
                    source_url=source_url,
                    content_hash=content_hash,
                    content_json={
                        "title": page.get("title"),
                        "visible_text": visible_text[:_STRUCTURED_FULL_TEXT_CHARS],
                        "effective_url": page.get("effective_url"),
                        "redirect_chain": page.get("redirect_chain", []),
                        "http_status": page.get("http_status"),
                        "quality": page.get("quality"),
                        "quality_signal": page.get("quality_signal"),
                    },
                )
                artifact_ref = {
                    "artifact_id": artifact.id,
                    "artifact_type": "public_job_page",
                    "tool": observation.tool_name,
                    "source_url": source_url,
                    "content_hash": content_hash,
                }
                if page.get("quality") is not None:
                    artifact_ref["quality"] = page["quality"]
                artifact_refs.append(artifact_ref)
                run_repository.append_event(
                    db,
                    run_id=run_id,
                    event_type="executor_tool_observation",
                    payload_json={
                        "sequence": step.sequence,
                        "tool": observation.tool_name,
                        "artifact_id": artifact.id,
                        "source_url": source_url,
                        "content_hash": content_hash,
                    },
                )
        for observation in execution.observations:
            output = observation.output or {}
            source_url = output.get("source_url")
            content_hash = output.get("content_hash")
            # Sheet queries emit ``records``; page searches emit ``results``.
            # Both are candidate/URL lists carrying the evidence binding, so
            # both persist as job_search_results artifacts (C005).
            raw = output.get("results")
            if raw is None:
                raw = output.get("records")
            if not (
                isinstance(source_url, str)
                and isinstance(content_hash, str)
                and isinstance(raw, list)
            ):
                continue
            search_content: dict[str, Any] = {
                "query": output.get("query"),
                "results": raw,
            }
            if output.get("provider") == "juejin_official_search":
                search_content.update(
                    {
                        "terminal_reason": output.get("terminal_reason"),
                        "provider": output.get("provider"),
                        "source_scope": output.get("source_scope"),
                        "time_window_days": output.get("time_window_days"),
                        "coverage_complete": output.get("coverage_complete"),
                        "scanned_result_count": output.get("scanned_result_count"),
                        "matched_result_count": output.get("matched_result_count"),
                        "scan_queries": output.get("scan_queries", []),
                        "scan_evidence": output.get("scan_evidence", []),
                    }
                )
            artifact = run_repository.create_artifact(
                db,
                run_id=run_id,
                step_id=step.id,
                artifact_type="job_search_results",
                source_url=source_url,
                content_hash=content_hash,
                content_json=search_content,
            )
            completion_valid = (
                observation.tool_name == "search-public-job-pages"
                and skill_observation_is_semantically_valid(
                    observation.tool_name, output
                )
            )
            artifact_refs.append(
                {
                    "artifact_id": artifact.id,
                    "artifact_type": "job_search_results",
                    "tool": observation.tool_name,
                    "source_url": source_url,
                    "content_hash": content_hash,
                    "semantic_valid": (
                        "true"
                        if job_search_results_are_routable(raw) or completion_valid
                        else "false"
                    ),
                    "completion_valid": "true" if completion_valid else "false",
                    "result_count": str(len(raw)),
                }
            )
            run_repository.append_event(
                db,
                run_id=run_id,
                event_type="executor_search_artifact",
                payload_json={
                    "sequence": step.sequence,
                    "tool": observation.tool_name,
                    "artifact_id": artifact.id,
                    "source_url": source_url,
                    "content_hash": content_hash,
                },
            )
        for observation in execution.observations:
            output = observation.output or {}
            raw_details = output.get("details")
            details = raw_details if isinstance(raw_details, list) else [output]
            for detail in details:
                if not isinstance(detail, dict):
                    continue
                source_url = detail.get("source_url")
                content_hash = detail.get("content_hash")
                candidates = detail.get("candidates")
                if not (
                    isinstance(source_url, str)
                    and isinstance(content_hash, str)
                    and isinstance(candidates, list)
                ):
                    continue
                artifact = run_repository.create_artifact(
                    db,
                    run_id=run_id,
                    step_id=step.id,
                    artifact_type="structured_job_details",
                    source_url=source_url,
                    content_hash=content_hash,
                    content_json={"candidates": candidates},
                )
                detail_ref = {
                        "artifact_id": artifact.id,
                        "artifact_type": "structured_job_details",
                        "tool": observation.tool_name,
                        "source_url": source_url,
                        "content_hash": content_hash,
                }
                if detail.get("source_quality") is not None:
                    detail_ref["source_quality"] = detail["source_quality"]
                artifact_refs.append(detail_ref)
                run_repository.append_event(
                    db,
                    run_id=run_id,
                    event_type="executor_structured_artifact",
                    payload_json={
                        "sequence": step.sequence,
                        "tool": observation.tool_name,
                        "artifact_id": artifact.id,
                        "source_url": source_url,
                        "content_hash": content_hash,
                    },
                )
        # Stage 1.4: tool_name -> artifact_type comes from the single source
        # of truth in ``career_skills.registry.TOOL_ARTIFACT_TYPE`` (replaces
        # the previously duplicated 3-entry inline dict). Page, search and
        # detail artifacts are already persisted by the loops above, so only
        # the report artifacts (matching/tailoring/planning) are written here.
        _ALREADY_PERSISTED_ARTIFACT_TYPES = frozenset(
            {"public_job_page", "job_search_results", "structured_job_details"}
        )
        for observation in execution.observations:
            artifact_type = TOOL_ARTIFACT_TYPE.get(observation.tool_name)
            if artifact_type in _ALREADY_PERSISTED_ARTIFACT_TYPES:
                continue
            output = observation.output or {}
            source_url = _skill_artifact_source_url(artifact_type, output)
            if artifact_type is None or source_url is None:
                continue
            content_json = dict(output)
            content_hash = hashlib.sha256(
                json.dumps(
                    content_json,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
            ).hexdigest()
            artifact = run_repository.create_artifact(
                db,
                run_id=run_id,
                step_id=step.id,
                artifact_type=artifact_type,
                source_url=source_url,
                content_hash=content_hash,
                content_json=content_json,
            )
            artifact_refs.append(
                {
                    "artifact_id": artifact.id,
                    "artifact_type": artifact_type,
                    "tool": observation.tool_name,
                    "source_url": source_url,
                    "content_hash": content_hash,
                    "semantic_valid": (
                        "true"
                        if skill_observation_is_semantically_valid(
                            observation.tool_name, output
                        )
                        else "false"
                    ),
                }
            )
            run_repository.append_event(
                db,
                run_id=run_id,
                event_type="executor_skill_artifact",
                payload_json={
                    "sequence": step.sequence,
                    "tool": observation.tool_name,
                    "artifact_id": artifact.id,
                    "artifact_type": artifact_type,
                },
            )
        return artifact_refs

    @staticmethod
    def _persist_seeded_public_evidence(
        *,
        db: Session,
        run_id: str,
        step: AgentStep,
        task: AgentTaskRequest,
    ) -> None:
        """Hydrate trusted chain evidence into the current run's ledger.

        Chained evaluations pass the previous run's immutable artifact
        projection as ``observed_public_evidence``. Persisting that projection
        before the first step keeps the current run's audit and downstream
        tools consistent with the evidence already shown to the Executor.
        Entries without a source URL, hash, or visible text are ignored.
        """
        raw_evidence = task.context.get("observed_public_evidence")
        if not isinstance(raw_evidence, list):
            return
        seeded_count = 0
        for item in raw_evidence:
            if not isinstance(item, dict) or item.get("artifact_type") not in {
                None,
                "public_job_page",
            }:
                continue
            source_url = item.get("source_url")
            content_hash = item.get("content_hash")
            visible_text = item.get("visible_text")
            if not all(
                isinstance(value, str) and value
                for value in (source_url, content_hash, visible_text)
            ):
                continue
            content_json = {
                key: item[key]
                for key in (
                    "title",
                    "visible_text",
                    "effective_url",
                    "redirect_chain",
                    "http_status",
                    "quality",
                    "quality_signal",
                )
                if key in item
            }
            run_repository.create_evidence_artifact(
                db,
                run_id=run_id,
                step_id=step.id,
                source_url=source_url,
                content_hash=content_hash,
                content_json=content_json,
            )
            seeded_count += 1
        if seeded_count:
            run_repository.append_event(
                db,
                run_id=run_id,
                event_type="inherited_public_evidence_hydrated",
                payload_json={"sequence": step.sequence, "artifact_count": seeded_count},
            )

    @staticmethod
    def _step_seeded_artifact_refs(
        *, db: Session, run_id: str, step: AgentStep
    ) -> list[dict[str, str]]:
        """Project chain-hydrated page artifacts into the current step refs."""
        refs: list[dict[str, str]] = []
        for artifact in run_repository.list_evidence_artifacts(db, run_id):
            if artifact.step_id != step.id or artifact.artifact_type != "public_job_page":
                continue
            quality = artifact.content_json.get("quality")
            ref: dict[str, str] = {
                "artifact_id": artifact.id,
                "artifact_type": artifact.artifact_type,
                "tool": "fetch-public-job-pages",
                "source_url": artifact.source_url,
                "content_hash": artifact.content_hash,
            }
            if isinstance(quality, str):
                ref["quality"] = quality
            refs.append(ref)
        return refs

    @staticmethod
    def _resolved_step_artifact_refs(
        context: ToolContext,
    ) -> list[dict[str, str]]:
        """Project trusted upstream step outputs into the current step refs.

        ``_prepare_step_inputs`` resolves artifact ports from the current run's
        previous steps and places that bounded projection in ToolContext. The
        Executor must see those refs as usable evidence even when it chooses no
        new fetch tool; otherwise a valid upstream JD is invisible to the
        current step's deterministic completion gate.
        """
        resolved = context.metadata.get("resolved_step_inputs", {})
        if not isinstance(resolved, dict):
            return []
        artifacts = resolved.get("artifacts", {})
        if not isinstance(artifacts, dict):
            return []
        refs: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for source in artifacts.values():
            if not isinstance(source, list):
                continue
            for item in source:
                if not isinstance(item, dict):
                    continue
                required = ("artifact_id", "artifact_type", "source_url", "content_hash")
                if not all(isinstance(item.get(key), str) and item.get(key) for key in required):
                    continue
                key = (item["artifact_id"], item["content_hash"])
                if key in seen:
                    continue
                seen.add(key)
                ref = {
                    key: item[key]
                    for key in ("artifact_id", "artifact_type", "source_url", "content_hash")
                }
                for optional in (
                    "tool",
                    "quality",
                    "source_quality",
                    "output_name",
                    "semantic_valid",
                    "completion_valid",
                ):
                    value = item.get(optional)
                    if isinstance(value, str) and value:
                        ref[optional] = value
                refs.append(ref)
        return refs

    def _recovery_context(
        self,
        *,
        db: Session,
        run_id: str,
        task: AgentTaskRequest,
        plan_step: PlanStep,
        persisted_step: AgentStep,
        context: ToolContext,
        tool_budget: ToolCallBudget,
    ) -> _AgentRecoveryContext:
        """Build the runtime-owned capability context for skill recovery."""
        return _AgentRecoveryContext(
            runtime=self,
            db=db,
            run_id=run_id,
            task=task,
            plan_step=plan_step,
            persisted_step=persisted_step,
            context=context,
            tool_budget=tool_budget,
        )

    def _auto_extract_jd_details(
        self,
        *,
        db: Session,
        run_id: str,
        task: AgentTaskRequest,
        plan_step: PlanStep,
        persisted_step: AgentStep,
        context: ToolContext,
        artifact_refs: list[dict[str, Any]],
        tool_budget: ToolCallBudget,
    ) -> tuple[list[Any], list[dict[str, str]]]:
        """Delegate JD normalization to the job-discovery skill strategy."""
        if "job-discovery" not in plan_step.allowed_skills:
            return [], []
        ctx = self._recovery_context(
            db=db, run_id=run_id, task=task, plan_step=plan_step,
            persisted_step=persisted_step, context=context,
            tool_budget=tool_budget,
        )
        return _skill_auto_extract(ctx, artifact_refs)

    def _auto_recover_discovery_evidence(
        self,
        *,
        db: Session,
        run_id: str,
        task: AgentTaskRequest,
        plan_step: PlanStep,
        persisted_step: AgentStep,
        context: ToolContext,
        observations: list[Any],
        artifact_refs: list[dict[str, Any]],
        tool_budget: ToolCallBudget,
    ) -> tuple[list[Any], list[dict[str, str]]]:
        """Delegate source-bound discovery recovery to the job-discovery skill."""
        if "job-discovery" not in plan_step.allowed_skills:
            return [], []
        ctx = self._recovery_context(
            db=db, run_id=run_id, task=task, plan_step=plan_step,
            persisted_step=persisted_step, context=context,
            tool_budget=tool_budget,
        )
        return _skill_recover(ctx, observations, artifact_refs)

    def _auto_build_role_deliverable(
        self,
        *,
        db: Session,
        run_id: str,
        task: AgentTaskRequest,
        plan_step: PlanStep,
        persisted_step: AgentStep,
        context: ToolContext,
        artifact_refs: list[dict[str, Any]],
        tool_budget: ToolCallBudget,
    ) -> tuple[list[Any], list[dict[str, str]]]:
        """Delegate deliverable recovery to the owning career skill."""
        ctx = self._recovery_context(
            db=db, run_id=run_id, task=task, plan_step=plan_step,
            persisted_step=persisted_step, context=context,
            tool_budget=tool_budget,
        )
        if "job-matching" in plan_step.allowed_skills:
            return _skill_build_matching(ctx, artifact_refs)
        if "resume-tailoring" in plan_step.allowed_skills:
            return _skill_build_tailoring(ctx, artifact_refs)
        return [], []

    @staticmethod
    def _record_failed_executor_observations(
        db: Session,
        run_id: str,
        step: AgentStep,
        execution: ExecutorResult,
    ) -> None:
        """Persist stable failure codes so Agent retries are independently auditable."""
        for observation in execution.observations:
            if observation.status != "failed" or not observation.error_code:
                continue
            run_repository.append_event(
                db,
                run_id=run_id,
                event_type="executor_tool_failed",
                payload_json={
                    "sequence": step.sequence,
                    "tool": observation.tool_name,
                    "error_code": observation.error_code,
                },
            )

    def _finish_planner_non_plan(
        self,
        db: Session,
        run_id: str,
        run: AgentRun,
        planner_result: PlannerResult,
    ) -> AgentRunResult:
        if planner_result.status == "needs_user":
            planner_error_code = planner_result.error_code or "need_user"
            return self._finish_planner_waiting(
                db,
                run_id,
                run,
                planner_result.user_question,
                event_type="planner_needs_user",
                error_code=planner_result.error_code,
                terminal_contract=build_terminal_contract(
                    error_code=planner_error_code,
                    source_role="planner",
                    phase="planning",
                ),
            )
        if planner_result.error_code == "wall_clock_budget_exhausted":
            # The planner ran out of wall-clock before producing a plan. This
            # is a transport/resource pause, not a business failure: resume()
            # recomputes a fresh deadline and the planner re-attempts its plan.
            # Route to a recoverable waiting_user with a distinct event and the
            # stable error_code for observability, instead of failing the run.
            return self._finish_planner_waiting(
                db,
                run_id,
                run,
                "运行时间预算耗尽（模型响应偏慢），无法生成执行计划。恢复运行将获得新的时间窗口重试。",
                event_type="planner_budget_exhausted",
                error_code="wall_clock_budget_exhausted",
            )
        error_code = planner_result.error_code or "planner_failed"
        run_repository.finish_run(db, run, status=RunStatus.failed, error_code=error_code)
        run_repository.append_event(
            db,
            run_id=run_id,
            event_type="run_failed",
            payload_json={
                "error_code": error_code,
                **build_terminal_contract(
                    error_code=error_code,
                    source_role="planner",
                    phase="planning",
                ).as_payload(),
            },
        )
        return AgentRunResult(run_id, RunStatus.failed, None, error_code)

    def _finish_planner_invalid_model(
        self,
        db: Session,
        run_id: str,
        run: AgentRun,
    ) -> AgentRunResult:
        """A planner with persistently invalid output cannot plan safely: wait for a retry."""
        return self._finish_planner_waiting(
            db,
            run_id,
            run,
            "模型输出格式异常，无法生成执行计划。请重试，或补充岗位/条件信息后重新发起。",
            event_type="planner_needs_user",
        )

    def _finish_planner_waiting(
        self,
        db: Session,
        run_id: str,
        run: AgentRun,
        question: str,
        *,
        event_type: str,
        error_code: str | None = None,
        terminal_contract: TerminalContract | None = None,
    ) -> AgentRunResult:
        """Finish a planner-paused run as ``waiting_user`` without an AgentStep.

        The planner has no persisted step yet, so ``_wait_for_user`` (which
        requires a step) cannot be used. This helper closes the run as
        recoverable and emits the caller-supplied ``event_type`` so the
        distinct pause reasons (``planner_needs_user`` for invalid-model /
        need-user, ``planner_budget_exhausted`` for wall-clock) stay
        distinguishable in the event trace. ``error_code`` is persisted on the
        run row and surfaced on the result for observability when set (e.g.
        wall-clock); the invalid-model / need-user paths keep ``error_code=None``.
        """
        run_repository.finish_run(
            db,
            run,
            status=RunStatus.waiting_user,
            final_summary=question,
            error_code=error_code,
        )
        run_repository.append_event(
            db,
            run_id=run_id,
            event_type=event_type,
            payload_json={
                "question": question,
                **(
                    terminal_contract.as_payload()
                    if terminal_contract is not None
                    else build_terminal_contract(
                        error_code=error_code,
                        source_role="planner",
                        phase="planning",
                    ).as_payload()
                ),
            },
        )
        return AgentRunResult(run_id, RunStatus.waiting_user, question, error_code)

    def _rescue_step_succeeded(
        self,
        db: Session,
        run_id: str,
        step: AgentStep,
        execution: ExecutorResult,
        *,
        event_type: str,
        reason: str,
        output_artifact_refs: list[dict[str, str]] | None = None,
    ) -> AgentRunResult:
        """Upgrade a step to succeeded when its deliverable is already tool-backed.

        Used by the termination rescues: a verifier transport failure (invalid
        model output) or the executor's own needs_user hand-off must not fail
        a step whose skill deliverable is already persisted and verified by
        the deterministic contract. The caller guarantees both gates - the
        deliverable contract (tool-backed observation) and the blocked-
        evidence gate (no human-gated evidence) - so this helper never
        auto-passes a blocked step; blocked evidence keeps ending
        human-in-the-loop. The rescue event is explicit and human-readable so
        the trace always distinguishes a rescue from a real verification PASS.
        """
        run_repository.finish_step(
            db,
            step,
            status=StepStatus.succeeded,
            output_artifact_refs=(
                output_artifact_refs
                if output_artifact_refs is not None
                else execution.artifact_refs
            ),
        )
        run_repository.append_event(
            db,
            run_id=run_id,
            event_type="step_succeeded",
            payload_json={"sequence": step.sequence},
        )
        run_repository.append_event(
            db,
            run_id=run_id,
            event_type=event_type,
            payload_json={"sequence": step.sequence, "reason": reason},
        )
        return AgentRunResult(run_id, RunStatus.running, execution.summary)

    def _request_replan(
        self,
        db: Session,
        run_id: str,
        step: AgentStep,
        *,
        feedback: str | None,
        summary: str,
        output_artifact_refs: list[dict[str, str]],
        reason: ReplanReason = ReplanReason.VERIFIER_REPLAN,
    ) -> AgentRunResult:
        """Close a step as skipped with a replan_required outcome.

        Shared by the verifier REPLAN decision and the bounded conversions
        (NEED_USER over a satisfied contract, RETRY over a scoped-out tool):
        the step is marked skipped with the stable ``replan_required`` code, a
        ``verification_replan`` event records the verifier feedback, and the
        run loop replans with ``summary`` appended to verifier_feedback.
        """
        run_repository.finish_step(
            db,
            step,
            status=StepStatus.skipped,
            output_artifact_refs=output_artifact_refs,
            error_code="replan_required",
        )
        run_repository.append_event(
            db,
            run_id=run_id,
            event_type="verification_replan",
            payload_json={
                "sequence": step.sequence,
                "feedback": feedback,
                "reason": reason.value,
            },
        )
        return AgentRunResult(
            run_id,
            RunStatus.running,
            summary,
            "replan_required",
            reason,
        )

    def _wait_for_user(
        self,
        db: Session,
        run_id: str,
        step: AgentStep,
        question: str | None,
        *,
        output_artifact_refs: list[dict[str, str]] | None = None,
        error_code: str | None = None,
        terminal_contract: TerminalContract | None = None,
    ) -> AgentRunResult:
        """Pause a step-bearing run for a human reply, recoverable via resume().

        ``error_code`` is persisted on both the step and the run row when set
        (e.g. ``wall_clock_budget_exhausted``) so the pause reason is observable
        without inspecting the event trace; genuine need-user pauses keep the
        default ``need_user`` step code and ``None`` run error_code.
        """
        run = db.get(AgentRun, run_id)
        if run is None:  # defensive: foreign-key integrity normally prevents this.
            raise RuntimeError("Agent run disappeared during execution")
        run_repository.finish_step(
            db,
            step,
            status=StepStatus.failed,
            output_artifact_refs=output_artifact_refs,
            error_code=error_code or "need_user",
        )
        run_repository.finish_run(
            db,
            run,
            status=RunStatus.waiting_user,
            final_summary=question,
            error_code=error_code,
        )
        run_repository.append_event(
            db,
            run_id=run_id,
            event_type="run_needs_user",
            payload_json={
                "question": question,
                **(
                    terminal_contract.as_payload()
                    if terminal_contract is not None
                    else build_terminal_contract(
                        error_code=error_code,
                        source_role="executor",
                        phase="execution",
                        artifact_count=len(output_artifact_refs or []),
                    ).as_payload()
                ),
            },
        )
        return AgentRunResult(run_id, RunStatus.waiting_user, question, error_code)

    def _fail_step(
        self,
        db: Session,
        run_id: str,
        step: AgentStep,
        error_code: str,
        *,
        output_artifact_refs: list[dict[str, str]] | None = None,
    ) -> AgentRunResult:
        run_repository.finish_step(
            db,
            step,
            status=StepStatus.failed,
            output_artifact_refs=output_artifact_refs,
            error_code=error_code,
        )
        return self._fail_run(db, run_id, error_code)

    @staticmethod
    def _fail_run(db: Session, run_id: str, error_code: str) -> AgentRunResult:
        """Close an already-created run with a stable, user-safe error code."""
        run = db.get(AgentRun, run_id)
        if run is None:  # defensive: foreign-key integrity normally prevents this.
            raise RuntimeError("Agent run disappeared during execution")
        run_repository.finish_run(
            db, run, status=RunStatus.failed, error_code=error_code
        )
        run_repository.append_event(
            db,
            run_id=run_id,
            event_type="run_failed",
            payload_json={
                "error_code": error_code,
                **build_terminal_contract(
                    error_code=error_code,
                    source_role="runtime",
                    phase="execution",
                ).as_payload(),
            },
        )
        return AgentRunResult(run_id, RunStatus.failed, None, error_code)


def _execution_progress_fingerprint(execution: ExecutorResult) -> str:
    """Fingerprint evidence identity, not database-generated artifact IDs.

    Verifier retries may persist a fresh report row for the same source. Using
    that row ID would look like progress and permit an infinite retry loop.
    Stable source URLs, content hashes, candidate IDs and terminal reasons are
    the meaningful progress signals.
    """
    observations: list[dict[str, Any]] = []
    for observation in execution.observations:
        output = observation.output if isinstance(observation.output, dict) else {}
        pages = output.get("pages") if isinstance(output.get("pages"), list) else []
        page_keys = [
            {
                "source_url": page.get("source_url"),
                "content_hash": page.get("content_hash"),
                "quality": page.get("quality"),
            }
            for page in pages
            if isinstance(page, dict)
        ]
        candidates = output.get("candidates")
        candidate_keys = []
        if isinstance(candidates, list):
            candidate_keys = [
                {
                    "candidate_id": item.get("candidate_id"),
                    "source_artifact_id": item.get("source_artifact_id"),
                    "source_url": item.get("source_url"),
                    "content_hash": item.get("content_hash"),
                }
                for item in candidates
                if isinstance(item, dict)
            ]
        observations.append(
            {
                "tool": observation.tool_name,
                "status": observation.status,
                "error_code": observation.error_code,
                "source_url": output.get("source_url"),
                "content_hash": output.get("content_hash"),
                "terminal_reason": output.get("terminal_reason"),
                "pages": page_keys,
                "candidates": candidate_keys,
            }
        )
    payload = {"observations": observations}
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _normalize_plan_steps(plan: ExecutionPlan) -> list[tuple[tuple[str, ...], str]]:
    """Project a plan onto its comparable step sequence for the replan guard."""
    return [
        (
            tuple(sorted(step.allowed_skills or [])),
            " ".join(str(step.objective or "").split()),
        )
        for step in plan.steps
    ]


def _skill_artifact_source_url(
    artifact_type: str | None, output: dict[str, object]
) -> str | None:
    """Keep a multi-JD report traceable without inventing a synthetic source.

    A matching report is derived from several immutable job pages, so it has no
    single top-level ``source_url``. Its individual match rows retain every
    source; use the first observed source only as the artifact's index field.
    """
    direct_source = output.get("source_url")
    if isinstance(direct_source, str) and direct_source:
        return direct_source
    if artifact_type != "job_matching_report":
        return None
    matches = output.get("matches")
    if not isinstance(matches, list):
        return None
    for match in matches:
        if isinstance(match, dict):
            source_url = match.get("source_url")
            if isinstance(source_url, str) and source_url:
                return source_url
    evaluated_source_urls = output.get("evaluated_source_urls")
    if isinstance(evaluated_source_urls, list):
        return next(
            (
                source_url
                for source_url in evaluated_source_urls
                if isinstance(source_url, str) and source_url
            ),
            None,
        )
    return None


def _persisted_job_search_observations(
    db: Session,
    run_id: str,
    artifact_refs: list[dict[str, Any]],
) -> list[ToolObservation]:
    """Rehydrate referenced search/sheet rows for deterministic recovery.

    Step inputs carry immutable pointers rather than provider payloads. The
    recovery path reloads only those same-run ``job_search_results`` rows so
    it can fetch their public URLs and derive bounded company search hints.
    """
    refs_by_id = {
        str(ref["artifact_id"]): ref
        for ref in artifact_refs
        if isinstance(ref, dict)
        and ref.get("artifact_type") == "job_search_results"
        and isinstance(ref.get("artifact_id"), str)
    }
    if not refs_by_id:
        return []
    observations: list[ToolObservation] = []
    for artifact in run_repository.list_evidence_artifacts(db, run_id):
        ref = refs_by_id.get(artifact.id)
        if ref is None or artifact.artifact_type != "job_search_results":
            continue
        results = artifact.content_json.get("results")
        if not isinstance(results, list):
            continue
        query = artifact.content_json.get("query")
        tool_name = ref.get("tool")
        if tool_name not in {
            "query-career-sheet-records",
            "search-public-job-pages",
        }:
            tool_name = (
                "query-career-sheet-records"
                if isinstance(query, dict)
                else "search-public-job-pages"
            )
        collection_name = (
            "records"
            if tool_name == "query-career-sheet-records"
            else "results"
        )
        observations.append(
            ToolObservation(
                tool_name=tool_name,
                status="succeeded",
                output={
                    collection_name: results,
                    "query": query,
                    "source_url": artifact.source_url,
                    "content_hash": artifact.content_hash,
                },
            )
        )
    return observations


def _structured_job_candidates(db: Session, run_id: str) -> list[dict[str, Any]]:
    """Flatten the run's persisted ``structured_job_details`` into candidate units.

    Each extract artifact's ``candidates`` list becomes one compact dict
    (title / locations / bounded sections) carrying the artifact's identity.
    Returns [] when the run has no structured extraction yet, so the matching
    tool falls back to raw page evidence.
    """
    items: list[dict[str, Any]] = []
    artifacts = run_repository.list_evidence_artifacts(db, run_id)
    quality_by_source = {
        artifact.source_url: artifact.content_json.get("quality")
        for artifact in artifacts
        if artifact.artifact_type == "public_job_page"
        and isinstance(artifact.content_json.get("quality"), str)
    }
    search_metadata_by_url: dict[str, dict[str, Any]] = {}
    for artifact in artifacts:
        if artifact.artifact_type != "job_search_results":
            continue
        raw_results = artifact.content_json.get("results")
        if not isinstance(raw_results, list):
            continue
        for result in raw_results:
            if not isinstance(result, dict):
                continue
            urls = [
                result.get("url"),
                result.get("source_url"),
                result.get("apply_url"),
                result.get("link"),
            ]
            prior = result.get("prior_metadata")
            if isinstance(prior, dict):
                urls.append(prior.get("apply_url"))
            metadata = {
                key: result.get(key)
                for key in (
                    "updated_at",
                    "published_at",
                    "posted_at",
                    "publish_time",
                    "update_time",
                )
                if result.get(key) not in (None, "")
            }
            if isinstance(prior, dict):
                metadata.update(
                    {
                        key: prior.get(key)
                        for key in (
                            "updated_at",
                            "published_at",
                            "posted_at",
                            "publish_time",
                            "update_time",
                        )
                        if prior.get(key) not in (None, "")
                    }
                )
            if metadata:
                for url in urls:
                    if isinstance(url, str) and url:
                        search_metadata_by_url[url] = metadata
    for artifact in artifacts:
        raw_candidates = artifact.content_json.get("candidates")
        if not isinstance(raw_candidates, list):
            continue
        for candidate_index, candidate in enumerate(raw_candidates):
            if not isinstance(candidate, dict):
                continue
            source_url = candidate.get("apply_url")
            if not isinstance(source_url, str) or not source_url:
                source_url = artifact.source_url
            title = candidate.get("title")
            items.append(
                {
                    "artifact_id": artifact.id,
                    "candidate_id": f"{artifact.id}:candidate:{candidate_index}",
                    #: The evidence artifact this candidate was extracted from
                    #: (``ExtractedJobDetails.evidence_refs``), so a collapsed
                    #: page pointer can also resolve by artifact identity.
                    "source_artifact_id": _evidence_artifact_id(candidate),
                    "source_url": source_url,
                    "page_source_url": artifact.source_url,
                    "page_title": artifact.content_json.get("title")
                    if isinstance(artifact.content_json.get("title"), str)
                    else None,
                    "page_text_prefix": (
                        artifact.content_json.get("visible_text", "")[:2000]
                        if isinstance(artifact.content_json.get("visible_text"), str)
                        else ""
                    ),
                    "apply_url": (
                        candidate.get("apply_url")
                        if isinstance(candidate.get("apply_url"), str)
                        else None
                    ),
                    "content_hash": artifact.content_hash,
                    # The page that produced the candidate is authoritative:
                    # a list page may point at a URL that was later fetched as
                    # a detail page, but its card must not inherit the detail
                    # page's quality and bypass the list-only gate.
                    "source_quality": quality_by_source.get(artifact.source_url)
                    or quality_by_source.get(source_url),
                    **search_metadata_by_url.get(artifact.source_url, {}),
                    **search_metadata_by_url.get(source_url, {}),
                    "title": title if isinstance(title, str) else None,
                    "locations": _string_list(candidate.get("locations")),
                    "recruitment_types": _string_list(
                        candidate.get("recruitment_types")
                    ),
                    "deadline_text": (
                        candidate.get("deadline_text")
                        if isinstance(candidate.get("deadline_text"), str)
                        else None
                    ),
                    "published_at": (
                        candidate.get("published_at")
                        if isinstance(candidate.get("published_at"), str)
                        else None
                    ),
                    "skills": _string_list(candidate.get("skills")),
                    # Card-list extraction can land JD snippets in company_name
                    # (Feishu campus cards) — carry it as bounded evidence so the
                    # match tool can score on it, never as a trusted company fact.
                    "company_name": _bounded_section(candidate.get("company_name")),
                    "responsibilities": _bounded_section(candidate.get("responsibilities")),
                    "requirements": _bounded_section(candidate.get("requirements")),
                    # B1: strength dict {score, tier, base_score, evidence[]},
                    # optional for downstream scoring, carried for audit.
                    "strength": candidate.get("strength"),
                    # Full candidate JD text for deliverable tools (e.g.
                    # build-resume-tailoring-brief): the evidence projection may
                    # collapse an old artifact's visible_text, but the extracted
                    # sections retain the complete job text. Tool-side authority
                    # only - never enters model prompts.
                    "full_text": _full_candidate_text(candidate, title),
                }
            )
    return items


def _full_observed_public_evidence(
    db: Session, run_id: str
) -> list[dict[str, Any]]:
    """Hydrate persisted page pointers only at the deterministic tool boundary."""
    items: list[dict[str, Any]] = []
    artifacts = run_repository.list_evidence_artifacts(db, run_id)
    for artifact in artifacts:
        visible_text = artifact.content_json.get("visible_text")
        if not isinstance(visible_text, str) or not visible_text:
            continue
        item: dict[str, Any] = {
            "artifact_id": artifact.id,
            "source_url": artifact.source_url,
            "content_hash": artifact.content_hash,
            "visible_text": visible_text,
        }
        for key in (
            "title",
            "effective_url",
            "redirect_chain",
            "http_status",
            "quality",
            "quality_signal",
        ):
            value = artifact.content_json.get(key)
            if value not in (None, "", [], {}):
                item[key] = value
        items.append(item)
    return items


def _structured_artifact_visible_text(content_json: dict[str, Any]) -> str:
    """Project extracted JD candidates back into bounded model-visible text.

    Structured extraction artifacts intentionally do not duplicate page
    ``visible_text``.  They are nevertheless durable, tool-produced evidence
    and are the only complete fallback when an older page artifact has been
    collapsed by the run-level evidence budget.  Keep the projection bounded
    so this fallback cannot bypass the global context ceiling.
    """
    raw_candidates = content_json.get("candidates")
    if not isinstance(raw_candidates, list):
        return ""
    sections: list[str] = []
    for candidate in raw_candidates:
        if not isinstance(candidate, dict):
            continue
        text = _full_candidate_text(candidate, candidate.get("title"))
        if text:
            sections.append(text)
    return "\n\n".join(sections)[:_STRUCTURED_FULL_TEXT_CHARS]


def _evidence_artifact_id(candidate: dict[str, Any]) -> str | None:
    """Return the evidence artifact a candidate was extracted from, if recorded.

    ``ExtractedJobDetails.evidence_refs`` pins each candidate to the source
    page artifact; carrying it lets a collapsed ``observed_public_evidence``
    pointer match by artifact identity even when the extraction found a
    distinct ``apply_url`` for the candidate.
    """
    evidence_refs = candidate.get("evidence_refs")
    if not isinstance(evidence_refs, list):
        return None
    for ref in evidence_refs:
        if not isinstance(ref, dict):
            continue
        value = ref.get("artifact_id")
        if isinstance(value, str) and value:
            return value
    return None


def _full_candidate_text(candidate: dict[str, Any], title: object) -> str:
    """Preserve the complete candidate JD text for deliverable tools.

    Section fields are kept at their persisted length (extraction already
    bounds them to the page's visible text); only a defensive per-candidate
    cap applies, so a collapsed page pointer can still resolve the full JD.
    """
    company_name = candidate.get("company_name")
    responsibilities = candidate.get("responsibilities")
    requirements = candidate.get("requirements")
    parts = [
        title if isinstance(title, str) and title else None,
        company_name if isinstance(company_name, str) and company_name else None,
        " ".join(_string_list(candidate.get("locations"))),
        responsibilities if isinstance(responsibilities, str) and responsibilities else None,
        requirements if isinstance(requirements, str) and requirements else None,
    ]
    return "\n".join(part for part in parts if part)[:_STRUCTURED_FULL_TEXT_CHARS]


def _string_list(value: object) -> list[str]:
    """Return the string items of a list value, or [] for non-list input."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _bounded_section(value: object) -> str:
    """Return a string section truncated to the structured-candidate budget."""
    if not isinstance(value, str):
        return ""
    return value[:_STRUCTURED_SECTION_CHARS]
