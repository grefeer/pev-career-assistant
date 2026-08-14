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
from backend.app.services.agent_runtime.evidence_gate import (
    completion_evidence_gate as legacy_completion_evidence_gate,
    has_blocked_evidence as legacy_has_blocked_evidence,
    has_known_deliverable_attempt,
    step_contract_met as legacy_step_contract_met,
)
from backend.app.services.agent_runtime.error_policy import (
    TerminalContract,
    build_terminal_contract,
)
from backend.app.services.agent_runtime.executor_agent import ExecutorAgent
from backend.app.services.agent_runtime.model_gateway import AgentModelGatewayError
from backend.app.services.agent_runtime.model_budget import ModelCallBudget
from backend.app.services.agent_runtime.planner_agent import PlannerAgent
from backend.app.services.agent_runtime.schemas import (
    AgentTaskRequest,
    ExecutionPlan,
    ExecutorResult,
    PlanStep,
    PlannerResult,
    ReplanReason,
    VerifierResult,
)
from backend.app.services.agent_runtime.skill_definition import SkillRegistry
from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.agent_runtime.tool_budget import ToolCallBudget
from backend.app.services.agent_runtime.turn_budget import AgentTurnBudget
from backend.app.services.agent_runtime.verifier_agent import VerifierAgent

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


