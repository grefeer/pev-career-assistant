"""Thin lifecycle harness for autonomous Planner–Executor–Verifier runs."""

from __future__ import annotations

from dataclasses import dataclass

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
from backend.app.services.agent_runtime.planner_agent import PlannerAgent
from backend.app.services.agent_runtime.schemas import (
    AgentTaskRequest,
    ExecutionPlan,
    ExecutorResult,
    PlanStep,
    PlannerResult,
)
from backend.app.services.agent_runtime.tool_context import ToolContext
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

    def run(self, db: Session, *, user_id: str, task: AgentTaskRequest) -> AgentRunResult:
        """Run bounded PEV lifecycle; agents retain all semantic tool decisions."""
        run = run_repository.create_run(
            db,
            user_id=user_id,
            goal=task.goal,
            allowed_skills=task.allowed_skills,
            context_summary=task.context,
            budget_json=task.budget.model_dump(mode="json"),
            agent_version=self._agent_version,
        )
        context = ToolContext(user_id=user_id, run_id=run.id)
        run_repository.start_run(db, run)
        run_repository.append_event(
            db,
            run_id=run.id,
            event_type="run_started",
            payload_json={"agent_version": self._agent_version},
        )
        planning_task = task
        revision = 0
        replans = 0
        trace = self._build_decision_trace(db, run.id)
        while True:
            planner_result = self._planner.run(
                task=planning_task, context=context, trace=trace
            )
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
                outcome = self._run_step(
                    db=db,
                    run_id=run.id,
                    task=planning_task,
                    plan=plan,
                    plan_step=plan_step,
                    persisted_step=step,
                    context=context,
                    trace=trace,
                )
                if outcome.error_code == "replan_required":
                    replan_feedback = outcome.summary
                    break
                if outcome.status is not RunStatus.running:
                    return outcome
                final_summary = outcome.summary or final_summary
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
                feedback_context = dict(task.context)
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
    ) -> AgentRunResult:
        """Execute and conditionally verify one agent-defined planned outcome."""
        retries = 0
        execution_task = task
        while True:
            execution = self._executor.run(
                task=execution_task,
                plan=plan,
                step=plan_step,
                context=context,
                trace=trace,
            )
            if execution.status == "needs_user":
                return self._wait_for_user(
                    db, run_id, persisted_step, execution.user_question
                )
            if execution.status != "succeeded":
                return self._fail_step(db, run_id, persisted_step, "executor_failed")
            execution = execution.model_copy(
                update={
                    "artifact_refs": self._persist_observed_evidence(
                        db, run_id, persisted_step, execution
                    )
                }
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
            verification = self._verifier.run(
                task=task,
                plan=plan,
                step=plan_step,
                execution=execution,
                context=context,
                trace=trace,
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
                    retry_context = dict(task.context)
                    feedback = list(retry_context.get("verifier_feedback", []))
                    if verification.feedback:
                        feedback.append(verification.feedback)
                    retry_context["verifier_feedback"] = feedback
                    execution_task = task.model_copy(
                        update={"context": retry_context}
                    )
                    continue
                return self._fail_step(
                    db, run_id, persisted_step, "executor_retry_budget_exhausted"
                )
            if verification.decision is VerificationDecision.NEED_USER:
                return self._wait_for_user(
                    db, run_id, persisted_step, verification.feedback
                )
            run_repository.finish_step(
                db,
                persisted_step,
                status=StepStatus.skipped,
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
    def _build_decision_trace(db: Session, run_id: str):
        """Return a run-local, role-indexed callback for safe decision summaries."""
        turn_indices = {role: 0 for role in AgentRole}

        def trace(role: AgentRole, decision_json: dict[str, str]) -> None:
            turn_indices[role] += 1
            run_repository.create_turn(
                db,
                run_id=run_id,
                role=role,
                turn_index=turn_indices[role],
                decision_json=decision_json,
            )

        return trace

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
            source_url = output.get("source_url")
            content_hash = output.get("content_hash")
            visible_text = output.get("visible_text")
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
                    "title": output.get("title"),
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
        return artifact_refs

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
        run_repository.finish_run(
            db, run, status=RunStatus.failed, error_code="planner_failed"
        )
        run_repository.append_event(
            db,
            run_id=run_id,
            event_type="run_failed",
            payload_json={"error_code": "planner_failed"},
        )
        return AgentRunResult(run_id, RunStatus.failed, None, "planner_failed")

    def _wait_for_user(
        self,
        db: Session,
        run_id: str,
        step: AgentStep,
        question: str | None,
    ) -> AgentRunResult:
        run = db.get(AgentRun, run_id)
        if run is None:  # defensive: foreign-key integrity normally prevents this.
            raise RuntimeError("Agent run disappeared during execution")
        run_repository.finish_step(db, step, status=StepStatus.failed, error_code="need_user")
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
        self, db: Session, run_id: str, step: AgentStep, error_code: str
    ) -> AgentRunResult:
        run = db.get(AgentRun, run_id)
        if run is None:  # defensive: foreign-key integrity normally prevents this.
            raise RuntimeError("Agent run disappeared during execution")
        run_repository.finish_step(
            db, step, status=StepStatus.failed, error_code=error_code
        )
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
