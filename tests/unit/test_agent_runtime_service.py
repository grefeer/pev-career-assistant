"""User-scoped application service behavior for persisted PEV runs."""

from __future__ import annotations

import pytest

from backend.app.db.models import ConfirmedProfileVersion, Profile, User, UserRole
from backend.app.domain.agent_runtime import ComplexityLevel, RunStatus
from backend.app.repositories import agent_runtime as run_repository
from backend.app.services.agent_runtime.runtime import AgentRunResult
from backend.app.services.agent_runtime.schemas import AgentTaskRequest
from backend.app.services.agent_runtime.service import (
    AgentRuntimeDisabledError,
    AgentRunNotFoundError,
    AgentRunNotResumableError,
    AgentRuntimeUnavailableError,
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


class CapturingRuntime:
    """Runtime boundary double that exposes the task passed by the service."""

    def __init__(self) -> None:
        self.task: AgentTaskRequest | None = None

    def run(self, _db, *, user_id: str, task: AgentTaskRequest) -> AgentRunResult:
        self.task = task
        return AgentRunResult("run-a", status=RunStatus.succeeded, summary=user_id)

    def resume(
        self, _db, *, user_id: str, run_id: str, task: AgentTaskRequest
    ) -> AgentRunResult:
        self.task = task
        return AgentRunResult(run_id, status=RunStatus.succeeded, summary=user_id)

    def recover(
        self, _db, *, user_id: str, run_id: str, task: AgentTaskRequest
    ) -> AgentRunResult:
        self.task = task
        return AgentRunResult(run_id, status=RunStatus.succeeded, summary=user_id)


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


def test_service_lists_only_the_callers_persisted_runs(db_session) -> None:
    """The personal task workspace receives no cross-user run history."""
    owner = _user("user-a", "user-a@example.test")
    other = _user("user-b", "user-b@example.test")
    db_session.add_all([owner, other])
    db_session.commit()
    run_repository.create_run(
        db_session, user_id=owner.id, goal="我的任务", allowed_skills=["job-discovery"],
        context_summary={}, budget_json={}, agent_version="pev-test",
    )
    run_repository.create_run(
        db_session, user_id=other.id, goal="他人任务", allowed_skills=["job-discovery"],
        context_summary={}, budget_json={}, agent_version="pev-test",
    )
    service = AgentRunService(settings_override(agent_harness_enabled=True), runtime=None)

    runs = service.list_runs(db_session, user_id=owner.id, limit=20)

    assert [run.goal for run in runs] == ["我的任务"]


def test_service_lists_plans_only_after_owner_check(db_session) -> None:
    owner = _user("user-a", "user-a@example.test")
    other = _user("user-b", "user-b@example.test")
    db_session.add_all([owner, other])
    db_session.commit()
    run = run_repository.create_run(
        db_session, user_id=owner.id, goal="我的任务", allowed_skills=["job-discovery"],
        context_summary={}, budget_json={}, agent_version="pev-test",
    )
    run_repository.create_plan(
        db_session, run_id=run.id, revision=1, complexity=ComplexityLevel.L2,
        plan_json={"goal": "我的任务", "steps": []},
    )
    service = AgentRunService(settings_override(agent_harness_enabled=True), runtime=None)

    assert len(service.list_plans(db_session, user_id=owner.id, run_id=run.id)) == 1
    with pytest.raises(AgentRunNotFoundError):
        service.list_plans(db_session, user_id=other.id, run_id=run.id)


def test_service_injects_only_the_owners_latest_confirmed_profile_into_private_task_context(db_session) -> None:
    """Resume tailoring must source facts from server-owned confirmed profile data."""
    user = _user("user-a", "user-a@example.test")
    db_session.add(user)
    db_session.commit()
    profile = Profile(user_id=user.id, version=1, local_sensitive_references={})
    db_session.add(profile)
    db_session.flush()
    db_session.add(ConfirmedProfileVersion(
        profile_id=profile.id, version_number=1, aggregate_version=1,
        facts_snapshot={"skills": ["Python", "RAG"]}, evidence_refs={},
        local_sensitive_references={},
    ))
    db_session.commit()
    runtime = CapturingRuntime()
    service = AgentRunService(settings_override(agent_harness_enabled=True), runtime=runtime)

    service.create_run(
        db_session,
        user_id=user.id,
        task=AgentTaskRequest(goal="修改简历", allowed_skills=["resume-tailoring"]),
    )

    assert runtime.task is not None
    assert runtime.task.private_context == {
        "confirmed_profile_facts": {"skills": ["Python", "RAG"]}
    }


def test_service_resumes_only_an_owner_waiting_run_and_preserves_their_reply(db_session) -> None:
    """Human-in-the-loop replies become safe task context, never a second Run."""
    user = _user("user-a", "user-a@example.test")
    other = _user("user-b", "user-b@example.test")
    db_session.add_all([user, other])
    db_session.commit()
    run = run_repository.create_run(
        db_session,
        user_id=user.id,
        goal="找岗位",
        allowed_skills=["job-discovery"],
        context_summary={"candidate_urls": ["https://jobs.example/1"]},
        budget_json={"max_agent_turns": 3, "max_tool_calls": 3, "max_replans": 0},
        agent_version="pev-test",
    )
    run_repository.start_run(db_session, run)
    run_repository.finish_run(
        db_session, run, status=RunStatus.waiting_user, final_summary="请确认城市"
    )
    runtime = CapturingRuntime()
    service = AgentRunService(settings_override(agent_harness_enabled=True), runtime=runtime)

    result = service.resume_run(
        db_session, user_id=user.id, run_id=run.id, user_response="北京"
    )

    assert result.run_id == run.id
    assert runtime.task is not None
    assert runtime.task.context == {
        "candidate_urls": ["https://jobs.example/1"],
        "user_responses": ["北京"],
    }
    assert run.context_summary_json["user_responses"] == ["北京"]
    with pytest.raises(AgentRunNotFoundError):
        service.resume_run(
            db_session, user_id=other.id, run_id=run.id, user_response="上海"
        )


def test_service_recovers_only_an_owner_running_run_from_durable_context(db_session) -> None:
    user = _user("user-a", "user-a@example.test")
    db_session.add(user)
    db_session.commit()
    run = run_repository.create_run(
        db_session,
        user_id=user.id,
        goal="找岗位",
        allowed_skills=["job-discovery"],
        context_summary={"candidate_urls": ["https://jobs.example/1"]},
        budget_json={"max_agent_turns": 3, "max_tool_calls": 3, "max_replans": 0},
        agent_version="pev-test",
    )
    run_repository.start_run(db_session, run)
    runtime = CapturingRuntime()
    service = AgentRunService(settings_override(agent_harness_enabled=True), runtime=runtime)

    result = service.recover_run(db_session, user_id=user.id, run_id=run.id)

    assert result.run_id == run.id
    assert runtime.task is not None
    assert runtime.task.context == {"candidate_urls": ["https://jobs.example/1"]}
    run_repository.finish_run(db_session, run, status=RunStatus.succeeded)
    with pytest.raises(AgentRunNotResumableError):
        service.recover_run(db_session, user_id=user.id, run_id=run.id)


def test_service_fails_closed_when_enabled_but_runtime_is_unavailable(db_session) -> None:
    service = AgentRunService(settings_override(agent_harness_enabled=True), runtime=None)

    with pytest.raises(AgentRuntimeUnavailableError):
        service.create_run(
            db_session, user_id="user-a", task=AgentTaskRequest(goal="找岗位", allowed_skills=["job-discovery"])
        )


def test_service_resume_and_recovery_enforce_lifecycle_and_nonempty_reply(db_session) -> None:
    user = _user("user-a", "user-a@example.test")
    db_session.add(user)
    db_session.commit()
    run = run_repository.create_run(
        db_session, user_id=user.id, goal="找岗位", allowed_skills=["job-discovery"],
        context_summary={}, budget_json={"max_agent_turns": 3, "max_tool_calls": 3, "max_replans": 0},
        agent_version="pev-test",
    )
    runtime = CapturingRuntime()
    service = AgentRunService(settings_override(agent_harness_enabled=True), runtime=runtime)

    with pytest.raises(AgentRunNotResumableError):
        service.resume_run(db_session, user_id=user.id, run_id=run.id, user_response="北京")
    run_repository.start_run(db_session, run)
    run_repository.finish_run(db_session, run, status=RunStatus.waiting_user)
    with pytest.raises(ValueError, match="empty"):
        service.resume_run(db_session, user_id=user.id, run_id=run.id, user_response=" ")
    with pytest.raises(AgentRunNotResumableError):
        service.recover_run(db_session, user_id=user.id, run_id=run.id)


def test_service_preserves_task_private_context_when_no_profile_version_exists(db_session) -> None:
    task = AgentTaskRequest(
        goal="找岗位", allowed_skills=["job-discovery"], private_context={"other": "kept"}
    )

    projected = AgentRunService._with_confirmed_profile_facts(
        db_session, user_id="no-profile", task=task
    )

    assert projected is task
