"""SQLite-backed persistence behavior for append-only PEV execution traces."""

from __future__ import annotations

from backend.app.domain.agent_runtime import AgentRole, ComplexityLevel, RunStatus
from backend.app.db.models import User, UserRole
from backend.app.repositories import agent_runtime


def _user(user_id: str, account: str) -> User:
    return User(
        id=user_id,
        account=account,
        nickname=account,
        password_hash="not-a-real-password-hash",
        role=UserRole.STUDENT,
    )


def test_repository_persists_ordered_plan_turn_and_event_trace(db_session) -> None:
    """A completed run remains explainable through ordered durable evidence."""
    user = _user("user-a", "user-a@example.test")
    db_session.add(user)
    db_session.commit()

    run = agent_runtime.create_run(
        db_session,
        user_id=user.id,
        goal="提取 AI Agent 岗位",
        allowed_skills=["job-discovery"],
        context_summary={"preference_version": 1},
        budget_json={"max_agent_turns": 8},
        agent_version="pev-1",
    )
    plan = agent_runtime.create_plan(
        db_session,
        run_id=run.id,
        revision=1,
        complexity=ComplexityLevel.L2,
        plan_json={"steps": ["discover"]},
    )
    step = agent_runtime.create_step(
        db_session,
        run_id=run.id,
        plan_id=plan.id,
        sequence=1,
        objective="提取公开 JD",
        allowed_skills=["job-discovery"],
    )
    turn = agent_runtime.create_turn(
        db_session,
        run_id=run.id,
        role=AgentRole.executor,
        turn_index=1,
        decision_json={"action": "call_tool"},
    )
    first = agent_runtime.append_event(
        db_session,
        run_id=run.id,
        event_type="tool_call_started",
        payload_json={"tool": "fetch_public_url"},
    )
    second = agent_runtime.append_event(
        db_session,
        run_id=run.id,
        event_type="tool_call_finished",
        payload_json={"artifact_ref": "artifact://job/1"},
    )
    db_session.commit()

    assert run.status is RunStatus.queued
    assert plan.revision == 1
    assert step.sequence == 1
    assert turn.role is AgentRole.executor
    assert (first.sequence, second.sequence) == (1, 2)
    assert [event.event_type for event in agent_runtime.list_events(db_session, run.id)] == [
        "tool_call_started",
        "tool_call_finished",
    ]


def test_repository_owner_lookup_does_not_leak_another_users_run(db_session) -> None:
    """Owner filtering is a repository boundary, not an optional route choice."""
    first_user = _user("user-a", "user-a@example.test")
    second_user = _user("user-b", "user-b@example.test")
    db_session.add_all([first_user, second_user])
    db_session.commit()
    run = agent_runtime.create_run(
        db_session,
        user_id=first_user.id,
        goal="找岗位",
        allowed_skills=["job-discovery"],
        context_summary={},
        budget_json={},
        agent_version="pev-1",
    )

    assert agent_runtime.get_run_for_owner(db_session, run.id, second_user.id) is None
    assert agent_runtime.get_run_for_owner(db_session, run.id, first_user.id) is not None
