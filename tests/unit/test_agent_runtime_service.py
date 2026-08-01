"""User-scoped application service behavior for persisted PEV runs."""

from __future__ import annotations

import pytest

from backend.app.db.models import User, UserRole
from backend.app.repositories import agent_runtime as run_repository
from backend.app.services.agent_runtime.service import (
    AgentRuntimeDisabledError,
    AgentRunNotFoundError,
    AgentRunService,
)
from tests.conftest import settings_override


def _user(user_id: str, account: str) -> User:
    return User(
        id=user_id,
        account=account,
        nickname=account,
        password_hash="not-a-real-password-hash",
        role=UserRole.STUDENT,
    )


def test_service_fails_closed_when_adaptive_harness_is_disabled(db_session) -> None:
    """Legacy deployments cannot accidentally activate a partly configured Agent."""
    service = AgentRunService(settings_override(agent_harness_enabled=False), runtime=None)

    with pytest.raises(AgentRuntimeDisabledError):
        service.create_run(db_session, user_id="user-a", task=None)


def test_service_hides_other_users_persisted_run_and_events(db_session) -> None:
    """A trace may reveal goals and artifacts, so service ownership is mandatory."""
    owner = _user("user-a", "user-a@example.test")
    other = _user("user-b", "user-b@example.test")
    db_session.add_all([owner, other])
    db_session.commit()
    run = run_repository.create_run(
        db_session,
        user_id=owner.id,
        goal="找 AI 应用开发岗位",
        allowed_skills=["job-discovery"],
        context_summary={},
        budget_json={},
        agent_version="pev-test",
    )
    run_repository.append_event(
        db_session,
        run_id=run.id,
        event_type="run_started",
        payload_json={},
    )
    service = AgentRunService(settings_override(agent_harness_enabled=True), runtime=None)

    with pytest.raises(AgentRunNotFoundError):
        service.get_run(db_session, user_id=other.id, run_id=run.id)
    with pytest.raises(AgentRunNotFoundError):
        service.list_events(db_session, user_id=other.id, run_id=run.id)
    assert service.get_run(db_session, user_id=owner.id, run_id=run.id).id == run.id
    assert len(service.list_events(db_session, user_id=owner.id, run_id=run.id)) == 1
