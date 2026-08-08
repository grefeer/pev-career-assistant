"""Thin lifecycle harness for autonomous Planner–Executor–Verifier runs."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import time
from typing import Any

from sqlalchemy.orm import Session

from backend.app.db.models import AgentRun, AgentStep
from backend.app.domain.agent_runtime import (
    AgentRole,
    RunStatus,
    StepStatus,
    VerificationDecision,
)
from backend.app.repositories import agent_runtime as run_repository
from backend.app.services.agent_runtime.executor_agent import ExecutorAgent
from backend.app.services.agent_runtime.model_gateway import AgentModelGatewayError
from backend.app.services.agent_runtime.planner_agent import PlannerAgent
from backend.app.services.agent_runtime.schemas import (
    AgentTaskRequest,
    ExecutionPlan,
    ExecutorResult,
    PlanStep,
    PlannerResult,
    VerifierResult,
)
from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.agent_runtime.tool_budget import ToolCallBudget
from backend.app.services.agent_runtime.turn_budget import AgentTurnBudget
from backend.app.services.agent_runtime.verifier_agent import VerifierAgent

#: Character budget for the ``observed_public_evidence`` context supplied to
#: later Agent turns. The most-recent artifacts are kept full (with bounded
#: ``visible_text``); older artifacts that exceed this budget collapse to
#: identifier-only summary lines so early-link evidence is preserved as a
#: pointer rather than silently dropped.
_EVIDENCE_BUDGET_CHARS = 48_000

#: Per-candidate section truncation when projecting extract outputs into the
#: tool context: keyword scoring needs representative text, not full JD text.
_STRUCTURED_SECTION_CHARS = 600


@dataclass(frozen=True)
class AgentRunResult:
    """Safe terminal or waiting summary returned by the application service."""

    run_id: str
    status: RunStatus
    summary: str | None
    error_code: str | None = None


class AgentRuntime:
    """Schedules agent-produced decisions while enforcing only hard lifecycle bounds."""

    def __init__(
        self,
        *,
        planner: PlannerAgent,
        executor: ExecutorAgent,
        verifier: VerifierAgent,
        agent_version: str,
    ) -> None:
        self._planner = planner
        self._executor = executor
        self._verifier = verifier
        self._agent_version = agent_version

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
        replans = max(0, revision - 1)
        trace = self._build_decision_trace(
            db,
            run.id,
            initial_turn_indices=run_repository.turn_indices_by_role(db, run.id),
        )
        while True:
            try:
                planner_result = self._planner.run(
                    task=planning_task,
                    context=context,
                    trace=trace,
                    tool_budget=tool_budget,
                    turn_budget=turn_budget,
                    deadline=deadline,
                )
            except AgentModelGatewayError as error:
                if error.code == "invalid_model_response":
                    return self._finish_planner_invalid_model(db, run.id, run)
                return self._fail_run(db, run.id, error.code)
            if planner_result.status != "planned" or planner_result.plan is None:
                return self._finish_planner_non_plan(db, run.id, run, planner_result)

            plan = planner_result.plan
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
                outcome = self._run_step(
                    db=db,
                    run_id=run.id,
                    task=planning_task,
                    plan=plan,
                    plan_step=plan_step,
                    persisted_step=step,
                    context=context,
                    trace=trace,
                    tool_budget=tool_budget,
                    turn_budget=turn_budget,
                    deadline=deadline,
                )
                if outcome.error_code == "replan_required":
                    replan_feedback = outcome.summary
                    break
                if outcome.status is not RunStatus.running:
                    return outcome
                final_summary = outcome.summary or final_summary
                planning_task = self._with_observed_public_evidence(
                    db, planning_task, run.id
                )
                context = self._tool_context(
                    user_id=user_id, run_id=run.id, task=planning_task, db=db
                )
            if replan_feedback is not None:
                replans += 1
                if replans > task.budget.max_replans:
                    run_repository.finish_run(
                        db,
                        run,
                        status=RunStatus.failed,
                        error_code="replan_budget_exhausted",
                    )
                    run_repository.append_event(
                        db,
                        run_id=run.id,
                        event_type="run_failed",
                        payload_json={"error_code": "replan_budget_exhausted"},
                    )
                    return AgentRunResult(
                        run.id, RunStatus.failed, None, "replan_budget_exhausted"
                    )
                feedback_context = dict(planning_task.context)
                feedback = list(feedback_context.get("verifier_feedback", []))
                feedback.append(replan_feedback)
                feedback_context["verifier_feedback"] = feedback
                planning_task = task.model_copy(update={"context": feedback_context})
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
        run = run_repository.get_run_for_owner(db, run_id, user_id)
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
        run = run_repository.get_run_for_owner(db, run_id, user_id)
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
        deadline: float | None = None,
    ) -> AgentRunResult:
        """Execute and conditionally verify one agent-defined planned outcome."""
        retries = 0
        execution_task = task
        prior_observations = []
        prior_artifact_refs: list[dict[str, str]] = []
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
            # A verifier retry continues the same planned outcome. Keep prior
            # tool-backed observations for independent verification, but persist
            # only the new observation set from this Executor invocation.
            execution = execution.model_copy(
                update={
                    "observations": [*prior_observations, *execution.observations],
                    "artifact_refs": [*prior_artifact_refs, *observed_artifact_refs],
                }
            )
            if execution.status == "needs_user":
                return self._wait_for_user(
                    db,
                    run_id,
                    persisted_step,
                    execution.user_question,
                    output_artifact_refs=observed_artifact_refs,
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
            if execution.status != "succeeded":
                return self._fail_step(
                    db,
                    run_id,
                    persisted_step,
                    execution.error_code or "executor_failed",
                    output_artifact_refs=observed_artifact_refs,
                )
            if not self._requires_verification(plan, plan_step):
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
                        update={"context": retry_context}
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
                # would only burn turns on a stuck loop, so hand the step to
                # the human. The run stays recoverable (waiting_user -> resume)
                # instead of failing outright, and the verifier feedback tells
                # the human what the agents could not reconcile.
                return self._wait_for_user(
                    db,
                    run_id,
                    persisted_step,
                    "多次重试后仍未通过核验：" + verification.feedback + "。请人工确认产出，或补充缺失信息后重试。",
                    output_artifact_refs=execution.artifact_refs,
                )
            if verification.decision is VerificationDecision.NEED_USER:
                return self._wait_for_user(
                    db,
                    run_id,
                    persisted_step,
                    verification.feedback,
                    output_artifact_refs=execution.artifact_refs,
                )
            run_repository.finish_step(
                db,
                persisted_step,
                status=StepStatus.skipped,
                output_artifact_refs=execution.artifact_refs,
                error_code="replan_required",
            )
            run_repository.append_event(
                db,
                run_id=run_id,
                event_type="verification_replan",
                payload_json={
                    "sequence": persisted_step.sequence,
                    "feedback": verification.feedback,
                },
            )
            return AgentRunResult(
                run_id,
                RunStatus.running,
                verification.feedback,
                "replan_required",
            )

    @staticmethod
    def _requires_verification(plan: ExecutionPlan, plan_step: PlanStep) -> bool:
        return plan_step.requires_verification or plan.complexity.value in {"L3", "L4"}

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
            decision_json: dict[str, str],
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
        """Expose bounded, tool-produced public evidence to later Agent turns.

        Most-recent artifacts are kept full (with bounded ``visible_text``);
        older artifacts that do not fit the character budget collapse to
        identifier-only summary lines (``artifact_id``/``source_url``/
        ``content_hash``/``title`` - never ``visible_text``), so early-link
        evidence is preserved as a traceable pointer rather than silently
        dropped. Total ``visible_text`` characters stay within ``_EVIDENCE_BUDGET_CHARS``.

        ``list_evidence_artifacts`` returns oldest-first (production order); we
        walk newest-to-oldest when assigning the character budget so the
        freshest evidence - most relevant to the current decision - is kept
        full, and the oldest evidence is the first to be summarized.
        """
        budget_chars = _EVIDENCE_BUDGET_CHARS
        # Build candidate items in oldest-first order (production order) with
        # full visible_text. Artifacts without a non-empty visible_text are
        # skipped - they carry no page evidence the model can act on.
        candidates: list[dict[str, str]] = []
        for artifact in run_repository.list_evidence_artifacts(db, run_id):
            visible_text = artifact.content_json.get("visible_text")
            if not isinstance(visible_text, str) or not visible_text:
                continue
            item: dict[str, str] = {
                "artifact_id": artifact.id,
                "source_url": artifact.source_url,
                "content_hash": artifact.content_hash,
                "visible_text": visible_text,
            }
            title = artifact.content_json.get("title")
            if isinstance(title, str):
                item["title"] = title
            candidates.append(item)
        # Walk newest-to-oldest, assigning truncated visible_text to items
        # that fit within the remaining budget. Items that don't fit (budget
        # exhausted) become identifier-only summary lines. This preserves the
        # most-recent evidence full while keeping older evidence as pointers.
        full_visible_text: dict[int, str] = {}
        remaining = budget_chars
        for index in range(len(candidates) - 1, -1, -1):
            text = candidates[index]["visible_text"]
            truncated = text[:remaining]
            if not truncated:
                break  # Budget exhausted; this and all older items get summarized.
            full_visible_text[index] = truncated
            remaining -= len(truncated)
        # Assemble the result in oldest-first order: summaries for older
        # artifacts, full items (with bounded visible_text) for recent ones.
        evidence: list[dict[str, str]] = []
        for index, item in enumerate(candidates):
            if index in full_visible_text:
                full_item = dict(item)
                full_item["visible_text"] = full_visible_text[index]
                evidence.append(full_item)
            else:
                summary: dict[str, str] = {
                    "artifact_id": item["artifact_id"],
                    "source_url": item["source_url"],
                    "content_hash": item["content_hash"],
                }
                if "title" in item:
                    summary["title"] = item["title"]
                evidence.append(summary)
        context = dict(task.context)
        context["observed_public_evidence"] = evidence
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
        evidence = task.context.get("observed_public_evidence", [])
        profile_facts = task.private_context.get("confirmed_profile_facts", {})
        return ToolContext(
            user_id=user_id,
            run_id=run_id,
            metadata={
                "observed_public_evidence": evidence if isinstance(evidence, list) else [],
                "structured_job_candidates": _structured_job_candidates(db, run_id),
                "confirmed_profile_facts": profile_facts
                if isinstance(profile_facts, dict)
                else {},
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
                        "visible_text": visible_text,
                    },
                )
                artifact_ref = {
                    "artifact_id": artifact.id,
                    "source_url": source_url,
                    "content_hash": content_hash,
                }
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
            results = output.get("results")
            if not (
                isinstance(source_url, str)
                and isinstance(content_hash, str)
                and isinstance(results, list)
            ):
                continue
            artifact = run_repository.create_artifact(
                db,
                run_id=run_id,
                step_id=step.id,
                artifact_type="job_search_results",
                source_url=source_url,
                content_hash=content_hash,
                content_json={"query": output.get("query"), "results": results},
            )
            artifact_refs.append(
                {
                    "artifact_id": artifact.id,
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
                artifact_refs.append(
                    {
                        "artifact_id": artifact.id,
                        "source_url": source_url,
                        "content_hash": content_hash,
                    }
                )
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
            return self._finish_planner_waiting(
                db,
                run_id,
                run,
                planner_result.user_question,
                event_type="planner_needs_user",
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
            payload_json={"error_code": error_code},
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
            payload_json={"question": question},
        )
        return AgentRunResult(run_id, RunStatus.waiting_user, question, error_code)

    def _wait_for_user(
        self,
        db: Session,
        run_id: str,
        step: AgentStep,
        question: str | None,
        *,
        output_artifact_refs: list[dict[str, str]] | None = None,
        error_code: str | None = None,
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
            payload_json={"question": question},
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
            payload_json={"error_code": error_code},
        )
        return AgentRunResult(run_id, RunStatus.failed, None, error_code)


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
    for artifact in run_repository.list_evidence_artifacts(db, run_id):
        raw_candidates = artifact.content_json.get("candidates")
        if not isinstance(raw_candidates, list):
            continue
        for candidate in raw_candidates:
            if not isinstance(candidate, dict):
                continue
            source_url = candidate.get("apply_url")
            if not isinstance(source_url, str) or not source_url:
                source_url = artifact.source_url
            title = candidate.get("title")
            items.append(
                {
                    "artifact_id": artifact.id,
                    "source_url": source_url,
                    "content_hash": artifact.content_hash,
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
                }
            )
    return items


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