class AgentRuntime:
    """Schedules agent-produced decisions while enforcing only hard lifecycle bounds."""

    def __init__(
        self,
        *,
        planner: PlannerAgent,
        executor: ExecutorAgent,
        verifier: VerifierAgent,
        agent_version: str,
        skills: SkillRegistry | None = None,
    ) -> None:
        self._planner = planner
        self._executor = executor
        self._verifier = verifier
        self._agent_version = agent_version
        # ``None`` is a deliberate migration mode for existing embedders. The
        # application composition root always injects a registry, which turns
        # on strict skill contracts and the final deterministic gate.
        self._skills = skills

    def run(
        self,
        db: Session,
        *,
        user_id: str,
        task: AgentTaskRequest,
        existing_run: AgentRun | None = None,
    ) -> AgentRunResult:
        """Run bounded PEV lifecycle; agents retain all semantic tool decisions."""
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
                    return self._finish_planner_invalid_model(db, run.id, run)
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
                try:
                    if self._skills is not None:
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
            observed_artifact_refs = self._persist_observed_evidence(
                db, run_id, persisted_step, execution
            )
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
                    artifact_refs=[*prior_artifact_refs, *observed_artifact_refs],
                    tool_budget=tool_budget,
                )
                if auto_observations or auto_artifact_refs:
                    auto_extract_attempted = True
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
            try:
                verification = self._verifier.run(
                    task=task,
                    plan=plan,
                    step=plan_step,
                    execution=execution,
                    context=context,
                    trace=trace,
                    tool_budget=tool_budget,
                    turn_budget=turn_budget,
                    deadline=deadline,
                )
            except AgentModelGatewayError as error:
                if error.code == "invalid_model_response":
                    if (
                        self._step_contract_met(
                            plan_step, execution.observations, execution.artifact_refs
                        )
                        and not self._step_has_blocked_evidence(
                            plan_step, execution.observations, execution.artifact_refs
                        )
                    ):
                        # The verifier transport degraded after the step's
                        # deliverable was already tool-backed (invalid
                        # model-output pattern). The contract is the same
                        # evidence the verifier would have checked; terminate
                        # the step as succeeded with an explicit rescue event
                        # instead of a spurious waiting_user. Blocked evidence
                        # (login/captcha/anti-bot/OCR-off) always keeps the
                        # human hand-off.
                        return self._rescue_step_succeeded(
                            db,
                            run_id,
                            persisted_step,
                            execution,
                            event_type="verifier_rescue_succeeded",
                            reason="invalid_model_response_contract_met",
                        )
                    # The verifier cannot produce a machine decision; route the
                    # step to a human check rather than failing the run.
                    verification = VerifierResult(
                        decision=VerificationDecision.NEED_USER,
                        feedback="核验模型输出格式异常，无法独立核验该步骤产出，请人工确认。",
                    )
                else:
                    return self._fail_step(
                        db,
                        run_id,
                        persisted_step,
                        error.code,
                        output_artifact_refs=execution.artifact_refs,
                    )
            if verification.error_code == "wall_clock_budget_exhausted":
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
                diagnostics = (
                    self._skills.completion_evidence_diagnostics(
                        plan_step,
                        execution.observations,
                        summary=execution.summary,
                        artifact_refs=execution.artifact_refs,
                    )
                    if self._skills is not None
                    else None
                )
                if diagnostics is not None and not diagnostics["gate_passed"]:
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
                    # VerifierResult schema rejects non-PASS decisions without
                    # non-empty feedback, so the False branch here is unreachable.
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
                    # R009/R018/R033/R013: the verifier asks the human even
                    # though the step's deliverable is already tool-backed and
                    # unblocked. The deterministic contract (the same evidence
                    # the verifier would inspect) is satisfied, so the hand-off
                    # is a request the agents already finished; a bounded replan
                    # lets the planner restructure the step instead of dying at
                    # step 1. Guarded by the replan budget and a once-per-run
                    # marker: the run loop appends this summary to
                    # verifier_feedback, so a second identical NEED_USER after
                    # the replan keeps the human hand-off. Blocked evidence
                    # (login/captcha/anti-bot/OCR-off) always keeps the human
                    # hand-off.
                    return self._request_replan(
                        db,
                        run_id,
                        persisted_step,
                        feedback=verification.feedback,
                        summary=(
                            f"{verification.feedback} {_NEEDS_USER_REPLAN_MARKER}"
                        ),
                        output_artifact_refs=execution.artifact_refs,
                        reason=ReplanReason.NEED_USER_CONTRACT,
                    )
                return self._wait_for_user(
                    db,
                    run_id,
                    persisted_step,
                    verification.feedback,
                    output_artifact_refs=execution.artifact_refs,
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
        if self._skills is None:
            return plan_step.requires_verification or plan.complexity.value in {"L3", "L4"}
        return self._skills.requires_verification(plan_step, plan.complexity.value)

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
        if self._skills is None:
            return legacy_step_contract_met(step, observations)
        if artifact_refs:
            return bool(
                self._skills.completion_evidence_diagnostics(
                    step,
                    observations,
                    summary="contract-check",
                    artifact_refs=artifact_refs,
                )["contract_met"]
            )
        return self._skills.step_contract_met(step, observations)

    def _has_blocked_evidence(self, observations: list) -> bool:
        if self._skills is None:
            return legacy_has_blocked_evidence(observations)
        return self._skills.has_blocked_evidence(observations)

    def _step_has_blocked_evidence(
        self,
        step: PlanStep,
        observations: list,
        artifact_refs: list[dict[str, Any]],
    ) -> bool:
        if self._skills is None:
            return legacy_has_blocked_evidence(observations)
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
        if self._skills is None:
            return has_known_deliverable_attempt(observations) and not legacy_completion_evidence_gate(
                step, observations, summary=summary
            )
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
            artifact = run_repository.create_artifact(
                db,
                run_id=run_id,
                step_id=step.id,
                artifact_type="job_search_results",
                source_url=source_url,
                content_hash=content_hash,
                content_json={"query": output.get("query"), "results": raw},
            )
            artifact_refs.append(
                {
                    "artifact_id": artifact.id,
                    "artifact_type": "job_search_results",
                    "tool": observation.tool_name,
                    "source_url": source_url,
                    "content_hash": content_hash,
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
        skill_artifact_types = {
            "match-observed-jobs": "job_matching_report",
            "build-resume-tailoring-brief": "resume_tailoring_brief",
            "build-preparation-plan": "career_preparation_plan",
        }
        for observation in execution.observations:
            artifact_type = skill_artifact_types.get(observation.tool_name)
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
        """Normalize captured JD pages before a model stall becomes a handoff."""
        if "job-discovery" not in plan_step.allowed_skills:
            return [], []
        if any(ref.get("runtime_auto_extract") == "true" for ref in artifact_refs):
            return [], []
        page_ids = [
            str(ref["artifact_id"])
            for ref in artifact_refs
            if ref.get("artifact_type") == "public_job_page"
            and ref.get("quality") == "jd_complete"
            and isinstance(ref.get("artifact_id"), str)
        ]
        if not page_ids:
            return [], []
        page_ids = list(dict.fromkeys(page_ids))
        tool_context = self._tool_context(
            user_id=context.user_id,
            run_id=run_id,
            task=task,
            db=db,
        )
        observations: list[Any] = []
        refs: list[dict[str, str]] = []
        for offset in range(0, len(page_ids), 10):
            if not tool_budget.try_consume():
                break
            observation = self._executor.invoke_registered_tool(
                name="extract-observed-job-details-batch",
                context=tool_context,
                payload={"artifact_ids": page_ids[offset : offset + 10]},
            )
            observations.append(observation)
            if observation.status != "succeeded":
                continue
            auto_execution = ExecutorResult(
                status="succeeded",
                observations=[observation],
                summary="已对已抓取的公开 JD 页面执行确定性结构化提取。",
            )
            batch_refs = self._persist_observed_evidence(
                db, run_id, persisted_step, auto_execution
            )
            for ref in batch_refs:
                ref["runtime_auto_extract"] = "true"
            refs.extend(batch_refs)
        run_repository.append_event(
            db,
            run_id=run_id,
            event_type="runtime_auto_extracted_jd_details",
            payload_json={
                "step_id": plan_step.step_id,
                "tool": "extract-observed-job-details-batch",
                "page_count": len(page_ids),
                "artifact_count": len(refs),
            },
        )
        return observations, refs

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
        """Finish a role-specific artifact from trusted candidates after a model stall."""
        if "resume-tailoring" in plan_step.allowed_skills:
            artifact_type = "resume_tailoring_brief"
            tool_name = "build-resume-tailoring-brief"
            keywords = self._goal_role_keywords(task.goal)
        elif "career-planning" in plan_step.allowed_skills:
            artifact_type = "career_preparation_plan"
            tool_name = "build-preparation-plan"
            keywords = self._goal_role_keywords(task.goal)
        else:
            return [], []
        if any(ref.get("artifact_type") == artifact_type for ref in artifact_refs):
            return [], []
        candidates = _structured_job_candidates(db, run_id)
        ranked: list[tuple[int, dict[str, Any]]] = []
        for candidate in candidates:
            source_quality = candidate.get("source_quality")
            if source_quality in {"list_only", "js_shell", "empty"}:
                continue
            searchable = "\n".join(
                str(candidate.get(key) or "")
                for key in ("title", "company_name", "responsibilities", "requirements")
            ).lower()
            score = sum(1 for keyword in keywords if keyword.lower() in searchable)
            if score:
                ranked.append((score, candidate))
        if not ranked or not tool_budget.try_consume():
            return [], []
        ranked.sort(
            key=lambda item: (
                -item[0],
                str(item[1].get("title") or ""),
                str(item[1].get("artifact_id") or ""),
            )
        )
        selected = ranked[0][1]
        artifact_id = selected.get("artifact_id")
        if not isinstance(artifact_id, str) or not artifact_id:
            return [], []
        target_keywords = [
            keyword
            for keyword in keywords
            if keyword.lower()
            in "\n".join(
                str(selected.get(key) or "")
                for key in ("title", "responsibilities", "requirements")
            ).lower()
        ] or ["岗位"]
        payload: dict[str, Any] = {
            "target_artifact_id": artifact_id,
        }
        if tool_name == "build-resume-tailoring-brief":
            payload["target_keywords"] = target_keywords
        else:
            payload["focus_keywords"] = target_keywords
        observation = self._executor.invoke_registered_tool(
            name=tool_name,
            context=self._tool_context(
                user_id=context.user_id,
                run_id=run_id,
                task=task,
                db=db,
            ),
            payload=payload,
        )
        if observation.status != "succeeded":
            return [observation], []
        auto_execution = ExecutorResult(
            status="succeeded",
            observations=[observation],
            summary="已基于目标角色匹配的公开 JD 生成职业交付物。",
        )
        refs = self._persist_observed_evidence(db, run_id, persisted_step, auto_execution)
        for ref in refs:
            ref["runtime_auto_deliverable"] = "true"
        return [observation], refs

    @staticmethod
    def _goal_role_keywords(goal: str) -> list[str]:
        lowered = goal.lower()
        if "产品经理" in lowered or "aigc" in lowered:
            return ["产品经理", "AIGC", "AI"]
        if "大模型应用开发" in lowered or "llm 应用" in lowered or "llm应用" in lowered:
            return ["大模型", "应用开发", "Agent", "AI"]
        if "前端开发" in lowered:
            return ["前端", "Frontend", "Vue"]
        if "java 后端" in lowered or "java后端" in lowered:
            return ["Java", "后端"]
        return ["岗位"]

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
    return None


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
                    "apply_url": (
                        candidate.get("apply_url")
                        if isinstance(candidate.get("apply_url"), str)
                        else None
                    ),
                    "content_hash": artifact.content_hash,
                    "source_quality": quality_by_source.get(source_url),
                    "title": title if isinstance(title, str) else None,
                    "locations": _string_list(candidate.get("locations")),
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
