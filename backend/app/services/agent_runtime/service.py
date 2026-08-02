"""User-scoped business service for the adaptive PEV runtime."""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from backend.app.config import Settings
from backend.app.db.models import AgentArtifact, AgentEvent, AgentPlan, AgentRun
from backend.app.domain.agent_runtime import RunStatus
from backend.app.repositories import agent_runtime as run_repository
from backend.app.repositories import profiles as profile_repository
from backend.app.services.agent_runtime.runtime import AgentRunResult, AgentRuntime
from backend.app.services.agent_runtime.schemas import AgentBudget, AgentTaskRequest


logger = logging.getLogger(__name__)


class AgentRuntimeDisabledError(RuntimeError):
    """Raised when an API tries to invoke an opt-in harness while disabled."""


class AgentRuntimeUnavailableError(RuntimeError):
    """Raised when enabled configuration has no safely constructed live runtime."""


class AgentRunNotFoundError(LookupError):
    """Owner-scoped run lookup intentionally returned no record."""


class AgentRunNotResumableError(RuntimeError):
    """Raised when a user tries to continue a non-paused Agent run."""


def build_adaptive_agent_budget(settings: Settings, allowed_skills: list[str]) -> AgentBudget:
    """Set a hard, request-scoped ceiling without prescribing Agent actions.

    A single-skill question keeps the configured fast-path ceiling.  Every
    additional permitted Skill can require an execution decision plus an
    independent verification/retry decision, so a multi-deliverable request is
    given room to finish rather than being incorrectly cut off mid-workflow.
    The Planner, Executor and Verifier still choose every actual turn.
    """
    unique_skill_count = len(set(allowed_skills))
    adaptive_turn_ceiling = settings.agent_harness_max_agent_turns + 8 * max(
        0, unique_skill_count - 1
    )
    return AgentBudget(
        max_agent_turns=min(100, adaptive_turn_ceiling),
        max_tool_calls=settings.agent_harness_max_tool_calls,
        max_replans=settings.agent_harness_max_replans,
    )


