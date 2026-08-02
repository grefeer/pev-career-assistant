"""Branch-coverage closure for the adaptive PEV runtime.

Each test here targets a single partial branch reported as missing by
``pytest --cov=backend.app --cov-branch``:

* ``runtime.py:887->886`` - ``_skill_artifact_source_url`` skips a non-dict match.
* ``service.py:119->exit`` - queued-run failure audit is skipped when the row vanished.
* ``executor_agent.py:269->271`` - ``_page_for_decision`` keeps non-string visible_text.
* ``domain/agent_runtime.py:108->exit`` - ``require_valid_run_transition`` permits a valid edge.
"""

from __future__ import annotations

from backend.app.db.models import User, UserRole
from backend.app.domain.agent_runtime import RunStatus, require_valid_run_transition
from backend.app.repositories import agent_runtime as run_repository
from backend.app.services.agent_runtime.executor_agent import _page_for_decision
from backend.app.services.agent_runtime.runtime import _skill_artifact_source_url
from backend.app.services.agent_runtime.schemas import AgentTaskRequest
from backend.app.services.agent_runtime.service import AgentRunService
from tests.conftest import settings_override


def _user(user_id: str, account: str) -> User:
    return User(
        id=user_id,
        account=account,
        nickname=account,
        password_hash="not-a-real-password-hash",
        role=UserRole.STUDENT,
    )


def test_skill_artifact_source_url_skips_non_dict_match_entries() -> None:
    """A malformed (non-dict) match row must not abort source resolution."""
    source = _skill_artifact_source_url(
        "job_matching_report",
        {"matches": ["not-a-dict", {"source_url": "https://jobs.example/first"}]},
    )
    assert source == "https://jobs.example/first"


def test_page_for_decision_keeps_non_string_visible_text_unchanged() -> None:
    """A page without a string visible_text projects without truncation or crash."""
    projected = _page_for_decision({"visible_text": None, "source_url": "https://jobs.example/x"})
    assert projected["visible_text"] is None
    assert projected["source_url"] == "https://jobs.example/x"


def test_require_valid_run_transition_accepts_a_permitted_edge() -> None:
    """A permitted non-terminal transition returns silently instead of raising."""
    require_valid_run_transition(RunStatus.queued, RunStatus.running)
    require_valid_run_transition(RunStatus.running, RunStatus.waiting_user)


class _DisappearingQueuedRuntime:
    """Runtime double that hard-deletes the run row before failing.

    Simulates a concurrent deletion: the queued row is committed-gone by the time
    ``execute_queued_run`` rolls back and re-queries, so the failure audit is skipped.
    """

    def create_queued_run(self, db_session, *, user_id: str, task: AgentTaskRequest):
        return run_repository.create_run(
            db_session,
            user_id=user_id,
            goal=task.goal,
            allowed_skills=task.allowed_skills,
            context_summary=task.context,
            budget_json=task.budget.model_dump(mode="json"),
            agent_version="pev-test",
        )

    def run(self, db, *, user_id: str, task: AgentTaskRequest, existing_run=None):  # noqa: ANN001
        db.delete(existing_run)
        db.commit()
        raise RuntimeError("run vanished during execution")


def test_queued_execution_skips_failure_audit_when_run_row_disappeared(db_session) -> None:
    """If the run row is gone after a runtime error, no run_failed audit is written."""
    user = _user("user-a", "user-a@example.test")
    db_session.add(user)
    db_session.commit()
    user_id = user.id
    runtime = _DisappearingQueuedRuntime()
    service = AgentRunService(settings_override(agent_harness_enabled=True), runtime=runtime)
    queued = runtime.create_queued_run(
        db_session,
        user_id=user_id,
        task=AgentTaskRequest(goal="找岗位", allowed_skills=["job-discovery"]),
    )
    db_session.commit()
    run_id = queued.id

    service.execute_queued_run(lambda: db_session, user_id=user_id, run_id=run_id)

    # The run row was deleted by the runtime double, so the failure handler could
    # not find it to mark as failed or append a run_failed event.
    assert run_repository.get_run_for_owner(db_session, run_id, user_id) is None
    assert run_repository.list_events(db_session, run_id) == []
