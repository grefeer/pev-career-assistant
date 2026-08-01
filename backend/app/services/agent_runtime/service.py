"""User-scoped business service for the adaptive PEV runtime."""

from __future__ import annotations

from sqlalchemy.orm import Session

from backend.app.config import Settings
from backend.app.db.models import AgentEvent, AgentRun
from backend.app.repositories import agent_runtime as run_repository
from backend.app.services.agent_runtime.runtime import AgentRunResult, AgentRuntime
from backend.app.services.agent_runtime.schemas import AgentTaskRequest


class AgentRuntimeDisabledError(RuntimeError):
    """Raised when an API tries to invoke an opt-in harness while disabled."""


class AgentRuntimeUnavailableError(RuntimeError):
    """Raised when enabled configuration has no safely constructed live runtime."""


class AgentRunNotFoundError(LookupError):
    """Owner-scoped run lookup intentionally returned no record."""


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
        return self._runtime.run(db, user_id=user_id, task=task)

    def get_run(self, db: Session, *, user_id: str, run_id: str) -> AgentRun:
        """Read a trace summary only for its owner."""
        run = run_repository.get_run_for_owner(db, run_id, user_id)
        if run is None:
            raise AgentRunNotFoundError(run_id)
        return run

    def list_events(
        self, db: Session, *, user_id: str, run_id: str
    ) -> list[AgentEvent]:
        """Return a trace only after an owner-scoped existence check."""
        self.get_run(db, user_id=user_id, run_id=run_id)
        return run_repository.list_events(db, run_id)