class AgentRunService:
    """Apply feature gating and ownership around the lower-level PEV runtime."""

    def __init__(self, settings: Settings, *, runtime: AgentRuntime | None) -> None:
        self._settings = settings
        self._runtime = runtime

    def create_run(
        self,
        db: Session,
        *,
        user_id: str,
        task: AgentTaskRequest,
    ) -> AgentRunResult:
        """Execute a bounded authenticated PEV request through the live runtime."""
        if not self._settings.agent_harness_enabled:
            raise AgentRuntimeDisabledError("agent_harness_disabled")
        if self._runtime is None:
            raise AgentRuntimeUnavailableError("agent_harness_unavailable")
        return self._runtime.run(
            db,
            user_id=user_id,
            task=self._with_confirmed_profile_facts(db, user_id=user_id, task=task),
        )

    def queue_run(
        self, db: Session, *, user_id: str, task: AgentTaskRequest
    ) -> AgentRunResult:
        """Create a durable queued run before returning its SSE address to the browser."""
        if not self._settings.agent_harness_enabled:
            raise AgentRuntimeDisabledError("agent_harness_disabled")
        if self._runtime is None:
            raise AgentRuntimeUnavailableError("agent_harness_unavailable")
        run = self._runtime.create_queued_run(db, user_id=user_id, task=task)
        return AgentRunResult(run.id, RunStatus.queued, None)

    def execute_queued_run(self, session_factory, *, user_id: str, run_id: str) -> None:
        """Run a previously committed queue item in an isolated request-free session."""
        if self._runtime is None:
            return
        with session_factory() as db:
            run = run_repository.get_run_for_owner(db, run_id, user_id)
            if run is None or run.status is not RunStatus.queued:
                return
            task = AgentTaskRequest(
                goal=run.goal,
                allowed_skills=run.allowed_skills_json,
                context=run.context_summary_json,
                budget=AgentBudget.model_validate(run.budget_json),
            )
            try:
                self._runtime.run(
                    db,
                    user_id=user_id,
                    task=self._with_confirmed_profile_facts(db, user_id=user_id, task=task),
                    existing_run=run,
                )
                db.commit()
            except Exception:
                logger.exception("queued PEV run failed", extra={"run_id": run_id})
                db.rollback()
                run = run_repository.get_run_for_owner(db, run_id, user_id)
                if run is not None:
                    run_repository.finish_run(db, run, status=RunStatus.failed, error_code="runtime_error")
                    run_repository.append_event(
                        db, run_id=run.id, event_type="run_failed", payload_json={"error_code": "runtime_error"}
                    )
                    db.commit()

    @staticmethod
    def _with_confirmed_profile_facts(
        db: Session, *, user_id: str, task: AgentTaskRequest
    ) -> AgentTaskRequest:
        """Supply only owner-confirmed profile facts as non-persisted run context."""
        versions = profile_repository.list_versions(db, user_id)
        if not versions:
            return task
        private_context = dict(task.private_context)
        private_context["confirmed_profile_facts"] = versions[0].facts_snapshot
        return task.model_copy(update={"private_context": private_context})

    def get_run(self, db: Session, *, user_id: str, run_id: str) -> AgentRun:
        """Read a trace summary only for its owner."""
        run = run_repository.get_run_for_owner(db, run_id, user_id)
        if run is None:
            raise AgentRunNotFoundError(run_id)
        return run

    def resume_run(
        self,
        db: Session,
        *,
        user_id: str,
        run_id: str,
        user_response: str,
    ) -> AgentRunResult:
        """Add a human reply and resume exactly one owner-scoped paused Run."""
        if not self._settings.agent_harness_enabled:
            raise AgentRuntimeDisabledError("agent_harness_disabled")
        if self._runtime is None:
            raise AgentRuntimeUnavailableError("agent_harness_unavailable")
        run = self.get_run(db, user_id=user_id, run_id=run_id)
        if run.status is not RunStatus.waiting_user:
            raise AgentRunNotResumableError("agent_run_not_waiting_user")
        cleaned_response = user_response.strip()
        if not cleaned_response:
            raise ValueError("user_response_empty")
        context = run_repository.append_user_response(db, run, cleaned_response)
        task = AgentTaskRequest(
            goal=run.goal,
            allowed_skills=run.allowed_skills_json,
            context=context,
            budget=AgentBudget.model_validate(run.budget_json),
        )
        return self._runtime.resume(
            db,
            user_id=user_id,
            run_id=run.id,
            task=self._with_confirmed_profile_facts(db, user_id=user_id, task=task),
        )

    def recover_run(
        self, db: Session, *, user_id: str, run_id: str
    ) -> AgentRunResult:
        """Restart a nonterminal interrupted run from durable context and budgets only."""
        if not self._settings.agent_harness_enabled:
            raise AgentRuntimeDisabledError("agent_harness_disabled")
        if self._runtime is None:
            raise AgentRuntimeUnavailableError("agent_harness_unavailable")
        run = self.get_run(db, user_id=user_id, run_id=run_id)
        if run.status is not RunStatus.running:
            raise AgentRunNotResumableError("agent_run_not_running")
        task = AgentTaskRequest(
            goal=run.goal,
            allowed_skills=run.allowed_skills_json,
            context=run.context_summary_json,
            budget=AgentBudget.model_validate(run.budget_json),
        )
        return self._runtime.recover(
            db,
            user_id=user_id,
            run_id=run.id,
            task=self._with_confirmed_profile_facts(db, user_id=user_id, task=task),
        )

    def list_runs(self, db: Session, *, user_id: str, limit: int) -> list[AgentRun]:
        """List recent task summaries within the requesting user's ownership boundary."""
        return run_repository.list_runs_for_owner(db, user_id, limit=limit)

    def list_events(
        self, db: Session, *, user_id: str, run_id: str
    ) -> list[AgentEvent]:
        """Return a trace only after an owner-scoped existence check."""
        self.get_run(db, user_id=user_id, run_id=run_id)
        return run_repository.list_events(db, run_id)

    def list_plans(
        self, db: Session, *, user_id: str, run_id: str
    ) -> list[AgentPlan]:
        """Return Planner revisions only after checking the requesting owner."""
        self.get_run(db, user_id=user_id, run_id=run_id)
        return run_repository.list_plans(db, run_id)

    def list_artifacts(
        self, db: Session, *, user_id: str, run_id: str
    ) -> list[AgentArtifact]:
        """Return immutable artifacts only after verifying the run owner."""
        self.get_run(db, user_id=user_id, run_id=run_id)
        return run_repository.list_evidence_artifacts(db, run_id)
