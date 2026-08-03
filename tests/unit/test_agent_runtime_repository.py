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


def test_repository_list_events_filters_by_sequence_cursor(db_session) -> None:
    """An SSE poll cursor returns only events appended after it, in order."""
    user = _user("user-a", "user-a@example.test")
    db_session.add(user)
    db_session.commit()
    run = agent_runtime.create_run(
        db_session, user_id=user.id, goal="找岗位", allowed_skills=["job-discovery"],
        context_summary={}, budget_json={}, agent_version="pev-test",
    )
    agent_runtime.append_event(
        db_session, run_id=run.id, event_type="run_started", payload_json={},
    )
    agent_runtime.append_event(
        db_session, run_id=run.id, event_type="plan_created", payload_json={},
    )
    agent_runtime.append_event(
        db_session, run_id=run.id, event_type="step_started", payload_json={},
    )
    db_session.commit()

    # default cursor (0) replays the whole trace; a durable cursor returns only
    # events appended after it, backed by ix_agent_events_run_sequence.
    assert [e.sequence for e in agent_runtime.list_events(db_session, run.id)] == [1, 2, 3]
    assert [e.sequence for e in agent_runtime.list_events(db_session, run.id, after_sequence=1)] == [2, 3]
    assert [e.sequence for e in agent_runtime.list_events(db_session, run.id, after_sequence=2)] == [3]
    assert agent_runtime.list_events(db_session, run.id, after_sequence=3) == []


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


def test_repository_lists_only_owner_runs_in_newest_first_order(db_session) -> None:
    """The workspace history never includes another user's potentially sensitive goal."""
    first_user = _user("user-a", "user-a@example.test")
    second_user = _user("user-b", "user-b@example.test")
    db_session.add_all([first_user, second_user])
    db_session.commit()
    oldest = agent_runtime.create_run(
        db_session, user_id=first_user.id, goal="旧任务", allowed_skills=["job-discovery"],
        context_summary={}, budget_json={}, agent_version="pev-1",
    )
    newest = agent_runtime.create_run(
        db_session, user_id=first_user.id, goal="新任务", allowed_skills=["job-discovery"],
        context_summary={}, budget_json={}, agent_version="pev-1",
    )
    agent_runtime.create_run(
        db_session, user_id=second_user.id, goal="其他用户任务", allowed_skills=["job-discovery"],
        context_summary={}, budget_json={}, agent_version="pev-1",
    )

    runs = agent_runtime.list_runs_for_owner(db_session, first_user.id, limit=20)

    assert [run.id for run in runs] == [newest.id, oldest.id]


def test_repository_lists_plan_revisions_in_execution_order(db_session) -> None:
    user = _user("user-a", "user-a@example.test")
    db_session.add(user)
    db_session.commit()
    run = agent_runtime.create_run(
        db_session, user_id=user.id, goal="找岗位", allowed_skills=["job-discovery"],
        context_summary={}, budget_json={}, agent_version="pev-test",
    )
    first = agent_runtime.create_plan(
        db_session, run_id=run.id, revision=1, complexity=ComplexityLevel.L2,
        plan_json={"goal": "找岗位", "steps": [{"id": "discover"}]},
    )
    second = agent_runtime.create_plan(
        db_session, run_id=run.id, revision=2, complexity=ComplexityLevel.L3,
        plan_json={"goal": "找岗位", "steps": [{"id": "match"}]},
    )

    assert [plan.id for plan in agent_runtime.list_plans(db_session, run.id)] == [first.id, second.id]


def test_repository_keeps_distinct_artifact_types_for_one_source_and_deduplicates_each_type(db_session) -> None:
    """A page snapshot and its parsed JD share a hash but are distinct immutable products."""
    user = _user("user-a", "user-a@example.test")
    db_session.add(user)
    db_session.commit()
    run = agent_runtime.create_run(
        db_session, user_id=user.id, goal="提取 JD", allowed_skills=["job-discovery"],
        context_summary={}, budget_json={}, agent_version="pev-test",
    )
    plan = agent_runtime.create_plan(
        db_session, run_id=run.id, revision=1, complexity=ComplexityLevel.L2,
        plan_json={"steps": ["discover"]},
    )
    step = agent_runtime.create_step(
        db_session, run_id=run.id, plan_id=plan.id, sequence=1,
        objective="提取 JD", allowed_skills=["job-discovery"],
    )
    page = agent_runtime.create_artifact(
        db_session, run_id=run.id, step_id=step.id, artifact_type="public_job_page",
        source_url="https://jobs.example/1", content_hash="a" * 64,
        content_json={"visible_text": "JD"},
    )
    structured = agent_runtime.create_artifact(
        db_session, run_id=run.id, step_id=step.id, artifact_type="structured_job_details",
        source_url="https://jobs.example/1", content_hash="a" * 64,
        content_json={"candidates": []},
    )
    repeated = agent_runtime.create_artifact(
        db_session, run_id=run.id, step_id=step.id, artifact_type="structured_job_details",
        source_url="https://jobs.example/1", content_hash="a" * 64,
        content_json={"candidates": []},
    )

    assert page.id != structured.id
    assert repeated.id == structured.id
