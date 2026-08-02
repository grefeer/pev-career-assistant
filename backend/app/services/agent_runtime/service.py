"""User-scoped business service for the adaptive PEV runtime."""

from __future__ import annotations

from sqlalchemy.orm import Session

from backend.app.config import Settings
from backend.app.db.models import AgentArtifact, AgentEvent, AgentPlan, AgentRun
from backend.app.domain.agent_runtime import RunStatus
from backend.app.repositories import agent_runtime as run_repository
from backend.app.repositories import profiles as profile_repository
from backend.app.services.agent_runtime.runtime import AgentRunResult, AgentRuntime
from backend.app.services.agent_runtime.schemas import AgentBudget, AgentTaskRequest


class AgentRuntimeDisabledError(RuntimeError):
    """Raised when an API tries to invoke an opt-in harness while disabled."""


class AgentRuntimeUnavailableError(RuntimeError):
    """Raised when enabled configuration has no safely constructed live runtime."""


class AgentRunNotFoundError(LookupError):
    """Owner-scoped run lookup intentionally returned no record."""


class AgentRunNotResumableError(RuntimeError):
    """Raised when a user tries to continue a non-paused Agent run."""


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
