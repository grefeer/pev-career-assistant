"""Thin lifecycle harness for autonomous Planner–Executor–Verifier runs."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import time

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
)
from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.agent_runtime.tool_budget import ToolCallBudget
from backend.app.services.agent_runtime.turn_budget import AgentTurnBudget
from backend.app.services.agent_runtime.verifier_agent import VerifierAgent


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
        context = self._tool_context(user_id=user_id, run_id=run.id, task=task)
        tool_budget = ToolCallBudget(
            task.budget.max_tool_calls, used=consumed_tool_calls
        )
        turn_budget = AgentTurnBudget(
            task.budget.max_agent_turns, used=consumed_turns
        )
        deadline = time.monotonic() + task.budget.max_wall_clock_seconds
        planning_task = task
        replans = 0
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
                    user_id=user_id, run_id=run.id, task=planning_task
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
                )
            except AgentModelGatewayError as error:
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
                return self._fail_step(
                    db,
                    run_id,
                    persisted_step,
                    error.code,
                    output_artifact_refs=execution.artifact_refs,
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
                    if verification.feedback:
                        feedback.append(verification.feedback)
                    retry_context["verifier_feedback"] = feedback
                    execution_task = execution_task.model_copy(
                        update={"context": retry_context}
                    )
                    execution_task = self._with_observed_public_evidence(
                        db, execution_task, run_id
                    )
                    context = self._tool_context(
                        user_id=context.user_id, run_id=run_id, task=execution_task
                    )
                    continue
                return self._fail_step(
                    db,
                    run_id,
                    persisted_step,
                    "executor_retry_budget_exhausted",
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

        def trace(role: AgentRole, decision_json: dict[str, str]) -> None:
            turn_indices[role] += 1
            run_repository.create_turn(
                db,
                run_id=run_id,
                role=role,
                turn_index=turn_indices[role],
                decision_json=decision_json,
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
        """Expose bounded, tool-produced public evidence to later Agent turns."""
        remaining_characters = 48_000
        evidence: list[dict[str, str]] = []
        for artifact in run_repository.list_evidence_artifacts(db, run_id):
            visible_text = artifact.content_json.get("visible_text")
            if not isinstance(visible_text, str) or not visible_text:
                continue
            text = visible_text[:remaining_characters]
            item = {
                "artifact_id": artifact.id,
                "source_url": artifact.source_url,
                "content_hash": artifact.content_hash,
                "visible_text": text,
            }
            title = artifact.content_json.get("title")
            if isinstance(title, str):
                item["title"] = title
            evidence.append(item)
            remaining_characters -= len(text)
            if remaining_characters <= 0:
                break
        context = dict(task.context)
        context["observed_public_evidence"] = evidence
        return task.model_copy(update={"context": context})

    @staticmethod
    def _tool_context(
        *, user_id: str, run_id: str, task: AgentTaskRequest
    ) -> ToolContext:
        """Project only verified public evidence into deterministic tool authority."""
        evidence = task.context.get("observed_public_evidence", [])
        profile_facts = task.private_context.get("confirmed_profile_facts", {})
        return ToolContext(
            user_id=user_id,
            run_id=run_id,
            metadata={
                "observed_public_evidence": evidence if isinstance(evidence, list) else [],
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
            run_repository.finish_run(
                db,
                run,
                status=RunStatus.waiting_user,
                final_summary=planner_result.user_question,
            )
            run_repository.append_event(
                db,
                run_id=run_id,
                event_type="planner_needs_user",
                payload_json={"question": planner_result.user_question},
            )
            return AgentRunResult(
                run_id, RunStatus.waiting_user, planner_result.user_question
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

    def _wait_for_user(
        self,
        db: Session,
        run_id: str,
        step: AgentStep,
        question: str | None,
        *,
        output_artifact_refs: list[dict[str, str]] | None = None,
    ) -> AgentRunResult:
        run = db.get(AgentRun, run_id)
        if run is None:  # defensive: foreign-key integrity normally prevents this.
            raise RuntimeError("Agent run disappeared during execution")
        run_repository.finish_step(
            db,
            step,
            status=StepStatus.failed,
            output_artifact_refs=output_artifact_refs,
            error_code="need_user",
        )
        run_repository.finish_run(
            db,
            run,
            status=RunStatus.waiting_user,
            final_summary=question,
        )
        run_repository.append_event(
            db,
            run_id=run_id,
            event_type="run_needs_user",
            payload_json={"question": question},
        )
        return AgentRunResult(run_id, RunStatus.waiting_user, question)

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
