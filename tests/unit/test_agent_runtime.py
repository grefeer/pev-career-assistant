"""End-to-end service behavior for the real three-Agent PEV orchestration loop."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from pydantic import BaseModel
import pytest
from sqlalchemy import select

from backend.app.db.models import AgentArtifact, AgentPlan, AgentRun, AgentStep, AgentTurn, User, UserRole
from backend.app.domain.agent_runtime import (
    AgentRole,
    ComplexityLevel,
    RunStatus,
    StepStatus,
    VerificationDecision,
)
from backend.app.repositories import agent_runtime as run_repository
from backend.app.services.agent_runtime import runtime as agent_runtime_module
from backend.app.services.agent_runtime.executor_agent import ExecutorAgent
from backend.app.services.agent_runtime.model_gateway import AgentModelGatewayError
from backend.app.services.agent_runtime.planner_agent import PlannerAgent
from backend.app.services.agent_runtime.runtime import AgentRuntime, _skill_artifact_source_url
from backend.app.services.agent_runtime.schemas import (
    AgentBudget,
    AgentTaskRequest,
    ExecutionPlan,
    ExecutorResult,
    PlanStep,
    PlannerResult,
    ToolObservation,
    VerifierResult,
)
from backend.app.services.agent_runtime.tool_budget import ToolCallBudget
from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.agent_runtime.tool_registry import ToolDefinition, ToolRegistry
from backend.app.services.agent_runtime.turn_budget import AgentTurnBudget
from backend.app.services.agent_runtime.verifier_agent import VerifierAgent
from backend.app.services.career_skills.manifest import build_career_skill_registry
from backend.app.services.career_skills.job_discovery import SearchPublicJobPagesInput


class EmptyInput(BaseModel):
    pass


class JobOutput(BaseModel):
    title: str


class FetchedJobOutput(BaseModel):
    title: str
    source_url: str
    content_hash: str
    visible_text: str


class EvidenceOutput(BaseModel):
    complete: bool


class StructuredJobOutput(BaseModel):
    source_url: str
    content_hash: str
    candidates: list[dict[str, object]]


class SearchResultsOutput(BaseModel):
    query: str
    source_url: str
    content_hash: str
    results: list[dict[str, str]]


class OfficialNegativeSearchOutput(SearchResultsOutput):
    terminal_reason: str
    provider: str
    source_scope: str
    time_window_days: int
    coverage_complete: bool
    scanned_result_count: int
    matched_result_count: int
    scan_queries: list[str]
    scan_evidence: list[dict[str, str]]


class SheetRecordsOutput(BaseModel):
    """Mirror of QueryCareerSheetRecordsOutput's evidence shape (C005)."""

    records: list[dict[str, object]]
    source_url: str
    content_hash: str
    query: dict[str, object]


class ResumeTailoringOutput(BaseModel):
    target_artifact_id: str
    target_title: str | None
    source_url: str
    supported_keywords: list[str]
    missing_keywords: list[str]
    safe_actions: list[str]
    proposed_diffs: list[dict[str, str]]


class RoleScriptedGateway:
    """Controlled model boundary; real PEV roles and tool handlers execute."""

    def __init__(self, scripts: dict[AgentRole, list[dict[str, Any]]]) -> None:
        self.scripts = scripts
        self.states: dict[AgentRole, list[dict[str, Any]]] = {
            role: [] for role in AgentRole
        }
        self.last_usage = {
            "model_name": "scripted-model",
            "input_tokens": 100,
            "output_tokens": 50,
        }

    def decide(
        self,
        *,
        role: AgentRole,
        instruction: str,
        state: dict[str, Any],
        response_model: type[BaseModel],
    ) -> BaseModel:
        assert instruction and state
        self.states[role].append(state)
        return response_model.model_validate(self.scripts[role].pop(0))


class FailingGateway:
    """Provider boundary double that simulates a recoverable model outage."""

    @property
    def last_usage(self) -> dict[str, Any] | None:
        return None

    def decide(self, **_kwargs):  # noqa: ANN003
        raise AgentModelGatewayError("model_request_failed")


class InvalidModelGateway:
    """Provider double whose completions never parse into the response model."""

    @property
    def last_usage(self) -> dict[str, Any] | None:
        return None

    def decide(self, **_kwargs):  # noqa: ANN003
        raise AgentModelGatewayError("invalid_model_response")


class NoUsageGateway:
    """Gateway that returns valid decisions but reports no token usage."""

    def __init__(self, scripts: dict[AgentRole, list[dict[str, Any]]]) -> None:
        self.scripts = scripts

    @property
    def last_usage(self) -> dict[str, Any] | None:
        return None

    def decide(
        self,
        *,
        role: AgentRole,
        instruction: str,
        state: dict[str, Any],
        response_model: type[BaseModel],
    ) -> BaseModel:
        assert instruction and state
        return response_model.model_validate(self.scripts[role].pop(0))


class CrashAfterFirstExecutorDecisionGateway:
    """Simulate process loss after a persisted Executor decision checkpoint."""

    def __init__(self) -> None:
        self._executor_decisions = 0

    @property
    def last_usage(self) -> dict[str, Any] | None:
        return {"model_name": "crash-model", "input_tokens": 100, "output_tokens": 50}

    def decide(self, *, role: AgentRole, response_model: type[BaseModel], **_kwargs) -> BaseModel:
        if role is AgentRole.planner:
            return response_model.model_validate({
                "action": "plan", "complexity": "L2", "success_criteria": ["完成"],
                "steps": [{
                    "step_id": "discover", "objective": "获取公开 JD",
                    "allowed_skills": ["job-discovery"], "requires_verification": False,
                }],
            })
        if role is AgentRole.executor:
            self._executor_decisions += 1
            if self._executor_decisions == 1:
                return response_model.model_validate({
                    "action": "call_tool", "tool_name": "fetch-job", "tool_input": {},
                })
            raise RuntimeError("simulated_process_loss")
        raise AssertionError("Verifier should not run for an L2 step")


def test_runtime_can_persist_a_queued_run_before_background_execution(db_session) -> None:
    user = User(
        id="queued-user", account="queued@example.test", nickname="queued",
        password_hash="not-a-real-password-hash", role=UserRole.STUDENT,
    )
    db_session.add(user)
    db_session.commit()
    runtime = AgentRuntime(
        planner=MagicMock(), executor=MagicMock(), verifier=MagicMock(), agent_version="pev-test",
    )

    run = runtime.create_queued_run(
        db_session, user_id=user.id,
        task=AgentTaskRequest(goal="后台任务", allowed_skills=["job-discovery"]),
    )

    assert run.status is RunStatus.queued
    assert run.goal == "后台任务"


def _runtime_for_gateway(gateway: object) -> AgentRuntime:
    registry = ToolRegistry()
    return AgentRuntime(
        planner=PlannerAgent(gateway=gateway, tools=registry),
        executor=ExecutorAgent(gateway=gateway, tools=registry),
        verifier=VerifierAgent(gateway=gateway, tools=registry),
        agent_version="pev-test",
    )


def _create_running_step(
    db_session,
    user: User,
    *,
    requires_verification: bool,
    budget: AgentBudget | None = None,
):
    task = AgentTaskRequest(
        goal="验证运行时失败分支",
        allowed_skills=["job-discovery"],
        budget=budget or AgentBudget(max_agent_turns=4, max_tool_calls=4, max_replans=0),
    )
    plan_step = PlanStep(
        step_id="discover",
        objective="提取公开岗位",
        allowed_skills=["job-discovery"],
        requires_verification=requires_verification,
    )
    plan = ExecutionPlan(
        task=task,
        created_by=AgentRole.planner,
        complexity=ComplexityLevel.L3 if requires_verification else ComplexityLevel.L2,
        success_criteria=["有证据"],
        steps=[plan_step],
    )
    run = run_repository.create_run(
        db_session,
        user_id=user.id,
        goal=task.goal,
        allowed_skills=task.allowed_skills,
        context_summary={},
        budget_json=task.budget.model_dump(mode="json"),
        agent_version="pev-test",
    )
    run_repository.start_run(db_session, run)
    stored_plan = run_repository.create_plan(
        db_session,
        run_id=run.id,
        revision=1,
        complexity=plan.complexity,
        plan_json=plan.model_dump(mode="json"),
    )
    step = run_repository.create_step(
        db_session,
        run_id=run.id,
        plan_id=stored_plan.id,
        sequence=1,
        objective=plan_step.objective,
        allowed_skills=plan_step.allowed_skills,
    )
    return run, task, plan, plan_step, step


def test_runtime_persists_planner_executor_verifier_success_trace(db_session) -> None:
    """The harness schedules agent outcomes but never preselects their tools."""
    user = User(
        id="user-a",
        account="user-a@example.test",
        nickname="user-a",
        password_hash="not-a-real-password-hash",
        role=UserRole.STUDENT,
    )
    db_session.add(user)
    db_session.commit()
    gateway = RoleScriptedGateway(
        {
            AgentRole.planner: [
                {
                    "action": "plan",
                    "complexity": "L3",
                    "success_criteria": ["完整 JD 有公开来源"],
                    "steps": [
                        {
                            "step_id": "discover",
                            "objective": "提取公开岗位",
                            "allowed_skills": ["job-discovery"],
                            "requires_verification": True,
                        }
                    ],
                }
            ],
            AgentRole.executor: [
                {
                    "action": "call_tool",
                    "tool_name": "fetch-job",
                    "tool_input": {},
                },
                {
                    "action": "complete",
                    "summary": "已提取完整 JD",
                    "artifact_refs": [{"uri": "artifact://job/1"}],
                },
            ],
            AgentRole.verifier: [
                {
                    "action": "call_tool",
                    "tool_name": "check-job-evidence",
                    "tool_input": {},
                },
                {"action": "decide", "verification_decision": "PASS"},
            ],
        }
    )
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="fetch-job",
            skill_name="job-discovery",
            input_model=EmptyInput,
            output_model=JobOutput,
            allowed_roles=frozenset({AgentRole.executor}),
            handler=lambda _context, _payload: {"title": "AI Agent 开发工程师"},
        )
    )
    registry.register(
        ToolDefinition(
            name="check-job-evidence",
            skill_name="job-discovery",
            input_model=EmptyInput,
            output_model=EvidenceOutput,
            allowed_roles=frozenset({AgentRole.verifier}),
            handler=lambda _context, _payload: {"complete": True},
        )
    )
    runtime = AgentRuntime(
        planner=PlannerAgent(gateway=gateway, tools=registry),
        executor=ExecutorAgent(gateway=gateway, tools=registry),
        verifier=VerifierAgent(gateway=gateway, tools=registry),
        agent_version="pev-test",
    )

    result = runtime.run(
        db_session,
        user_id=user.id,
        task=AgentTaskRequest(goal="找 AI Agent 岗位", allowed_skills=["job-discovery"]),
    )
    db_session.commit()

    assert result.status is RunStatus.succeeded
    assert result.summary == "已提取完整 JD"
    assert [event.event_type for event in run_repository.list_events(db_session, result.run_id)] == [
        "run_started",
        "plan_created",
        "step_succeeded",
        "verification_passed",
        "run_succeeded",
    ]
    turns = list(
        db_session.scalars(
            select(AgentTurn)
            .where(AgentTurn.run_id == result.run_id)
            .order_by(AgentTurn.created_at.asc(), AgentTurn.id.asc())
        )
    )
    assert [(turn.role, turn.decision_json["action"]) for turn in turns] == [
        (AgentRole.planner, "plan"),
        (AgentRole.executor, "call_tool"),
        (AgentRole.executor, "complete"),
        (AgentRole.verifier, "call_tool"),
        (AgentRole.verifier, "decide"),
    ]
    # Verify token usage is persisted for every turn
    for turn in turns:
        assert turn.model_name == "scripted-model"
        assert turn.input_tokens == 100
        assert turn.output_tokens == 50
    # Verify context manifest is persisted with expected fields for every turn
    for turn in turns:
        assert turn.context_manifest is not None
        assert "system_prompt_chars" in turn.context_manifest
        assert "tool_catalog_count" in turn.context_manifest
        assert "tool_catalog_chars" in turn.context_manifest
        assert "observation_count" in turn.context_manifest
        assert "observation_chars" in turn.context_manifest
        assert "evidence_chars" in turn.context_manifest
        assert "model_name" in turn.context_manifest
        assert turn.context_manifest["model_name"] == "scripted-model"
        # All count fields are integers (no raw content)
        assert isinstance(turn.context_manifest["system_prompt_chars"], int)
        assert isinstance(turn.context_manifest["tool_catalog_count"], int)
        assert isinstance(turn.context_manifest["tool_catalog_chars"], int)
        assert isinstance(turn.context_manifest["observation_count"], int)
        assert isinstance(turn.context_manifest["observation_chars"], int)
        # evidence_chars is always an int (0 for no evidence, >0 otherwise)
        assert isinstance(turn.context_manifest["evidence_chars"], int)
        assert turn.context_manifest["evidence_chars"] >= 0
        # Planner has observations
        if turn.role is AgentRole.planner:
            assert turn.context_manifest["observation_count"] >= 0


def test_runtime_trace_records_nulls_when_gateway_has_no_usage(db_session) -> None:
    """When the gateway reports last_usage=None, turns persist null model/usage/manifest."""
    user = User(
        id="user-no-usage",
        account="user-no-usage@example.test",
        nickname="user-no-usage",
        password_hash="not-a-real-password-hash",
        role=UserRole.STUDENT,
    )
    db_session.add(user)
    db_session.commit()
    gateway = NoUsageGateway(
        {
            AgentRole.planner: [
                {
                    "action": "plan",
                    "complexity": "L3",
                    "success_criteria": ["完整 JD 有公开来源"],
                    "steps": [
                        {
                            "step_id": "discover",
                            "objective": "提取公开岗位",
                            "allowed_skills": ["job-discovery"],
                            "requires_verification": True,
                        }
                    ],
                }
            ],
            AgentRole.executor: [
                {
                    "action": "call_tool",
                    "tool_name": "fetch-job",
                    "tool_input": {},
                },
                {
                    "action": "complete",
                    "summary": "已提取完整 JD",
                    "artifact_refs": [{"uri": "artifact://job/1"}],
                },
            ],
            AgentRole.verifier: [
                {
                    "action": "call_tool",
                    "tool_name": "check-job-evidence",
                    "tool_input": {},
                },
                {"action": "decide", "verification_decision": "PASS"},
            ],
        }
    )
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="fetch-job",
            skill_name="job-discovery",
            input_model=EmptyInput,
            output_model=JobOutput,
            allowed_roles=frozenset({AgentRole.executor}),
            handler=lambda _context, _payload: {"title": "AI Agent 开发工程师"},
        )
    )
    registry.register(
        ToolDefinition(
            name="check-job-evidence",
            skill_name="job-discovery",
            input_model=EmptyInput,
            output_model=EvidenceOutput,
            allowed_roles=frozenset({AgentRole.verifier}),
            handler=lambda _context, _payload: {"complete": True},
        )
    )
    runtime = AgentRuntime(
        planner=PlannerAgent(gateway=gateway, tools=registry),
        executor=ExecutorAgent(gateway=gateway, tools=registry),
        verifier=VerifierAgent(gateway=gateway, tools=registry),
        agent_version="pev-test",
    )

    result = runtime.run(
        db_session,
        user_id=user.id,
        task=AgentTaskRequest(goal="找 AI Agent 岗位", allowed_skills=["job-discovery"]),
    )
    db_session.commit()

    assert result.status is RunStatus.succeeded
    turns = list(
        db_session.scalars(
            select(AgentTurn)
            .where(AgentTurn.run_id == result.run_id)
            .order_by(AgentTurn.created_at.asc(), AgentTurn.id.asc())
        )
    )
    assert len(turns) == 5
    # When last_usage is None, every turn records null usage and no context manifest.
    for turn in turns:
        assert turn.model_name is None
        assert turn.input_tokens is None
        assert turn.output_tokens is None
        assert turn.context_manifest is None


def test_runtime_recovers_a_process_interrupted_run_from_committed_checkpoints(db_session) -> None:
    user = User(
        id="user-a", account="user-a@example.test", nickname="user-a",
        password_hash="not-a-real-password-hash", role=UserRole.STUDENT,
    )
    db_session.add(user)
    db_session.commit()
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="fetch-job", skill_name="job-discovery", input_model=EmptyInput,
            output_model=JobOutput, allowed_roles=frozenset({AgentRole.executor}),
            handler=lambda _context, _payload: {"title": "AI Agent 开发工程师"},
        )
    )
    crashing_gateway = CrashAfterFirstExecutorDecisionGateway()
    runtime = AgentRuntime(
        planner=PlannerAgent(gateway=crashing_gateway, tools=registry),
        executor=ExecutorAgent(gateway=crashing_gateway, tools=registry),
        verifier=VerifierAgent(gateway=crashing_gateway, tools=registry),
        agent_version="pev-test",
    )
    task = AgentTaskRequest(goal="找 AI Agent 岗位", allowed_skills=["job-discovery"])

    with pytest.raises(RuntimeError, match="simulated_process_loss"):
        runtime.run(db_session, user_id=user.id, task=task)

    interrupted = db_session.scalar(select(AgentRun))
    assert interrupted is not None
    assert interrupted.status is RunStatus.running
    assert db_session.scalar(select(AgentPlan).where(AgentPlan.run_id == interrupted.id)) is not None
    assert [(turn.role, turn.decision_json["action"]) for turn in db_session.scalars(
        select(AgentTurn).where(AgentTurn.run_id == interrupted.id)
    )] == [(AgentRole.planner, "plan"), (AgentRole.executor, "call_tool")]

    recovery_gateway = RoleScriptedGateway({
        AgentRole.planner: [{
            "action": "plan", "complexity": "L2", "success_criteria": ["完成"],
            "steps": [{
                "step_id": "discover", "objective": "重新确认公开 JD",
                "allowed_skills": ["job-discovery"], "requires_verification": False,
            }],
        }],
        AgentRole.executor: [{"action": "complete", "summary": "恢复完成", "artifact_refs": []}],
        AgentRole.verifier: [],
    })
    recovery_runtime = AgentRuntime(
        planner=PlannerAgent(gateway=recovery_gateway, tools=registry),
        executor=ExecutorAgent(gateway=recovery_gateway, tools=registry),
        verifier=VerifierAgent(gateway=recovery_gateway, tools=registry),
        agent_version="pev-test",
    )

    result = recovery_runtime.recover(
        db_session, user_id=user.id, run_id=interrupted.id, task=task
    )

    assert result.status is RunStatus.succeeded
    assert run_repository.count_plans(db_session, interrupted.id) == 2
    assert "run_recovery_started" in [
        event.event_type for event in run_repository.list_events(db_session, interrupted.id)
    ]


def test_runtime_recovers_replan_budget_from_persisted_plans(db_session) -> None:
    """A recovered run keeps its already-spent replan budget instead of resetting it.

    A process-interrupted run that already consumed one replan (two plans
    persisted) must resume with replans == 1, not 0. Otherwise the budget spent
    before the crash becomes spendable again and ``max_replans`` is silently
    doubled on every recovery.
    """
    user = User(
        id="user-a", account="user-a@example.test", nickname="user-a",
        password_hash="not-a-real-password-hash", role=UserRole.STUDENT,
    )
    db_session.add(user)
    db_session.commit()
    registry = ToolRegistry()
    # Recovery forces one more replan. With the budget correctly recovered
    # (replans resumes at 1), the next replan becomes a human hand-off rather
    # than a failed run.
    plan_decision = {
        "action": "plan", "complexity": "L2",
        "success_criteria": ["完整 JD"],
        "steps": [{
            "step_id": "discover", "objective": "重新提取岗位",
            "allowed_skills": ["job-discovery"], "requires_verification": True,
        }],
    }
    gateway = RoleScriptedGateway({
        AgentRole.planner: [plan_decision],
        AgentRole.executor: [{"action": "complete", "summary": "恢复后结果", "artifact_refs": []}],
        AgentRole.verifier: [{
            "action": "decide", "verification_decision": "REPLAN",
            "feedback": "来源改版，需要重新规划。",
        }],
    })
    runtime = AgentRuntime(
        planner=PlannerAgent(gateway=gateway, tools=registry),
        executor=ExecutorAgent(gateway=gateway, tools=registry),
        verifier=VerifierAgent(gateway=gateway, tools=registry),
        agent_version="pev-test",
    )
    task = AgentTaskRequest(
        goal="找岗位",
        allowed_skills=["job-discovery"],
        budget={"max_agent_turns": 8, "max_tool_calls": 8, "max_replans": 1},
    )
    # Seed a run that already consumed one replan (two plans persisted, revision
    # == count_plans == 2) before the process was interrupted mid-execution.
    run = runtime.create_queued_run(db_session, user_id=user.id, task=task)
    run_repository.start_run(db_session, run)
    plan_json = {
        "complexity": "L2",
        "success_criteria": ["完整 JD"],
        "steps": [{
            "step_id": "discover", "objective": "提取岗位",
            "allowed_skills": ["job-discovery"], "requires_verification": True,
        }],
    }
    run_repository.create_plan(
        db_session, run_id=run.id, revision=1,
        complexity=ComplexityLevel.L2, plan_json=plan_json,
    )
    run_repository.create_plan(
        db_session, run_id=run.id, revision=2,
        complexity=ComplexityLevel.L2, plan_json=plan_json,
    )
    db_session.commit()
    assert run.status is RunStatus.running
    assert run_repository.count_plans(db_session, run.id) == 2

    result = runtime.recover(
        db_session, user_id=user.id, run_id=run.id, task=task
    )

    assert result.status is RunStatus.waiting_user
    assert result.error_code == "replan_budget_exhausted"
    # Three plans now persisted: two seeded + one from the recovery replan.
    assert run_repository.count_plans(db_session, run.id) == 3


def test_runtime_rejects_resume_or_recovery_for_missing_or_wrong_state_runs(db_session) -> None:
    runtime = object.__new__(AgentRuntime)
    task = AgentTaskRequest(goal="找岗位", allowed_skills=["job-discovery"])
    with pytest.raises(ValueError, match="not_found"):
        runtime.resume(db_session, user_id="none", run_id="missing", task=task)
    with pytest.raises(ValueError, match="not_found"):
        runtime.recover(db_session, user_id="none", run_id="missing", task=task)

    user = User(
        id="user-a", account="user-a@example.test", nickname="user-a",
        password_hash="not-a-real-password-hash", role=UserRole.STUDENT,
    )
    db_session.add(user)
    db_session.commit()
    run = run_repository.create_run(
        db_session, user_id=user.id, goal="找岗位", allowed_skills=["job-discovery"],
        context_summary={}, budget_json={}, agent_version="pev-test",
    )
    with pytest.raises(ValueError, match="not_waiting_user"):
        runtime.resume(db_session, user_id=user.id, run_id=run.id, task=task)
    with pytest.raises(ValueError, match="not_running"):
        runtime.recover(db_session, user_id=user.id, run_id=run.id, task=task)


def test_runtime_handles_executor_and_verifier_provider_errors_without_leaving_run_open(db_session) -> None:
    user = User(
        id="user-a", account="user-a@example.test", nickname="user-a",
        password_hash="not-a-real-password-hash", role=UserRole.STUDENT,
    )
    db_session.add(user)
    db_session.commit()
    run, task, plan, plan_step, step = _create_running_step(
        db_session, user, requires_verification=False
    )
    result = _runtime_for_gateway(FailingGateway())._run_step(
        db=db_session, run_id=run.id, task=task, plan=plan, plan_step=plan_step,
        persisted_step=step, context=ToolContext(user_id=user.id, run_id=run.id),
        trace=lambda *_args: None, tool_budget=ToolCallBudget(4), turn_budget=AgentTurnBudget(4),
    )
    assert (result.status, result.error_code) == (RunStatus.failed, "model_request_failed")

    run, task, plan, plan_step, step = _create_running_step(
        db_session, user, requires_verification=True
    )
    gateway = RoleScriptedGateway({
        AgentRole.planner: [],
        AgentRole.executor: [{"action": "complete", "summary": "提取完成"}],
        AgentRole.verifier: [],
    })
    runtime = _runtime_for_gateway(gateway)
    runtime._verifier = VerifierAgent(gateway=FailingGateway(), tools=ToolRegistry())
    result = runtime._run_step(
        db=db_session, run_id=run.id, task=task, plan=plan, plan_step=plan_step,
        persisted_step=step, context=ToolContext(user_id=user.id, run_id=run.id),
        trace=lambda *_args: None, tool_budget=ToolCallBudget(4), turn_budget=AgentTurnBudget(4),
    )
    assert (result.status, result.error_code) == (RunStatus.failed, "model_request_failed")


def test_runtime_persists_an_executor_input_request_as_waiting_for_user(db_session) -> None:
    user = User(
        id="user-a", account="user-a@example.test", nickname="user-a",
        password_hash="not-a-real-password-hash", role=UserRole.STUDENT,
    )
    db_session.add(user)
    db_session.commit()
    run, task, plan, plan_step, step = _create_running_step(
        db_session, user, requires_verification=False
    )
    gateway = RoleScriptedGateway({
        AgentRole.planner: [],
        AgentRole.executor: [{"action": "need_user", "user_question": "请确认城市"}],
        AgentRole.verifier: [],
    })

    result = _runtime_for_gateway(gateway)._run_step(
        db=db_session, run_id=run.id, task=task, plan=plan, plan_step=plan_step,
        persisted_step=step, context=ToolContext(user_id=user.id, run_id=run.id),
        trace=lambda *_args: None, tool_budget=ToolCallBudget(4), turn_budget=AgentTurnBudget(4),
    )

    assert result.status is RunStatus.waiting_user
    assert step.error_code == "need_user"


@pytest.mark.parametrize(
    ("verdict", "feedback", "expected_status", "expected_error"),
    [
        ("RETRY_EXECUTOR", "补充来源", RunStatus.waiting_user, None),
        ("NEED_USER", "请确认城市", RunStatus.waiting_user, None),
    ],
)
def test_runtime_routes_verifier_nonpass_outcomes_to_safe_terminal_state(
    db_session, verdict: str, feedback: str, expected_status: RunStatus, expected_error: str | None
) -> None:
    user = User(
        id="user-a", account="user-a@example.test", nickname="user-a",
        password_hash="not-a-real-password-hash", role=UserRole.STUDENT,
    )
    db_session.add(user)
    db_session.commit()
    run, task, plan, plan_step, step = _create_running_step(
        db_session, user, requires_verification=True
    )
    gateway = RoleScriptedGateway({
        AgentRole.planner: [],
        AgentRole.executor: [{"action": "complete", "summary": "提取完成"}],
        AgentRole.verifier: [{
            "action": "decide", "verification_decision": verdict, "feedback": feedback,
        }],
    })
    result = _runtime_for_gateway(gateway)._run_step(
        db=db_session, run_id=run.id, task=task, plan=plan, plan_step=plan_step,
        persisted_step=step, context=ToolContext(user_id=user.id, run_id=run.id),
        trace=lambda *_args: None, tool_budget=ToolCallBudget(4), turn_budget=AgentTurnBudget(4),
    )
    assert result.status is expected_status
    assert result.error_code == expected_error


def test_runtime_retry_exhaustion_hands_a_stuck_step_to_the_human(db_session) -> None:
    """Repeated verifier RETRY after the retry budget routes to waiting_user with feedback."""
    user = User(
        id="user-a", account="user-a@example.test", nickname="user-a",
        password_hash="not-a-real-password-hash", role=UserRole.STUDENT,
    )
    db_session.add(user)
    db_session.commit()
    run, task, plan, plan_step, step = _create_running_step(
        db_session, user, requires_verification=True,
        budget=AgentBudget(max_agent_turns=4, max_tool_calls=4, max_replans=1),
    )
    gateway = RoleScriptedGateway({
        AgentRole.planner: [],
        AgentRole.executor: [
            {"action": "complete", "summary": "提取完成"},
            {"action": "complete", "summary": "提取完成"},
        ],
        AgentRole.verifier: [
            {"action": "decide", "verification_decision": "RETRY_EXECUTOR", "feedback": "缺少来源标注"},
            {"action": "decide", "verification_decision": "RETRY_EXECUTOR", "feedback": "缺少来源标注"},
        ],
    })
    result = _runtime_for_gateway(gateway)._run_step(
        db=db_session, run_id=run.id, task=task, plan=plan, plan_step=plan_step,
        persisted_step=step, context=ToolContext(user_id=user.id, run_id=run.id),
        trace=lambda *_args: None, tool_budget=ToolCallBudget(4), turn_budget=AgentTurnBudget(4),
    )

    assert result.status is RunStatus.waiting_user
    assert step.error_code == "no_progress_duplicate"
    assert "缺少来源标注" in result.summary
    events = run_repository.list_events(db_session, run.id)
    assert events[-1].event_type == "run_needs_user"


def test_runtime_private_terminal_helpers_reject_disappeared_runs_and_persist_waiting_state(db_session) -> None:
    runtime = object.__new__(AgentRuntime)
    with pytest.raises(RuntimeError, match="disappeared"):
        runtime._fail_run(db_session, "missing", "failed")
    with pytest.raises(RuntimeError, match="disappeared"):
        runtime._wait_for_user(db_session, "missing", None, "请确认")

    user = User(
        id="user-a", account="user-a@example.test", nickname="user-a",
        password_hash="not-a-real-password-hash", role=UserRole.STUDENT,
    )
    db_session.add(user)
    db_session.commit()
    run, _task, _plan, _plan_step, step = _create_running_step(
        db_session, user, requires_verification=False
    )
    waiting = runtime._wait_for_user(db_session, run.id, step, "请确认城市")
    assert waiting.status is RunStatus.waiting_user
    assert step.error_code == "need_user"


def test_runtime_marks_failed_planner_outcome_as_a_safe_failed_run(db_session) -> None:
    user = User(
        id="user-a", account="user-a@example.test", nickname="user-a",
        password_hash="not-a-real-password-hash", role=UserRole.STUDENT,
    )
    db_session.add(user)
    db_session.commit()
    run = run_repository.create_run(
        db_session, user_id=user.id, goal="找岗位", allowed_skills=["job-discovery"],
        context_summary={}, budget_json={}, agent_version="pev-test",
    )
    runtime = object.__new__(AgentRuntime)
    result = runtime._finish_planner_non_plan(
        db_session, run.id, run, PlannerResult(status="failed", error_code="planner_failed")
    )
    assert (result.status, result.error_code) == (RunStatus.failed, "planner_failed")


def test_runtime_fails_safely_when_verifier_replan_budget_is_exhausted(db_session) -> None:
    user = User(
        id="user-a", account="user-a@example.test", nickname="user-a",
        password_hash="not-a-real-password-hash", role=UserRole.STUDENT,
    )
    db_session.add(user)
    db_session.commit()
    gateway = RoleScriptedGateway({
        AgentRole.planner: [{
            "action": "plan", "complexity": "L3", "success_criteria": ["有证据"],
            "steps": [{
                "step_id": "discover", "objective": "提取公开 JD",
                "allowed_skills": ["job-discovery"], "requires_verification": True,
            }],
        }],
        AgentRole.executor: [{"action": "complete", "summary": "已提取"}],
        AgentRole.verifier: [{
            "action": "decide", "verification_decision": "REPLAN", "feedback": "目标不完整",
        }],
    })

    result = _runtime_for_gateway(gateway).run(
        db_session,
        user_id=user.id,
        task=AgentTaskRequest(
            goal="找岗位",
            allowed_skills=["job-discovery"],
            budget=AgentBudget(max_agent_turns=4, max_tool_calls=4, max_replans=0),
        ),
    )

    assert (result.status, result.error_code) == (
        RunStatus.waiting_user,
        "replan_budget_exhausted",
    )


def test_runtime_persists_resume_tailoring_as_a_reviewable_skill_artifact(db_session) -> None:
    """An Executor-created resume diff must remain available after the model turn."""
    user = User(
        id="user-a", account="user-a@example.test", nickname="user-a",
        password_hash="not-a-real-password-hash", role=UserRole.STUDENT,
    )
    db_session.add(user)
    db_session.commit()
    gateway = RoleScriptedGateway({
        AgentRole.planner: [{
            "action": "plan", "complexity": "L2", "success_criteria": ["resume diff"],
            "steps": [{
                "step_id": "tailor", "objective": "produce a grounded resume diff",
                "allowed_skills": ["resume-tailoring"],
            }],
        }],
        AgentRole.executor: [
            {"action": "call_tool", "tool_name": "build-resume-tailoring-brief", "tool_input": {}},
            {"action": "complete", "summary": "resume diff ready"},
        ],
        AgentRole.verifier: [],
    })
    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="build-resume-tailoring-brief", skill_name="resume-tailoring", input_model=EmptyInput,
        output_model=ResumeTailoringOutput,
        allowed_roles=frozenset({AgentRole.executor}),
        handler=lambda _context, _payload: {
            "target_artifact_id": "observed:job", "target_title": "AI Agent 开发工程师",
            "source_url": "https://jobs.example/agent", "supported_keywords": ["python"],
            "missing_keywords": ["langgraph"], "safe_actions": ["不得虚构"],
            "proposed_diffs": [{
                "op": "highlight", "section": "skills", "fact_ref": "skills",
                "target_evidence_ref": "observed:job", "change_summary": "highlight Python",
            }],
        },
    ))
    runtime = AgentRuntime(
        planner=PlannerAgent(gateway=gateway, tools=registry),
        executor=ExecutorAgent(gateway=gateway, tools=registry),
        verifier=VerifierAgent(gateway=gateway, tools=registry), agent_version="pev-test",
    )

    result = runtime.run(
        db_session, user_id=user.id,
        task=AgentTaskRequest(goal="tailor resume", allowed_skills=["resume-tailoring"]),
    )

    artifacts = list(db_session.scalars(
        select(AgentArtifact).where(AgentArtifact.run_id == result.run_id)
    ))
    assert result.status is RunStatus.succeeded
    assert [(artifact.artifact_type, artifact.source_url) for artifact in artifacts] == [
        ("resume_tailoring_brief", "https://jobs.example/agent")
    ]
    assert artifacts[0].content_json["proposed_diffs"][0]["fact_ref"] == "skills"


def test_runtime_persists_multi_source_job_matching_report_with_observed_provenance(db_session) -> None:
    """A match report has many JD sources but must not be dropped for lacking one root URL."""
    user = User(
        id="user-a", account="user-a@example.test", nickname="user-a",
        password_hash="not-a-real-password-hash", role=UserRole.STUDENT,
    )
    db_session.add(user)
    db_session.commit()
    run, _task, _plan, _plan_step, step = _create_running_step(
        db_session, user, requires_verification=False
    )
    execution = ExecutorResult(
        status="succeeded",
        summary="已完成透明匹配",
        observations=[ToolObservation(
            tool_name="match-observed-jobs",
            status="succeeded",
            output={"matches": [
                {"artifact_id": "observed:a", "source_url": "https://jobs.example/a", "score": 80},
                {"artifact_id": "observed:b", "source_url": "https://jobs.example/b", "score": 70},
            ], "unresolved_ranking_criteria": ["salary"]},
        )],
    )

    refs = AgentRuntime._persist_observed_evidence(db_session, run.id, step, execution)

    artifacts = list(db_session.scalars(
        select(AgentArtifact).where(AgentArtifact.run_id == run.id)
    ))
    assert len(refs) == 1
    assert [(artifact.artifact_type, artifact.source_url) for artifact in artifacts] == [
        ("job_matching_report", "https://jobs.example/a")
    ]
    assert [match["source_url"] for match in artifacts[0].content_json["matches"]] == [
        "https://jobs.example/a", "https://jobs.example/b"
    ]
    assert _skill_artifact_source_url("job_matching_report", {"matches": "invalid"}) is None
    assert _skill_artifact_source_url("job_matching_report", {"matches": [{}]}) is None
    assert _skill_artifact_source_url(
        "job_matching_report",
        {
            "matches": [],
            "evaluated_source_urls": ["https://jobs.example/no-match"],
        },
    ) == "https://jobs.example/no-match"


def test_runtime_resumes_waiting_run_with_the_remaining_global_budget(db_session) -> None:
    """A user reply resumes one durable Run instead of opening a fresh budget."""
    user = User(
        id="user-a", account="user-a@example.test", nickname="user-a",
        password_hash="not-a-real-password-hash", role=UserRole.STUDENT,
    )
    db_session.add(user)
    db_session.commit()
    gateway = RoleScriptedGateway({
        AgentRole.planner: [
            {"action": "need_user", "user_question": "请确认目标城市。"},
            {
                "action": "plan", "complexity": "L1", "success_criteria": ["answer"],
                "steps": [{
                    "step_id": "answer", "objective": "return the constrained result",
                    "allowed_skills": ["job-discovery"],
                }],
            },
        ],
        AgentRole.executor: [{"action": "complete", "summary": "completed with user constraint"}],
        AgentRole.verifier: [],
    })
    runtime = AgentRuntime(
        planner=PlannerAgent(gateway=gateway, tools=ToolRegistry()),
        executor=ExecutorAgent(gateway=gateway, tools=ToolRegistry()),
        verifier=VerifierAgent(gateway=gateway, tools=ToolRegistry()),
        agent_version="pev-test",
    )
    original_task = AgentTaskRequest(
        goal="find suitable roles", allowed_skills=["job-discovery"],
        budget={"max_agent_turns": 3, "max_tool_calls": 3, "max_replans": 0},
    )

    waiting = runtime.run(db_session, user_id=user.id, task=original_task)
    resumed = runtime.resume(
        db_session,
        user_id=user.id,
        run_id=waiting.run_id,
        task=original_task.model_copy(
            update={"context": {"user_responses": ["北京"]}}
        ),
    )
    db_session.commit()

    assert waiting.status is RunStatus.waiting_user
    assert resumed.run_id == waiting.run_id
    assert resumed.status is RunStatus.succeeded
    assert gateway.states[AgentRole.planner][1]["context"]["user_responses"] == ["北京"]
    assert "private_context" not in gateway.states[AgentRole.planner][1]
    assert gateway.states[AgentRole.planner][1]["remaining_agent_turns"] == 1
    assert [event.event_type for event in run_repository.list_events(db_session, waiting.run_id)] == [
        "run_started",
        "planner_needs_user",
        "run_resumed",
        "plan_created",
        "step_succeeded",
        "run_succeeded",
    ]


def test_runtime_enforces_one_global_model_turn_budget_across_pev_roles(db_session) -> None:
    """A complex run cannot spend a fresh model-turn allowance per Agent."""
    user = User(
        id="user-a", account="user-a@example.test", nickname="user-a",
        password_hash="not-a-real-password-hash", role=UserRole.STUDENT,
    )
    db_session.add(user)
    db_session.commit()
    gateway = RoleScriptedGateway({
        AgentRole.planner: [{
            "action": "plan", "complexity": "L3", "success_criteria": ["complete"],
            "steps": [{
                "step_id": "one", "objective": "produce an observed result",
                "allowed_skills": ["job-discovery"], "requires_verification": True,
            }],
        }],
        AgentRole.executor: [{"action": "complete", "summary": "result ready"}],
        AgentRole.verifier: [{"action": "decide", "verification_decision": "PASS"}],
    })
    registry = ToolRegistry()
    runtime = AgentRuntime(
        planner=PlannerAgent(gateway=gateway, tools=registry),
        executor=ExecutorAgent(gateway=gateway, tools=registry),
        verifier=VerifierAgent(gateway=gateway, tools=registry),
        agent_version="pev-test",
    )

    result = runtime.run(
        db_session,
        user_id=user.id,
        task=AgentTaskRequest(
            goal="execute a verified job task",
            allowed_skills=["job-discovery"],
            budget={"max_agent_turns": 2, "max_tool_calls": 4, "max_replans": 0},
        ),
    )
    db_session.commit()

    assert result.status is RunStatus.failed
    assert result.error_code == "agent_turn_budget_exhausted"
    assert len(gateway.states[AgentRole.planner]) == 1
    assert len(gateway.states[AgentRole.executor]) == 1
    assert gateway.states[AgentRole.verifier] == []
    assert len(list(db_session.scalars(
        select(AgentTurn).where(AgentTurn.run_id == result.run_id)
    ))) == 2


def test_runtime_enforces_one_global_tool_budget_across_planner_and_executor(db_session) -> None:
    """A Planner context read consumes the same hard tool budget as an Executor Skill call."""
    user = User(
        id="user-a", account="user-a@example.test", nickname="user-a",
        password_hash="not-a-real-password-hash", role=UserRole.STUDENT,
    )
    db_session.add(user)
    db_session.commit()
    execution_count = 0

    def inspect_context(_context, _payload):  # noqa: ANN001
        nonlocal execution_count
        execution_count += 1
        return {"title": "上下文已读取"}

    gateway = RoleScriptedGateway({
        AgentRole.planner: [
            {"action": "call_tool", "tool_name": "inspect-context", "tool_input": {}},
            {
                "action": "plan", "complexity": "L2", "success_criteria": ["已读取上下文"],
                "steps": [{"step_id": "execute", "objective": "执行岗位 Skill", "allowed_skills": ["job-discovery"]}],
            },
        ],
        AgentRole.executor: [
            {"action": "call_tool", "tool_name": "inspect-context", "tool_input": {}},
            {"action": "complete", "summary": "执行完成"},
        ],
        AgentRole.verifier: [],
    })
    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="inspect-context", skill_name="job-discovery", input_model=EmptyInput,
        output_model=JobOutput, allowed_roles=frozenset({AgentRole.planner, AgentRole.executor}),
        handler=inspect_context,
    ))
    runtime = AgentRuntime(
        planner=PlannerAgent(gateway=gateway, tools=registry),
        executor=ExecutorAgent(gateway=gateway, tools=registry),
        verifier=VerifierAgent(gateway=gateway, tools=registry), agent_version="pev-test",
    )

    result = runtime.run(
        db_session,
        user_id=user.id,
        task=AgentTaskRequest(
            goal="先读上下文再执行", allowed_skills=["job-discovery"],
            budget={"max_agent_turns": 4, "max_tool_calls": 1, "max_replans": 0},
        ),
    )

    assert (result.status, result.error_code) == (RunStatus.failed, "tool_budget_exhausted")
    assert execution_count == 1
    events = run_repository.list_events(db_session, result.run_id)
    assert events[-1].payload_json["error_code"] == "tool_budget_exhausted"
    assert events[-1].payload_json["failure_class"] == "budget_exhausted"


def test_runtime_passes_verifier_retry_feedback_to_executor_next_turn(db_session) -> None:
    """Retry is a real feedback loop, not merely a second identical call."""
    user = User(
        id="user-a",
        account="user-a@example.test",
        nickname="user-a",
        password_hash="not-a-real-password-hash",
        role=UserRole.STUDENT,
    )
    db_session.add(user)
    db_session.commit()
    gateway = RoleScriptedGateway(
        {
            AgentRole.planner: [
                {
                    "action": "plan",
                    "complexity": "L3",
                    "success_criteria": ["完整 JD"],
                    "steps": [
                        {
                            "step_id": "discover",
                            "objective": "提取岗位",
                            "allowed_skills": ["job-discovery"],
                            "requires_verification": True,
                        }
                    ],
                }
            ],
            AgentRole.executor: [
                {"action": "call_tool", "tool_name": "fetch-job", "tool_input": {}},
                {"action": "complete", "summary": "信息不完整"},
                {"action": "complete", "summary": "信息已补齐"},
            ],
            AgentRole.verifier: [
                {
                    "action": "decide",
                    "verification_decision": "RETRY_EXECUTOR",
                    "feedback": "补充职责和任职要求。",
                },
                {"action": "decide", "verification_decision": "PASS"},
            ],
        }
    )
    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="fetch-job", skill_name="job-discovery", input_model=EmptyInput,
        output_model=FetchedJobOutput, allowed_roles=frozenset({AgentRole.executor}),
        handler=lambda _context, _payload: {
            "title": "AI Agent 开发工程师", "source_url": "https://jobs.example/retry",
            "content_hash": "e" * 64, "visible_text": "负责 Agent 应用开发。",
        },
    ))
    runtime = AgentRuntime(
        planner=PlannerAgent(gateway=gateway, tools=registry),
        executor=ExecutorAgent(gateway=gateway, tools=registry),
        verifier=VerifierAgent(gateway=gateway, tools=registry),
        agent_version="pev-test",
    )

    result = runtime.run(
        db_session,
        user_id=user.id,
        task=AgentTaskRequest(goal="找岗位", allowed_skills=["job-discovery"]),
    )

    assert result.status is RunStatus.succeeded
    assert gateway.states[AgentRole.executor][2]["context"]["verifier_feedback"] == [
        "补充职责和任职要求。"
    ]
    assert gateway.states[AgentRole.executor][2]["context"]["observed_public_evidence"][0][
        "source_url"
    ] == "https://jobs.example/retry"
    assert [observation["tool_name"] for observation in gateway.states[AgentRole.verifier][1][
        "execution"
    ]["observations"]] == ["fetch-job"]


def test_runtime_returns_verifier_replan_feedback_to_planner_as_new_revision(db_session) -> None:
    """A broken plan is replanned by Planner rather than mislabeled as execution failure."""
    user = User(
        id="user-a",
        account="user-a@example.test",
        nickname="user-a",
        password_hash="not-a-real-password-hash",
        role=UserRole.STUDENT,
    )
    db_session.add(user)
    db_session.commit()
    plan_decision = {
        "action": "plan",
        "complexity": "L3",
        "success_criteria": ["完整 JD"],
        "steps": [
            {
                "step_id": "discover",
                "objective": "提取岗位",
                "allowed_skills": ["job-discovery"],
                "requires_verification": True,
            }
        ],
    }
    gateway = RoleScriptedGateway(
        {
            AgentRole.planner: [plan_decision, plan_decision],
            AgentRole.executor: [
                {"action": "complete", "summary": "旧方案结果"},
                {"action": "complete", "summary": "新方案结果"},
            ],
            AgentRole.verifier: [
                {
                    "action": "decide",
                    "verification_decision": "REPLAN",
                    "feedback": "来源改版，需要重新规划提取路径。",
                },
                {"action": "decide", "verification_decision": "PASS"},
            ],
        }
    )
    registry = ToolRegistry()
    runtime = AgentRuntime(
        planner=PlannerAgent(gateway=gateway, tools=registry),
        executor=ExecutorAgent(gateway=gateway, tools=registry),
        verifier=VerifierAgent(gateway=gateway, tools=registry),
        agent_version="pev-test",
    )

    result = runtime.run(
        db_session,
        user_id=user.id,
        task=AgentTaskRequest(
            goal="找岗位",
            allowed_skills=["job-discovery"],
            budget={"max_agent_turns": 8, "max_tool_calls": 8, "max_replans": 1},
        ),
    )

    assert result.status is RunStatus.succeeded
    assert result.summary == "新方案结果"
    assert gateway.states[AgentRole.planner][1]["context"]["verifier_feedback"] == [
        "来源改版，需要重新规划提取路径。"
    ]
    assert [event.event_type for event in run_repository.list_events(db_session, result.run_id)].count(
        "plan_created"
    ) == 2


def test_runtime_replaces_model_artifact_claim_with_observed_public_evidence(db_session) -> None:
    """A model cannot invent an artifact URI when no tool observation supports it."""
    user = User(
        id="user-a", account="user-a@example.test", nickname="user-a",
        password_hash="not-a-real-password-hash", role=UserRole.STUDENT,
    )
    db_session.add(user)
    db_session.commit()
    gateway = RoleScriptedGateway(
        {
            AgentRole.planner: [{
                "action": "plan", "complexity": "L2", "success_criteria": ["有来源"],
                "steps": [{"step_id": "discover", "objective": "提取岗位", "allowed_skills": ["job-discovery"]}],
            }],
            AgentRole.executor: [
                {"action": "call_tool", "tool_name": "fetch-job", "tool_input": {}},
                {"action": "complete", "summary": "完成", "artifact_refs": [{"uri": "artifact://invented"}]},
            ],
            AgentRole.verifier: [],
        }
    )
    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="fetch-job", skill_name="job-discovery", input_model=EmptyInput,
        output_model=FetchedJobOutput, allowed_roles=frozenset({AgentRole.executor}),
        handler=lambda _context, _payload: {
            "title": "AI 应用开发", "source_url": "https://jobs.example/1",
            "content_hash": "b" * 64, "visible_text": "负责 Agent 应用开发与部署。",
        },
    ))
    runtime = AgentRuntime(
        planner=PlannerAgent(gateway=gateway, tools=registry),
        executor=ExecutorAgent(gateway=gateway, tools=registry),
        verifier=VerifierAgent(gateway=gateway, tools=registry), agent_version="pev-test",
    )

    result = runtime.run(
        db_session, user_id=user.id,
        task=AgentTaskRequest(goal="找岗位", allowed_skills=["job-discovery"]),
    )

    events = run_repository.list_events(db_session, result.run_id)
    observed = [event for event in events if event.event_type == "executor_tool_observation"]
    assert observed[0].payload_json["source_url"] == "https://jobs.example/1"
    assert observed[0].payload_json["content_hash"] == "b" * 64
    assert all("artifact://invented" not in str(event.payload_json) for event in events)
    step = db_session.scalar(select(AgentStep).where(AgentStep.run_id == result.run_id))
    assert step is not None
    assert step.output_artifact_refs_json == [{
        "artifact_id": step.output_artifact_refs_json[0]["artifact_id"],
        "artifact_type": "public_job_page",
        "tool": "fetch-job",
        "source_url": "https://jobs.example/1",
        "content_hash": "b" * 64,
    }]


def test_runtime_supplies_observed_public_evidence_to_the_next_planned_step(db_session) -> None:
    """Matching can reason over the actual JD found by the preceding discovery step."""
    user = User(
        id="user-a", account="user-a@example.test", nickname="user-a",
        password_hash="not-a-real-password-hash", role=UserRole.STUDENT,
    )
    db_session.add(user)
    db_session.commit()
    gateway = RoleScriptedGateway(
        {
            AgentRole.planner: [{
                "action": "plan", "complexity": "L2", "success_criteria": ["岗位匹配"],
                "steps": [
                    {"step_id": "discover", "objective": "抓取 JD", "allowed_skills": ["job-discovery"]},
                    {"step_id": "match", "objective": "匹配 JD", "allowed_skills": ["job-matching"]},
                ],
            }],
            AgentRole.executor: [
                {"action": "call_tool", "tool_name": "fetch-job", "tool_input": {}},
                {"action": "complete", "summary": "已发现岗位"},
                {"action": "complete", "summary": "已完成匹配"},
            ],
            AgentRole.verifier: [],
        }
    )
    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="fetch-job", skill_name="job-discovery", input_model=EmptyInput,
        output_model=FetchedJobOutput, allowed_roles=frozenset({AgentRole.executor}),
        handler=lambda _context, _payload: {
            "title": "AI Agent 开发工程师", "source_url": "https://jobs.example/1",
            "content_hash": "c" * 64, "visible_text": "负责 Agent 平台、RAG 与工具调用。",
        },
    ))
    runtime = AgentRuntime(
        planner=PlannerAgent(gateway=gateway, tools=registry),
        executor=ExecutorAgent(gateway=gateway, tools=registry),
        verifier=VerifierAgent(gateway=gateway, tools=registry), agent_version="pev-test",
    )

    result = runtime.run(
        db_session, user_id=user.id,
        task=AgentTaskRequest(
            goal="找并匹配 AI Agent 岗位",
            allowed_skills=["job-discovery", "job-matching"],
        ),
    )

    assert result.status is RunStatus.succeeded
    evidence = gateway.states[AgentRole.executor][2]["context"]["observed_public_evidence"]
    assert evidence == [{
        "artifact_id": evidence[0]["artifact_id"],
        "source_url": "https://jobs.example/1",
        "content_hash": "c" * 64,
        "title": "AI Agent 开发工程师",
    }]


def test_runtime_bounds_public_evidence_context_to_the_configured_character_limit(db_session) -> None:
    user = User(
        id="user-a", account="user-a@example.test", nickname="user-a",
        password_hash="not-a-real-password-hash", role=UserRole.STUDENT,
    )
    db_session.add(user)
    db_session.commit()
    run, task, _plan, _plan_step, step = _create_running_step(
        db_session, user, requires_verification=False
    )
    run_repository.create_evidence_artifact(
        db_session,
        run_id=run.id,
        step_id=step.id,
        source_url="https://jobs.example/large",
        content_hash="d" * 64,
        content_json={"visible_text": "x" * 48_001},
    )

    projected = AgentRuntime._with_observed_public_evidence(db_session, task, run.id)

    evidence = projected.context["observed_public_evidence"]
    assert "visible_text" not in evidence[0]


def test_tool_context_projects_structured_job_candidates_from_extract_artifacts(db_session) -> None:
    """Matching tools see per-job units from extract outputs, never one aggregated page."""
    user = User(
        id="user-a", account="user-a@example.test", nickname="user-a",
        password_hash="not-a-real-password-hash", role=UserRole.STUDENT,
    )
    db_session.add(user)
    db_session.commit()
    run, task, _plan, _plan_step, step = _create_running_step(
        db_session, user, requires_verification=False
    )
    run_repository.create_evidence_artifact(
        db_session,
        run_id=run.id,
        step_id=step.id,
        source_url="https://jobs.example/list",
        content_hash="e" * 64,
        content_json={"title": "蔚来校招", "visible_text": "岗位列表正文"},
    )
    structured = run_repository.create_artifact(
        db_session,
        run_id=run.id,
        step_id=step.id,
        artifact_type="structured_job_details",
        source_url="https://jobs.example/list",
        content_hash="s" * 64,
        content_json={"candidates": [
            {
                "title": "提前批-Agent开发工程师-NOMI",
                "apply_url": "https://jobs.example/apply/1",
                "locations": ["北京、上海"],
                "company_name": "自研 Agent harness 框架的全链路设计与开发",
                "responsibilities": "x" * 700,
                "requirements": "负责 Agent 开发。",
            },
            "not-a-dict",
            {
                "title": "无链接岗位",
                "locations": ["上海"],
            },
            {
                "title": 123,
                "apply_url": "https://jobs.example/apply/3",
                "locations": "上海",
                "responsibilities": "负责测试。",
            },
        ]},
    )

    context = AgentRuntime._tool_context(
        user_id=user.id, run_id=run.id, task=task, db=db_session
    )

    candidates = context.metadata["structured_job_candidates"]
    assert [c["artifact_id"] for c in candidates] == [structured.id, structured.id, structured.id]
    assert candidates[0]["title"] == "提前批-Agent开发工程师-NOMI"
    assert candidates[0]["source_url"] == "https://jobs.example/apply/1"
    assert candidates[0]["locations"] == ["北京、上海"]
    assert candidates[0]["company_name"] == "自研 Agent harness 框架的全链路设计与开发"
    assert len(candidates[0]["responsibilities"]) == 600  # bounded section
    assert candidates[0]["requirements"] == "负责 Agent 开发。"
    # Missing apply_url falls back to the artifact's own source_url.
    assert candidates[1]["source_url"] == "https://jobs.example/list"
    # Non-str title becomes None; non-list locations / non-str sections stay safe.
    assert candidates[2]["title"] is None
    assert candidates[2]["locations"] == []
    assert candidates[2]["responsibilities"] == "负责测试。"
    # The raw page evidence artifact contributes no candidates.
    assert all(c["content_hash"] == "s" * 64 for c in candidates)


def test_auto_tailoring_deliverable_targets_the_selected_candidate_id(db_session) -> None:
    """A multi-job artifact must retain the chosen candidate identity end to end."""
    user = User(
        id="user-candidate-id", account="candidate-id@example.test", nickname="candidate",
        password_hash="not-a-real-password-hash", role=UserRole.STUDENT,
    )
    db_session.add(user)
    db_session.commit()
    run, _task, _plan, _plan_step, step = _create_running_step(
        db_session, user, requires_verification=False
    )
    structured = run_repository.create_artifact(
        db_session,
        run_id=run.id,
        step_id=step.id,
        artifact_type="structured_job_details",
        source_url="https://jobs.example/list",
        content_hash="j" * 64,
        content_json={
            "candidates": [
                {
                    "title": "AI Agent 产品经理",
                    "responsibilities": "负责 AI 产品规划。",
                    "requirements": "要求产品经验。",
                },
                {
                    "title": "Java 后端开发工程师",
                    "responsibilities": "负责 Java 后端服务开发。",
                    "requirements": "熟悉 Java 与微服务。",
                },
            ]
        },
    )
    run_repository.create_artifact(
        db_session,
        run_id=run.id,
        step_id=step.id,
        artifact_type="job_matching_report",
        source_url="https://jobs.example/list",
        content_hash="m" * 64,
        content_json={
            "matches": [
                {
                    "artifact_id": structured.id,
                    "candidate_id": f"{structured.id}:candidate:1",
                    "source_url": "https://jobs.example/list",
                    "title": "Java 后端开发工程师",
                }
            ]
        },
    )
    executor = MagicMock()
    executor.invoke_registered_tool.return_value = ToolObservation(
        tool_name="build-resume-tailoring-brief",
        status="failed",
        error_code="captured_for_test",
    )
    runtime = AgentRuntime(
        planner=MagicMock(), executor=executor, verifier=MagicMock(), agent_version="pev-test"
    )
    task = AgentTaskRequest(
        goal="针对 Java 后端开发工程师岗位给出简历修改建议。",
        allowed_skills=["resume-tailoring"],
        private_context={"confirmed_profile_facts": {"skills": ["Java"]}},
    )
    plan_step = PlanStep(
        step_id="tailor",
        objective="生成 Java 后端岗位简历建议",
        allowed_skills=["resume-tailoring"],
    )

    runtime._auto_build_role_deliverable(
        db=db_session,
        run_id=run.id,
        task=task,
        plan_step=plan_step,
        persisted_step=step,
        context=ToolContext(user_id=user.id, run_id=run.id),
        artifact_refs=[],
        tool_budget=ToolCallBudget(2),
    )

    payload = executor.invoke_registered_tool.call_args.kwargs["payload"]
    assert payload["target_artifact_id"] == f"{structured.id}:candidate:1"


def test_auto_tailoring_deliverable_resolves_raw_page_match_without_structured_candidate(
    db_session,
) -> None:
    """A chained raw-page match remains a resolvable tailoring target."""
    user = User(
        id="user-raw-match-tailoring",
        account="raw-match-tailoring@example.test",
        nickname="raw-match",
        password_hash="not-a-real-password-hash",
        role=UserRole.STUDENT,
    )
    db_session.add(user)
    db_session.commit()
    run, _task, _plan, _plan_step, step = _create_running_step(
        db_session, user, requires_verification=False
    )
    raw_page = run_repository.create_artifact(
        db_session,
        run_id=run.id,
        step_id=step.id,
        artifact_type="public_job_page",
        source_url="https://jobs.example/agent-intern",
        content_hash="p" * 64,
        content_json={
            "quality": "jd_complete",
            "title": "AI Agent 研发实习生",
            "visible_text": (
                "AI Agent 研发实习生\n岗位职责：负责 Agent 应用开发。\n"
                "任职要求：熟悉 Python 与 RAG。"
            ),
        },
    )
    run_repository.create_artifact(
        db_session,
        run_id=run.id,
        step_id=step.id,
        artifact_type="job_matching_report",
        source_url=raw_page.source_url,
        content_hash="m" * 64,
        content_json={
            "matches": [
                {
                    "artifact_id": raw_page.id,
                    "candidate_id": None,
                    "source_url": raw_page.source_url,
                    "title": "AI Agent 研发实习生",
                }
            ]
        },
    )
    executor = MagicMock()
    executor.invoke_registered_tool.return_value = ToolObservation(
        tool_name="build-resume-tailoring-brief",
        status="failed",
        error_code="captured_for_test",
    )
    runtime = AgentRuntime(
        planner=MagicMock(),
        executor=executor,
        verifier=MagicMock(),
        agent_version="pev-test",
    )
    task = AgentTaskRequest(
        goal="基于上一环节最匹配的岗位生成简历定制化修改建议。",
        allowed_skills=["resume-tailoring"],
        private_context={"confirmed_profile_facts": {"skills": ["Python", "RAG"]}},
    )
    plan_step = PlanStep(
        step_id="tailor-raw-match",
        objective="生成上一环节匹配岗位的简历建议",
        allowed_skills=["resume-tailoring"],
    )

    runtime._auto_build_role_deliverable(
        db=db_session,
        run_id=run.id,
        task=task,
        plan_step=plan_step,
        persisted_step=step,
        context=ToolContext(user_id=user.id, run_id=run.id),
        artifact_refs=[],
        tool_budget=ToolCallBudget(2),
    )

    payload = executor.invoke_registered_tool.call_args.kwargs["payload"]
    assert payload["target_artifact_id"] == raw_page.id


def test_auto_public_search_prefers_a_targeted_discovery_hint(db_session) -> None:
    user = User(
        id="user-search-hint", account="search-hint@example.test", nickname="search",
        password_hash="not-a-real-password-hash", role=UserRole.STUDENT,
    )
    db_session.add(user)
    db_session.commit()
    run, _task, _plan, _plan_step, step = _create_running_step(
        db_session, user, requires_verification=False
    )
    executor = MagicMock()
    executor.invoke_registered_tool.return_value = ToolObservation(
        tool_name="search-public-job-pages",
        status="failed",
        error_code="no_public_results",
    )
    runtime = AgentRuntime(
        planner=MagicMock(), executor=executor, verifier=MagicMock(), agent_version="pev-test"
    )
    hint = "百度 AIGC 产品经理 应届生 校招 岗位详情 官方招聘"
    context = ToolContext(
        user_id=user.id,
        run_id=run.id,
        metadata={
            "public_search_query_hashes": [],
            "discovery_search_hints": [hint],
        },
    )

    runtime._auto_search_and_fetch(
        db=db_session,
        run_id=run.id,
        persisted_step=step,
        context=context,
        tool_budget=ToolCallBudget(2),
        task_goal="百度、美团、小米哪个大厂最近有适合我的 AIGC 产品经理应届生岗位？",
        step_id="discover",
    )

    assert executor.invoke_registered_tool.call_args.kwargs["payload"]["query"] == hint


def test_auto_public_search_tries_next_hint_after_an_empty_route(db_session) -> None:
    user = User(
        id="user-search-next", account="search-next@example.test", nickname="next",
        password_hash="not-a-real-password-hash", role=UserRole.STUDENT,
    )
    db_session.add(user)
    db_session.commit()
    run, _task, _plan, _plan_step, step = _create_running_step(
        db_session, user, requires_verification=False
    )
    executor = MagicMock()
    executor.invoke_registered_tool.side_effect = [
        ToolObservation(
            tool_name="search-public-job-pages",
            status="succeeded",
            output={
                "query": "华丞电子 AI 应用校招",
                "source_url": "https://search.example/first",
                "content_hash": "a" * 64,
                "results": [],
            },
        ),
        ToolObservation(
            tool_name="search-public-job-pages",
            status="succeeded",
            output={
                "query": "BIGO AI 应用校招",
                "source_url": "https://search.example/second",
                "content_hash": "b" * 64,
                "results": [
                    {
                        "title": "AI 应用开发工程师",
                        "url": "https://jobs.example/ai",
                        "snippet": "校招岗位",
                    }
                ],
            },
        ),
        ToolObservation(
            tool_name="fetch-public-job-pages",
            status="failed",
            error_code="public_fetch_failed",
        ),
    ]
    runtime = AgentRuntime(
        planner=MagicMock(), executor=executor, verifier=MagicMock(), agent_version="pev-test"
    )
    hints = ["华丞电子 AI 应用校招", "BIGO AI 应用校招"]

    runtime._auto_search_and_fetch(
        db=db_session,
        run_id=run.id,
        persisted_step=step,
        context=ToolContext(
            user_id=user.id,
            run_id=run.id,
            metadata={"public_search_query_hashes": [], "discovery_search_hints": hints},
        ),
        tool_budget=ToolCallBudget(5),
        task_goal="查找 AI 应用校招岗位",
        step_id="discover",
    )

    search_queries = [
        call.kwargs["payload"]["query"]
        for call in executor.invoke_registered_tool.call_args_list
        if call.kwargs["name"] == "search-public-job-pages"
    ]
    assert search_queries == hints


def test_auto_discovery_searches_when_sheet_and_model_search_have_no_urls(
    db_session,
) -> None:
    user = User(
        id="user-empty-routes", account="empty-routes@example.test", nickname="empty",
        password_hash="not-a-real-password-hash", role=UserRole.STUDENT,
    )
    db_session.add(user)
    db_session.commit()
    run, _task, _plan, plan_step, step = _create_running_step(
        db_session, user, requires_verification=False
    )
    executor = MagicMock()
    executor.invoke_registered_tool.return_value = ToolObservation(
        tool_name="search-public-job-pages",
        status="failed",
        error_code="no_public_results",
    )
    runtime = AgentRuntime(
        planner=MagicMock(), executor=executor, verifier=MagicMock(), agent_version="pev-test"
    )
    task = AgentTaskRequest(
        goal="字节跳动、腾讯、百度有哪些 AIGC 产品经理校招岗位？",
        allowed_skills=["job-discovery"],
    )
    empty_sheet = ToolObservation(
        tool_name="query-career-sheet-records",
        status="succeeded",
        output={"records": [], "source_url": "https://docs.example/sheet", "content_hash": "s" * 64},
    )

    runtime._auto_recover_discovery_evidence(
        db=db_session,
        run_id=run.id,
        task=task,
        plan_step=plan_step,
        persisted_step=step,
        context=ToolContext(
            user_id=user.id,
            run_id=run.id,
            metadata={"task_goal": task.goal, "public_search_query_hashes": []},
        ),
        observations=[empty_sheet],
        artifact_refs=[],
        tool_budget=ToolCallBudget(3),
    )

    assert executor.invoke_registered_tool.call_args.kwargs["name"] == (
        "search-public-job-pages"
    )


def test_auto_discovery_rehydrates_upstream_sheet_urls_from_artifact_ref(
    db_session,
) -> None:
    user = User(
        id="user-upstream-routes",
        account="upstream-routes@example.test",
        nickname="routes",
        password_hash="not-a-real-password-hash",
        role=UserRole.STUDENT,
    )
    db_session.add(user)
    db_session.commit()
    run, _task, _plan, plan_step, step = _create_running_step(
        db_session, user, requires_verification=False
    )
    route_url = "https://jobs.example/company-ai"
    artifact = run_repository.create_artifact(
        db_session,
        run_id=run.id,
        step_id=step.id,
        artifact_type="job_search_results",
        source_url="https://docs.example/recent-companies",
        content_hash="r" * 64,
        content_json={
            "query": {"recent_days": 1},
            "results": [
                {
                    "company_name": "示例科技",
                    "apply_url": f"{route_url} {route_url}",
                    "prior_metadata": {"apply_url": f"{route_url} {route_url}"},
                }
            ],
        },
    )
    executor = MagicMock()
    executor.invoke_registered_tool.return_value = ToolObservation(
        tool_name="fetch-public-job-pages",
        status="succeeded",
        output={
            "pages": [
                {
                    "source_url": route_url,
                    "content_hash": "p" * 64,
                    "visible_text": "岗位职责：负责 AI 产品。任职要求：本科及以上。",
                    "quality": "jd_complete",
                }
            ]
        },
    )
    runtime = AgentRuntime(
        planner=MagicMock(),
        executor=executor,
        verifier=MagicMock(),
        agent_version="pev-test",
    )
    task = AgentTaskRequest(
        goal="最近一天更新的公司有哪些 AI 岗位？",
        allowed_skills=["job-discovery"],
    )

    runtime._auto_recover_discovery_evidence(
        db=db_session,
        run_id=run.id,
        task=task,
        plan_step=plan_step,
        persisted_step=step,
        context=ToolContext(
            user_id=user.id,
            run_id=run.id,
            metadata={"task_goal": task.goal, "public_search_query_hashes": []},
        ),
        observations=[],
        artifact_refs=[
            {
                "artifact_id": artifact.id,
                "artifact_type": "job_search_results",
                "tool": "query-career-sheet-records",
                "source_url": artifact.source_url,
                "content_hash": artifact.content_hash,
                "semantic_valid": "true",
            }
        ],
        tool_budget=ToolCallBudget(2),
    )

    call = executor.invoke_registered_tool.call_args
    assert call.kwargs["name"] == "fetch-public-job-pages"
    assert call.kwargs["payload"] == {"urls": [route_url]}


def test_auto_discovery_tries_official_seed_after_routed_urls_are_empty(
    db_session,
) -> None:
    user = User(
        id="user-routed-official-seed",
        account="routed-official@example.test",
        nickname="official",
        password_hash="not-a-real-password-hash",
        role=UserRole.STUDENT,
    )
    db_session.add(user)
    db_session.commit()
    run, _task, _plan, plan_step, step = _create_running_step(
        db_session, user, requires_verification=False
    )
    routed_url = "https://jobs.example/empty"
    official_url = "https://job.xiaohongshu.com/campus"
    executor = MagicMock()
    executor.invoke_registered_tool.side_effect = [
        ToolObservation(
            tool_name="fetch-public-job-pages",
            status="succeeded",
            output={
                "pages": [],
                "failures": [
                    {"source_url": routed_url, "error_code": "empty_public_page"}
                ],
            },
        ),
        ToolObservation(
            tool_name="fetch-public-job-pages",
            status="succeeded",
            output={
                "pages": [
                    {
                        "source_url": official_url,
                        "content_hash": "o" * 64,
                        "visible_text": "岗位职责：负责 AI 产品。任职要求：应届生可投。",
                        "quality": "jd_complete",
                    }
                ]
            },
        ),
    ]
    runtime = AgentRuntime(
        planner=MagicMock(),
        executor=executor,
        verifier=MagicMock(),
        agent_version="pev-test",
    )
    task = AgentTaskRequest(
        goal="快手、小红书有没有 AI 产品经理应届生岗位？",
        allowed_skills=["job-discovery"],
    )
    routed_observation = ToolObservation(
        tool_name="search-public-job-pages",
        status="succeeded",
        output={
            "query": "小红书 AI 产品经理",
            "source_url": "https://search.example/xhs",
            "content_hash": "s" * 64,
            "results": [{"title": "岗位", "url": routed_url}],
        },
    )

    runtime._auto_recover_discovery_evidence(
        db=db_session,
        run_id=run.id,
        task=task,
        plan_step=plan_step,
        persisted_step=step,
        context=ToolContext(
            user_id=user.id,
            run_id=run.id,
            metadata={"task_goal": task.goal, "public_search_query_hashes": []},
        ),
        observations=[routed_observation],
        artifact_refs=[],
        tool_budget=ToolCallBudget(3),
    )

    calls = executor.invoke_registered_tool.call_args_list
    assert [call.kwargs["payload"] for call in calls] == [
        {"urls": [routed_url]},
        {"urls": [official_url]},
    ]


def test_auto_discovery_prefetches_named_official_company_seed_before_search(
    db_session,
) -> None:
    user = User(
        id="user-official-seed",
        account="official-seed@example.test",
        nickname="seed",
        password_hash="not-a-real-password-hash",
        role=UserRole.STUDENT,
    )
    db_session.add(user)
    db_session.commit()
    run, _task, _plan, plan_step, step = _create_running_step(
        db_session, user, requires_verification=False
    )
    executor = MagicMock()
    executor.invoke_registered_tool.return_value = ToolObservation(
        tool_name="fetch-public-job-pages",
        status="succeeded",
        output={
            "pages": [
                {
                    "source_url": "https://campus.meituan.com/",
                    "content_hash": "m" * 64,
                    "quality": "jd_complete",
                    "title": "AIGC 产品经理",
                    "content": "岗位职责：负责 AIGC 产品；任职要求：2027 届毕业生。",
                }
            ]
        },
    )
    runtime = AgentRuntime(
        planner=MagicMock(),
        executor=executor,
        verifier=MagicMock(),
        agent_version="pev-test",
    )
    task = AgentTaskRequest(
        goal="百度、美团、小米哪个大厂有 AIGC 产品经理校招岗位？",
        allowed_skills=["job-discovery"],
    )

    runtime._auto_recover_discovery_evidence(
        db=db_session,
        run_id=run.id,
        task=task,
        plan_step=plan_step,
        persisted_step=step,
        context=ToolContext(
            user_id=user.id,
            run_id=run.id,
            metadata={"task_goal": task.goal, "public_search_query_hashes": []},
        ),
        observations=[],
        artifact_refs=[],
        tool_budget=ToolCallBudget(3),
    )

    call = executor.invoke_registered_tool.call_args
    assert call.kwargs["name"] == "fetch-public-job-pages"
    assert call.kwargs["payload"] == {"urls": ["https://campus.meituan.com/"]}


def test_runtime_auto_extracts_pages_recovered_in_the_same_executor_pass(
    db_session,
) -> None:
    user = User(
        id="user-recovery-extract",
        account="recovery-extract@example.test",
        nickname="extract",
        password_hash="not-a-real-password-hash",
        role=UserRole.STUDENT,
    )
    db_session.add(user)
    db_session.commit()
    run, task, _plan, _plan_step, step = _create_running_step(
        db_session, user, requires_verification=False
    )
    source_url = "https://jobs.example/ai-pm"
    content_hash = "p" * 64
    visible_text = "岗位职责：负责大模型产品。任职要求：在校生可投。"
    fetch_observation = ToolObservation(
        tool_name="fetch-public-job-pages",
        status="succeeded",
        output={
            "pages": [
                {
                    "source_url": source_url,
                    "content_hash": content_hash,
                    "visible_text": visible_text,
                    "quality": "jd_complete",
                }
            ]
        },
    )
    extract_observation = ToolObservation(
        tool_name="extract-observed-job-details-batch",
        status="succeeded",
        output={
            "details": [
                {
                    "source_url": source_url,
                    "content_hash": content_hash,
                    "source_quality": "jd_complete",
                    "candidates": [
                        {
                            "title": "AIGC 产品经理实习生",
                            "responsibilities": "负责大模型产品",
                            "requirements": "在校生可投",
                        }
                    ],
                }
            ]
        },
    )
    executor = MagicMock()
    executor.run.return_value = ExecutorResult(
        status="needs_user",
        user_question="原始来源受阻。",
    )
    executor.invoke_registered_tool.return_value = extract_observation
    runtime = AgentRuntime(
        planner=MagicMock(),
        executor=executor,
        verifier=MagicMock(),
        agent_version="pev-test",
    )
    def recover_page(**_kwargs):  # noqa: ANN003
        page = run_repository.create_evidence_artifact(
            db_session,
            run_id=run.id,
            step_id=step.id,
            source_url=source_url,
            content_hash=content_hash,
            content_json={
                "title": "AIGC 产品经理实习生",
                "visible_text": visible_text,
                "quality": "jd_complete",
            },
        )
        return [fetch_observation], [
            {
                "artifact_id": page.id,
                "artifact_type": "public_job_page",
                "tool": "fetch-public-job-pages",
                "source_url": page.source_url,
                "content_hash": page.content_hash,
                "quality": "jd_complete",
            }
        ]

    runtime._auto_recover_discovery_evidence = recover_page
    plan_step = PlanStep(
        step_id="discover",
        objective="抓取并结构化 AIGC 产品经理岗位",
        allowed_skills=["job-discovery"],
        outputs=[
            {
                "name": "structured_job_details",
                "artifact_type": "structured_job_details",
            }
        ],
    )
    plan = ExecutionPlan(
        task=task,
        created_by=AgentRole.planner,
        complexity=ComplexityLevel.L2,
        success_criteria=["结构化 JD"],
        steps=[plan_step],
    )

    runtime._run_step(
        db=db_session,
        run_id=run.id,
        task=task,
        plan=plan,
        plan_step=plan_step,
        persisted_step=step,
        context=ToolContext(user_id=user.id, run_id=run.id),
        trace=lambda *_args: None,
        tool_budget=ToolCallBudget(4),
        turn_budget=AgentTurnBudget(4),
    )

    assert executor.invoke_registered_tool.call_args.kwargs["name"] == (
        "extract-observed-job-details-batch"
    )
    artifacts = list(
        db_session.scalars(
            select(AgentArtifact).where(AgentArtifact.run_id == run.id)
        )
    )
    assert "structured_job_details" in {
        artifact.artifact_type for artifact in artifacts
    }


def test_auto_discovery_tries_named_official_seed_after_list_shell(
    db_session,
) -> None:
    user = User(
        id="user-seed-after-list",
        account="seed-after-list@example.test",
        nickname="seed-list",
        password_hash="not-a-real-password-hash",
        role=UserRole.STUDENT,
    )
    db_session.add(user)
    db_session.commit()
    run, _task, _plan, plan_step, step = _create_running_step(
        db_session, user, requires_verification=False
    )
    search_shell = "https://careers.tencent.com/search.html?keyword=AIGC"
    query_seed = (
        "https://careers.tencent.com/tencentcareer/api/post/Query"
        "?keyword=AIGC&pageIndex=1&pageSize=10&language=zh-cn&area=cn"
    )
    executor = MagicMock()
    executor.invoke_registered_tool.side_effect = [
        ToolObservation(
            tool_name="fetch-public-job-pages",
            status="succeeded",
            output={
                "pages": [
                    {
                        "source_url": search_shell,
                        "content_hash": "s" * 64,
                        "quality": "list_only",
                        "visible_text": "招聘列表 " * 50,
                    }
                ]
            },
        ),
        ToolObservation(
            tool_name="fetch-public-job-pages",
            status="succeeded",
            output={
                "pages": [
                    {
                        "source_url": query_seed,
                        "content_hash": "q" * 64,
                        "quality": "jd_complete",
                        "visible_text": "岗位职责：负责 AIGC；任职要求：熟悉产品设计。",
                    }
                ]
            },
        ),
    ]
    runtime = AgentRuntime(
        planner=MagicMock(),
        executor=executor,
        verifier=MagicMock(),
        agent_version="pev-test",
    )
    task = AgentTaskRequest(
        goal="腾讯有 AIGC 产品经理岗位吗？请在腾讯招聘官网核实。",
        allowed_skills=["job-discovery"],
    )

    runtime._auto_recover_discovery_evidence(
        db=db_session,
        run_id=run.id,
        task=task,
        plan_step=plan_step,
        persisted_step=step,
        context=ToolContext(
            user_id=user.id,
            run_id=run.id,
            metadata={"task_goal": task.goal, "public_search_query_hashes": []},
        ),
        observations=[],
        artifact_refs=[
            {
                "artifact_type": "public_job_page",
                "quality": "list_only",
                "source_url": search_shell,
            }
        ],
        tool_budget=ToolCallBudget(3),
    )

    calls = executor.invoke_registered_tool.call_args_list
    assert [call.kwargs["name"] for call in calls] == [
        "fetch-public-job-pages",
        "fetch-public-job-pages",
    ]
    assert calls[1].kwargs["payload"] == {"urls": [query_seed]}


def test_auto_discovery_prioritizes_named_source_mirror_over_unrelated_complete_page(
    db_session,
) -> None:
    """A complete page from another source cannot suppress an explicit source constraint."""
    user = User(
        id="user-priority-source-mirror",
        account="priority-source-mirror@example.test",
        nickname="source-mirror",
        password_hash="not-a-real-password-hash",
        role=UserRole.STUDENT,
    )
    db_session.add(user)
    db_session.commit()
    run, _task, _plan, plan_step, step = _create_running_step(
        db_session, user, requires_verification=False
    )
    mirror_url = agent_runtime_module._public_source_mirror_seed_urls(
        "在猎聘网产品经理专区找北京的 AIGC 产品经理（应届生）岗位。"
    )[0]
    executor = MagicMock()
    executor.invoke_registered_tool.return_value = ToolObservation(
        tool_name="fetch-public-job-pages",
        status="succeeded",
        output={
            "pages": [
                {
                    "source_url": "https://cn.linkedin.com/jobs/view/123",
                    "content_hash": "m" * 64,
                    "quality": "jd_complete",
                    "visible_text": "AI产品经理实习生 北京 该职位来源于猎聘 岗位职责 任职要求",
                }
            ]
        },
    )
    runtime = AgentRuntime(
        planner=MagicMock(),
        executor=executor,
        verifier=MagicMock(),
        agent_version="pev-test",
    )
    task = AgentTaskRequest(
        goal="在猎聘网产品经理专区找北京的 AIGC 产品经理（应届生）岗位。",
        allowed_skills=["job-discovery"],
    )
    unrelated_observation = ToolObservation(
        tool_name="fetch-public-job-pages",
        status="succeeded",
        output={
            "pages": [
                {
                    "source_url": "https://agirobot.jobs.feishu.cn/s/unrelated",
                    "content_hash": "u" * 64,
                    "quality": "jd_complete",
                    "visible_text": "机器人产品实习生 上海 岗位职责 任职要求",
                }
            ]
        },
    )

    runtime._auto_recover_discovery_evidence(
        db=db_session,
        run_id=run.id,
        task=task,
        plan_step=plan_step,
        persisted_step=step,
        context=ToolContext(
            user_id=user.id,
            run_id=run.id,
            metadata={"task_goal": task.goal, "public_search_query_hashes": []},
        ),
        observations=[unrelated_observation],
        artifact_refs=[],
        tool_budget=ToolCallBudget(3),
    )

    assert executor.invoke_registered_tool.call_args.kwargs["payload"] == {
        "urls": [mirror_url]
    }


def test_auto_discovery_prioritizes_exact_role_seed_over_unrelated_complete_page(
    db_session,
) -> None:
    """A multi-role page cannot suppress an exact public JD requested by role."""
    user = User(
        id="user-priority-role-seed",
        account="priority-role-seed@example.test",
        nickname="role-seed",
        password_hash="not-a-real-password-hash",
        role=UserRole.STUDENT,
    )
    db_session.add(user)
    db_session.commit()
    run, _task, _plan, plan_step, step = _create_running_step(
        db_session, user, requires_verification=False
    )
    goal = (
        "给出 AI 应用开发实习生岗位的面试建议，包括常见问题与回答要点。"
        "请先找到一份该岗位的公开 JD 作为依据。"
    )
    role_seed_url = agent_runtime_module._requested_role_seed_urls(goal)[0]
    executor = MagicMock()
    executor.invoke_registered_tool.return_value = ToolObservation(
        tool_name="fetch-public-job-pages",
        status="succeeded",
        output={
            "pages": [
                {
                    "source_url": role_seed_url,
                    "content_hash": "r" * 64,
                    "quality": "jd_complete",
                    "visible_text": (
                        "AI应用开发实习生 职位已下线 岗位职责 任职要求 "
                        "AI Agent Python RAG"
                    ),
                }
            ]
        },
    )
    runtime = AgentRuntime(
        planner=MagicMock(),
        executor=executor,
        verifier=MagicMock(),
        agent_version="pev-test",
    )
    task = AgentTaskRequest(
        goal=goal,
        allowed_skills=["job-discovery", "career-planning"],
    )
    unrelated_observation = ToolObservation(
        tool_name="fetch-public-job-pages",
        status="succeeded",
        output={
            "pages": [
                {
                    "source_url": "https://moonton.jobs.feishu.cn/s/unrelated",
                    "content_hash": "u" * 64,
                    "quality": "jd_complete",
                    "visible_text": (
                        "AI Agent 产品经理 校园招聘 岗位职责 任职要求"
                    ),
                }
            ]
        },
    )

    runtime._auto_recover_discovery_evidence(
        db=db_session,
        run_id=run.id,
        task=task,
        plan_step=plan_step,
        persisted_step=step,
        context=ToolContext(
            user_id=user.id,
            run_id=run.id,
            metadata={"task_goal": task.goal, "public_search_query_hashes": []},
        ),
        observations=[unrelated_observation],
        artifact_refs=[],
        tool_budget=ToolCallBudget(3),
    )

    assert executor.invoke_registered_tool.call_args.kwargs["payload"] == {
        "urls": [role_seed_url]
    }


def test_sheet_records_produce_relevant_company_search_hints() -> None:
    observation = ToolObservation(
        tool_name="query-career-sheet-records",
        status="succeeded",
        output={
            "records": [
                {"company_name": "施耐德电气", "industry": "电力电气"},
                {"company_name": "BIGO", "industry": "互联网"},
                {"company_name": "华丞电子", "industry": "人工智能/芯片"},
            ]
        },
    )

    hints = agent_runtime_module._discovery_search_hints(
        "最近3天更新的公司里有没有适合我的 AI 算法/AI 应用校招岗位？",
        [observation],
    )

    assert hints[0].startswith("华丞电子 AI 算法 AI 应用")
    assert any(hint.startswith("BIGO ") for hint in hints)


def test_discovery_search_hints_keep_explicit_kuaishou_and_xiaohongshu_scope() -> None:
    hints = agent_runtime_module._discovery_search_hints(
        "快手、小红书有没有 AI 产品经理（应届生）岗位？请核实投递链接。",
        [],
    )

    assert hints == [
        "快手 产品经理 招聘 岗位职责",
        "小红书 产品经理 招聘 岗位职责",
    ]


def test_goal_role_keywords_recognize_ai_application_development() -> None:
    assert AgentRuntime._goal_role_keywords("AI 应用开发实习生面试准备") == [
        "AI",
        "应用开发",
        "Agent",
        "智能体",
    ]


def test_official_company_seed_urls_builds_tencent_query_from_explicit_role() -> None:
    assert agent_runtime_module._official_company_seed_urls(
        "在腾讯招聘官网搜索 AIGC 产品经理岗位并核实详情。"
    ) == [
        "https://careers.tencent.com/tencentcareer/api/post/Query"
        "?keyword=AIGC&pageIndex=1&pageSize=10&language=zh-cn&area=cn"
    ]


def test_observed_company_seed_urls_use_only_sheet_company_evidence() -> None:
    observations = [
        ToolObservation(
            tool_name="query-career-sheet-records",
            status="succeeded",
            output={
                "records": [
                    {
                        "company_name": "倍漾量化",
                        "apply_url": "https://mp.weixin.qq.com/s/example",
                    }
                ]
            },
        ),
        ToolObservation(
            tool_name="search-public-job-pages",
            status="succeeded",
            output={
                "results": [
                    {
                        "title": "倍漾量化招聘",
                        "url": "https://untrusted.example/jobs",
                    }
                ]
            },
        ),
    ]

    assert agent_runtime_module._observed_company_seed_urls(observations) == [
        "https://www.baiontcapital.com/careers.html"
    ]


def test_trusted_discovery_seed_urls_include_observed_sheet_company() -> None:
    observation = ToolObservation(
        tool_name="query-career-sheet-records",
        status="succeeded",
        output={"records": [{"company_name": "南京倍漾量化投资管理有限公司"}]},
    )

    assert agent_runtime_module._trusted_discovery_seed_urls(
        "最近3天更新的公司中查找 AI 算法校招岗位。", [observation]
    ) == ["https://www.baiontcapital.com/careers.html"]


def test_source_mirror_seed_urls_builds_public_liepin_provenance_search() -> None:
    urls = agent_runtime_module._public_source_mirror_seed_urls(
        "在猎聘网产品经理专区找北京的 AIGC 产品经理（应届生）岗位。"
    )

    assert urls == [
        "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
        "?keywords=AI%E4%BA%A7%E5%93%81%E7%BB%8F%E7%90%86%E5%AE%9E%E4%B9%A0%E7%94%9F"
        "&location=Beijing%2C+China&geoId=103873152&start=0"
    ]


def test_requested_role_seed_urls_include_exact_ai_application_intern_jd() -> None:
    assert agent_runtime_module._requested_role_seed_urls(
        "请先找一份 AI 应用开发实习生公开 JD，再给出面试建议。"
    ) == [
        "https://24365.smartedu.cn/student/jobs/"
        "SvSaumv8prNxWdGTQbF9mh/detail.html"
    ]
    assert agent_runtime_module._requested_role_seed_urls(
        "请找一份 Java 后端开发工程师公开 JD。"
    ) == [
        "https://app.mokahr.com/campus-recruitment/tal/146599"
        "?recommendCode=DSXc7DBC#/jobs"
    ]
    assert agent_runtime_module._requested_role_seed_urls(
        "请搜索 Java 编程学习资料。"
    ) == []


def test_discovery_search_hints_preserve_named_source_and_location() -> None:
    hints = agent_runtime_module._discovery_search_hints(
        "在猎聘网产品经理专区找北京的 AIGC 产品经理（应届生）岗位。",
        [],
    )

    assert hints[0] == (
        "site:liepin.com AIGC 产品经理 北京 应届生 校招 岗位详情 官方招聘"
    )


def test_tool_context_structured_candidates_empty_without_extract_artifacts(db_session) -> None:
    """Runs without a structured extraction keep the raw-evidence fallback path."""
    user = User(
        id="user-a", account="user-a@example.test", nickname="user-a",
        password_hash="not-a-real-password-hash", role=UserRole.STUDENT,
    )
    db_session.add(user)
    db_session.commit()
    run, task, _plan, _plan_step, _step = _create_running_step(
        db_session, user, requires_verification=False
    )

    context = AgentRuntime._tool_context(
        user_id=user.id, run_id=run.id, task=task, db=db_session
    )

    assert context.metadata["structured_job_candidates"] == []


def test_structured_candidates_keep_source_page_quality_for_matching(db_session) -> None:
    user = User(
        id="user-quality", account="user-quality@example.test", nickname="quality",
        password_hash="not-a-real-password-hash", role=UserRole.STUDENT,
    )
    db_session.add(user)
    db_session.commit()
    run, task, _plan, _plan_step, step = _create_running_step(
        db_session, user, requires_verification=False
    )
    run_repository.create_artifact(
        db_session,
        run_id=run.id,
        step_id=step.id,
        artifact_type="public_job_page",
        source_url="https://jobs.example/list",
        content_hash="page" * 16,
        content_json={
            "visible_text": "招聘首页 浏览岗位 联系我们",
            "quality": "list_only",
        },
    )
    run_repository.create_artifact(
        db_session,
        run_id=run.id,
        step_id=step.id,
        artifact_type="structured_job_details",
        source_url="https://jobs.example/list",
        content_hash="structured" * 8,
        content_json={"candidates": [{"title": "候选岗位", "requirements": "Python"}]},
    )

    context = AgentRuntime._tool_context(
        user_id=user.id, run_id=run.id, task=task, db=db_session
    )
    assert context.metadata["structured_job_candidates"][0]["source_quality"] == "list_only"


def test_runtime_persists_every_page_from_one_batch_fetch_observation(db_session) -> None:
    user = User(
        id="user-a", account="user-a@example.test", nickname="user-a",
        password_hash="not-a-real-password-hash", role=UserRole.STUDENT,
    )
    db_session.add(user)
    db_session.commit()
    run, _task, _plan, _plan_step, step = _create_running_step(
        db_session, user, requires_verification=False
    )
    execution = ExecutorResult(
        status="succeeded",
        summary="已抓取两页",
        observations=[ToolObservation(
            tool_name="fetch-public-job-pages",
            status="succeeded",
            output={"pages": [
                {
                    "artifact_id": "observed:a", "source_url": "https://jobs.example/a",
                    "title": "岗位 A", "visible_text": "职责 A", "content_hash": "a" * 64,
                },
                {
                    "artifact_id": "observed:b", "source_url": "https://jobs.example/b",
                    "title": "岗位 B", "visible_text": "职责 B", "content_hash": "b" * 64,
                },
                "malformed-page",
            ]},
        )],
    )

    refs = AgentRuntime._persist_observed_evidence(db_session, run.id, step, execution)

    assert len(refs) == 2
    assert [artifact.content_json["title"] for artifact in db_session.scalars(
        select(AgentArtifact).where(AgentArtifact.run_id == run.id)
    )] == ["岗位 A", "岗位 B"]


def test_runtime_caps_persisted_page_text(db_session) -> None:
    user = User(
        id="user-a", account="user-a@example.test", nickname="user-a",
        password_hash="not-a-real-password-hash", role=UserRole.STUDENT,
    )
    db_session.add(user)
    db_session.commit()
    run, _task, _plan, _plan_step, step = _create_running_step(
        db_session, user, requires_verification=False
    )
    execution = ExecutorResult(
        status="succeeded",
        summary="已抓取超长页面",
        observations=[ToolObservation(
            tool_name="fetch-public-job-pages",
            status="succeeded",
            output={"visible_text": "x" * 40_000, "source_url": "https://jobs.example/a", "content_hash": "a" * 64},
        )],
    )

    AgentRuntime._persist_observed_evidence(db_session, run.id, step, execution)

    artifact = db_session.scalar(
        select(AgentArtifact).where(AgentArtifact.run_id == run.id)
    )
    assert artifact is not None
    assert len(artifact.content_json["visible_text"]) == 32_000


def test_runtime_records_a_model_gateway_failure_as_a_safe_failed_run(db_session) -> None:
    """A provider outage cannot escape the harness as an untracked API failure."""
    user = User(
        id="user-a", account="user-a@example.test", nickname="user-a",
        password_hash="not-a-real-password-hash", role=UserRole.STUDENT,
    )
    db_session.add(user)
    db_session.commit()
    gateway = FailingGateway()
    registry = ToolRegistry()
    runtime = AgentRuntime(
        planner=PlannerAgent(gateway=gateway, tools=registry),
        executor=ExecutorAgent(gateway=gateway, tools=registry),
        verifier=VerifierAgent(gateway=gateway, tools=registry), agent_version="pev-test",
    )

    result = runtime.run(
        db_session, user_id=user.id,
        task=AgentTaskRequest(goal="找 AI Agent 岗位", allowed_skills=["job-discovery"]),
    )

    assert (result.status, result.error_code) == (RunStatus.failed, "model_request_failed")
    events = run_repository.list_events(db_session, result.run_id)
    assert events[-1].payload_json["error_code"] == "model_request_failed"
    assert events[-1].payload_json["failure_class"] == "model_or_verifier_decision"


def test_runtime_degrades_planner_invalid_model_to_waiting_for_user(db_session) -> None:
    """Persistently invalid planner output waits for a human retry, never fails the run."""
    user = User(
        id="user-a", account="user-a@example.test", nickname="user-a",
        password_hash="not-a-real-password-hash", role=UserRole.STUDENT,
    )
    db_session.add(user)
    db_session.commit()
    gateway = InvalidModelGateway()
    registry = ToolRegistry()
    runtime = AgentRuntime(
        planner=PlannerAgent(gateway=gateway, tools=registry),
        executor=ExecutorAgent(gateway=gateway, tools=registry),
        verifier=VerifierAgent(gateway=gateway, tools=registry), agent_version="pev-test",
    )

    result = runtime.run(
        db_session, user_id=user.id,
        task=AgentTaskRequest(goal="找 AI Agent 岗位", allowed_skills=["job-discovery"]),
    )

    assert (result.status, result.error_code) == (RunStatus.waiting_user, None)
    assert "无法生成执行计划" in (result.summary or "")
    events = run_repository.list_events(db_session, result.run_id)
    assert events[-1].event_type == "planner_needs_user"


def test_runtime_degrades_executor_invalid_model_to_waiting_for_user(db_session) -> None:
    """Invalid executor completions pause the step for a human retry instead of failing."""
    user = User(
        id="user-a", account="user-a@example.test", nickname="user-a",
        password_hash="not-a-real-password-hash", role=UserRole.STUDENT,
    )
    db_session.add(user)
    db_session.commit()
    run, task, plan, plan_step, step = _create_running_step(
        db_session, user, requires_verification=False
    )

    result = _runtime_for_gateway(InvalidModelGateway())._run_step(
        db=db_session, run_id=run.id, task=task, plan=plan, plan_step=plan_step,
        persisted_step=step, context=ToolContext(user_id=user.id, run_id=run.id),
        trace=lambda *_args: None, tool_budget=ToolCallBudget(4), turn_budget=AgentTurnBudget(4),
    )

    assert (result.status, result.error_code) == (RunStatus.waiting_user, None)
    assert step.error_code == "need_user"
    events = run_repository.list_events(db_session, run.id)
    assert events[-1].event_type == "run_needs_user"


def test_runtime_degrades_verifier_invalid_model_to_waiting_for_user(db_session) -> None:
    """A verifier that cannot parse its own output routes the step to a human check."""
    user = User(
        id="user-a", account="user-a@example.test", nickname="user-a",
        password_hash="not-a-real-password-hash", role=UserRole.STUDENT,
    )
    db_session.add(user)
    db_session.commit()
    run, task, plan, plan_step, step = _create_running_step(
        db_session, user, requires_verification=True
    )
    gateway = RoleScriptedGateway({
        AgentRole.planner: [],
        AgentRole.executor: [{"action": "complete", "summary": "提取完成"}],
        AgentRole.verifier: [],
    })
    runtime = _runtime_for_gateway(gateway)
    runtime._verifier = VerifierAgent(gateway=InvalidModelGateway(), tools=ToolRegistry())

    result = runtime._run_step(
        db=db_session, run_id=run.id, task=task, plan=plan, plan_step=plan_step,
        persisted_step=step, context=ToolContext(user_id=user.id, run_id=run.id),
        trace=lambda *_args: None, tool_budget=ToolCallBudget(4), turn_budget=AgentTurnBudget(4),
    )

    assert (result.status, result.error_code) == (RunStatus.waiting_user, None)
    assert "人工确认" in (result.summary or "")
    events = run_repository.list_events(db_session, run.id)
    assert events[-1].event_type == "run_needs_user"


def test_runtime_persists_structured_job_tool_output_as_a_separate_artifact(db_session) -> None:
    """A parsed JD remains available after its Executor step instead of only in model context."""
    user = User(
        id="user-a", account="user-a@example.test", nickname="user-a",
        password_hash="not-a-real-password-hash", role=UserRole.STUDENT,
    )
    db_session.add(user)
    db_session.commit()
    gateway = RoleScriptedGateway({
        AgentRole.planner: [{
            "action": "plan", "complexity": "L2", "success_criteria": ["结构化 JD"],
            "steps": [{"step_id": "extract", "objective": "提取 JD", "allowed_skills": ["job-discovery"]}],
        }],
        AgentRole.executor: [
            {"action": "call_tool", "tool_name": "extract-job", "tool_input": {}},
            {"action": "complete", "summary": "已提取完整 JD"},
        ],
        AgentRole.verifier: [],
    })
    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="extract-job", skill_name="job-discovery", input_model=EmptyInput,
        output_model=StructuredJobOutput, allowed_roles=frozenset({AgentRole.executor}),
        handler=lambda _context, _payload: {
            "source_url": "https://jobs.example/agent", "content_hash": "d" * 64,
            "candidates": [{"title": "AI Agent 开发工程师", "requirements": "Python"}],
        },
    ))
    runtime = AgentRuntime(
        planner=PlannerAgent(gateway=gateway, tools=registry),
        executor=ExecutorAgent(gateway=gateway, tools=registry),
        verifier=VerifierAgent(gateway=gateway, tools=registry), agent_version="pev-test",
    )

    result = runtime.run(
        db_session, user_id=user.id,
        task=AgentTaskRequest(goal="提取 JD", allowed_skills=["job-discovery"]),
    )

    artifacts = list(
        db_session.scalars(
            select(AgentArtifact).where(AgentArtifact.run_id == result.run_id)
        )
    )
    assert [(artifact.artifact_type, artifact.content_json) for artifact in artifacts] == [
        ("structured_job_details", {"candidates": [{"title": "AI Agent 开发工程师", "requirements": "Python"}]}),
    ]


def test_runtime_persists_every_valid_detail_from_one_batch_extraction(db_session) -> None:
    user = User(
        id="user-a", account="user-a@example.test", nickname="user-a",
        password_hash="not-a-real-password-hash", role=UserRole.STUDENT,
    )
    db_session.add(user)
    db_session.commit()
    run, _task, _plan, _plan_step, step = _create_running_step(
        db_session, user, requires_verification=False
    )
    execution = ExecutorResult(
        status="succeeded", summary="已结构化两份 JD",
        observations=[ToolObservation(
            tool_name="extract-observed-job-details-batch", status="succeeded",
            output={"details": [
                {"source_url": "https://jobs.example/a", "content_hash": "a" * 64,
                 "candidates": [{"title": "岗位 A"}]},
                {"source_url": "https://jobs.example/b", "content_hash": "b" * 64,
                 "candidates": [{"title": "岗位 B"}]},
                "malformed-detail",
            ]},
        )],
    )

    refs = AgentRuntime._persist_observed_evidence(db_session, run.id, step, execution)

    assert len(refs) == 2
    assert [artifact.content_json for artifact in db_session.scalars(
        select(AgentArtifact).where(AgentArtifact.run_id == run.id)
    )] == [
        {"candidates": [{"title": "岗位 A"}]},
        {"candidates": [{"title": "岗位 B"}]},
    ]


def test_runtime_persists_public_search_results_as_discovery_evidence(db_session) -> None:
    """A URL-discovery decision remains traceable after its Executor context is released."""
    user = User(
        id="user-a", account="user-a@example.test", nickname="user-a",
        password_hash="not-a-real-password-hash", role=UserRole.STUDENT,
    )
    db_session.add(user)
    db_session.commit()
    gateway = RoleScriptedGateway({
        AgentRole.planner: [{
            "action": "plan", "complexity": "L2", "success_criteria": ["找到公开来源"],
            "steps": [{"step_id": "search", "objective": "搜索岗位页面", "allowed_skills": ["job-discovery"]}],
        }],
        AgentRole.executor: [
            {"action": "call_tool", "tool_name": "search-jobs", "tool_input": {}},
            {"action": "complete", "summary": "已找到公开岗位页面"},
        ],
        AgentRole.verifier: [],
    })
    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="search-jobs", skill_name="job-discovery", input_model=EmptyInput,
        output_model=SearchResultsOutput, allowed_roles=frozenset({AgentRole.executor}),
        handler=lambda _context, _payload: {
            "query": "AI Agent 开发 官方招聘", "source_url": "https://www.bing.com/search?q=agent",
            "content_hash": "e" * 64,
            "results": [{"title": "Agent 工程师", "url": "https://jobs.example/agent", "snippet": "公开 JD"}],
        },
    ))
    runtime = AgentRuntime(
        planner=PlannerAgent(gateway=gateway, tools=registry),
        executor=ExecutorAgent(gateway=gateway, tools=registry),
        verifier=VerifierAgent(gateway=gateway, tools=registry), agent_version="pev-test",
    )

    result = runtime.run(
        db_session, user_id=user.id,
        task=AgentTaskRequest(goal="找 AI Agent 岗位", allowed_skills=["job-discovery"]),
    )

    artifacts = list(db_session.scalars(
        select(AgentArtifact).where(AgentArtifact.run_id == result.run_id)
    ))
    assert [(artifact.artifact_type, artifact.content_json) for artifact in artifacts] == [
        ("job_search_results", {
            "query": "AI Agent 开发 官方招聘",
            "results": [{"title": "Agent 工程师", "url": "https://jobs.example/agent", "snippet": "公开 JD"}],
        }),
    ]


def test_runtime_finishes_job_discovery_plan_after_complete_official_zero_match(
    db_session,
) -> None:
    """A complete source-scoped zero match is the answer to an existence
    question; downstream fetch/extract steps must not re-search an empty set."""
    user = User(
        id="juejin-user",
        account="juejin-user@example.test",
        nickname="juejin-user",
        password_hash="not-a-real-password-hash",
        role=UserRole.STUDENT,
    )
    db_session.add(user)
    db_session.commit()
    gateway = RoleScriptedGateway(
        {
            AgentRole.planner: [
                {
                    "action": "plan",
                    "complexity": "L2",
                    "success_criteria": ["回答最近三天是否存在匹配岗位"],
                    "steps": [
                        {
                            "step_id": "discover",
                            "objective": "检索掘金最近三天招聘帖",
                            "allowed_skills": ["job-discovery"],
                            "outputs": [
                                {
                                    "name": "job_search_results",
                                    "artifact_type": "job_search_results",
                                }
                            ],
                        },
                        {
                            "step_id": "fetch",
                            "objective": "抓取候选页面",
                            "allowed_skills": ["job-discovery"],
                            "depends_on": ["discover"],
                            "inputs": [
                                {
                                    "kind": "artifact",
                                    "name": "job_search_results",
                                    "from_step": "discover",
                                    "artifact_type": "job_search_results",
                                }
                            ],
                            "outputs": [
                                {
                                    "name": "public_job_page",
                                    "artifact_type": "public_job_page",
                                }
                            ],
                        },
                    ],
                }
            ],
            AgentRole.executor: [
                {
                    "action": "call_tool",
                    "tool_name": "search-public-job-pages",
                    "tool_input": {"query": "site:juejin.cn AIGC 产品经理 招聘"},
                },
                {
                    "action": "need_user",
                    "user_question": "没有候选页面，请提供其他来源。",
                },
            ],
            AgentRole.verifier: [],
        }
    )
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="search-public-job-pages",
            skill_name="job-discovery",
            input_model=SearchPublicJobPagesInput,
            output_model=OfficialNegativeSearchOutput,
            allowed_roles=frozenset({AgentRole.executor}),
            handler=lambda _context, payload: {
                "query": payload.query,
                "source_url": "https://api.juejin.cn/search_api/v1/search",
                "content_hash": "a" * 64,
                "results": [],
                "terminal_reason": "search_empty",
                "provider": "juejin_official_search",
                "source_scope": "juejin.cn",
                "time_window_days": 3,
                "coverage_complete": True,
                "scanned_result_count": 7,
                "matched_result_count": 0,
                "scan_queries": ["招聘", "内推", "校招"],
                "scan_evidence": [
                    {
                        "title": "招聘系统架构",
                        "url": "https://juejin.cn/post/7670000000000000001",
                        "snippet": "技术文章，不是岗位。",
                        "published_at": "2026-08-14T09:00:00+00:00",
                    }
                ],
            },
        )
    )
    skills = build_career_skill_registry(registry)
    runtime = AgentRuntime(
        planner=PlannerAgent(gateway=gateway, tools=registry, skills=skills),
        executor=ExecutorAgent(gateway=gateway, tools=registry, skills=skills),
        verifier=VerifierAgent(gateway=gateway, tools=registry, skills=skills),
        agent_version="pev-test",
        skills=skills,
    )

    result = runtime.run(
        db_session,
        user_id=user.id,
        task=AgentTaskRequest(
            goal=(
                "稀土掘金社区最近3天的招聘帖里，有没有适合我的 "
                "AIGC 产品经理（应届生）岗位？"
            ),
            allowed_skills=["job-discovery"],
        ),
    )

    steps = list(
        db_session.scalars(
            select(AgentStep)
            .where(AgentStep.run_id == result.run_id)
            .order_by(AgentStep.sequence)
        )
    )
    assert result.status is RunStatus.succeeded
    assert "未找到" in (result.summary or "")
    assert [(step.sequence, step.status) for step in steps] == [
        (1, StepStatus.succeeded)
    ]
    artifacts = list(
        db_session.scalars(
            select(AgentArtifact).where(AgentArtifact.run_id == result.run_id)
        )
    )
    assert [artifact.artifact_type for artifact in artifacts] == [
        "job_search_results"
    ]
    assert "terminal_negative_discovery_succeeded" in [
        event.event_type
        for event in run_repository.list_events(db_session, result.run_id)
    ]


def test_runtime_persists_sheet_records_as_discovery_evidence(db_session) -> None:
    """C005: sheet-backed records satisfy the evidence contract and persist too."""
    user = User(
        id="user-b", account="user-b@example.test", nickname="user-b",
        password_hash="not-a-real-password-hash", role=UserRole.STUDENT,
    )
    db_session.add(user)
    db_session.commit()
    gateway = RoleScriptedGateway({
        AgentRole.planner: [{
            "action": "plan", "complexity": "L2", "success_criteria": ["找到公开来源"],
            "steps": [{"step_id": "search", "objective": "查询内推表", "allowed_skills": ["job-discovery"]}],
        }],
        AgentRole.executor: [
            {"action": "call_tool", "tool_name": "query-sheet", "tool_input": {}},
            {"action": "complete", "summary": "已从内推表找到记录"},
        ],
        AgentRole.verifier: [],
    })
    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="query-sheet", skill_name="job-discovery", input_model=EmptyInput,
        output_model=SheetRecordsOutput, allowed_roles=frozenset({AgentRole.executor}),
        handler=lambda _context, _payload: {
            "records": [
                {"company_name": "字节跳动", "apply_url": "https://job.example/1", "sheet_name": "s"},
                {"company_name": "腾讯", "apply_url": "https://job.example/2", "sheet_name": "s"},
            ],
            "source_url": "https://job.example/1",
            "content_hash": "d" * 64,
            "query": {"company_keywords": ["字节"]},
        },
    ))
    runtime = AgentRuntime(
        planner=PlannerAgent(gateway=gateway, tools=registry),
        executor=ExecutorAgent(gateway=gateway, tools=registry),
        verifier=VerifierAgent(gateway=gateway, tools=registry), agent_version="pev-test",
    )

    result = runtime.run(
        db_session, user_id=user.id,
        task=AgentTaskRequest(goal="找 AI Agent 岗位", allowed_skills=["job-discovery"]),
    )

    artifacts = list(db_session.scalars(
        select(AgentArtifact).where(AgentArtifact.run_id == result.run_id)
    ))
    assert [(artifact.artifact_type, artifact.content_json) for artifact in artifacts] == [
        ("job_search_results", {
            "query": {"company_keywords": ["字节"]},
            "results": [
                {"company_name": "字节跳动", "apply_url": "https://job.example/1", "sheet_name": "s"},
                {"company_name": "腾讯", "apply_url": "https://job.example/2", "sheet_name": "s"},
            ],
        }),
    ]
    events = run_repository.list_events(db_session, result.run_id)
    assert any(
        event.event_type == "executor_search_artifact"
        and event.payload_json["source_url"] == "https://job.example/1"
        and event.payload_json["content_hash"] == "d" * 64
        for event in events
    )


def test_runtime_records_each_failed_executor_tool_observation_with_its_stable_code(db_session) -> None:
    """A real Agent retry must be explainable from persisted tool observations."""
    user = User(
        id="user-a", account="user-a@example.test", nickname="user-a",
        password_hash="not-a-real-password-hash", role=UserRole.STUDENT,
    )
    db_session.add(user)
    db_session.commit()
    gateway = RoleScriptedGateway({
        AgentRole.planner: [{
            "action": "plan", "complexity": "L2", "success_criteria": ["尝试工具"],
            "steps": [{"step_id": "discover", "objective": "尝试", "allowed_skills": ["job-discovery"]}],
        }],
        AgentRole.executor: [
            {"action": "call_tool", "tool_name": "missing-tool", "tool_input": {}},
            {"action": "complete", "summary": "已安全降级"},
        ],
        AgentRole.verifier: [],
    })
    registry = ToolRegistry()
    runtime = AgentRuntime(
        planner=PlannerAgent(gateway=gateway, tools=registry),
        executor=ExecutorAgent(gateway=gateway, tools=registry),
        verifier=VerifierAgent(gateway=gateway, tools=registry), agent_version="pev-test",
    )

    result = runtime.run(
        db_session, user_id=user.id,
        task=AgentTaskRequest(goal="找岗位", allowed_skills=["job-discovery"]),
    )

    events = run_repository.list_events(db_session, result.run_id)
    assert (events[2].event_type, events[2].payload_json) == (
        "executor_tool_failed",
        {"sequence": 1, "tool": "missing-tool", "error_code": "unknown_tool"},
    )


def test_runtime_audits_failed_executor_observations_before_terminal_failure(db_session) -> None:
    """A turn-budget or budget failure must not erase the tool error that caused it."""
    user = User(
        id="user-a", account="user-a@example.test", nickname="user-a",
        password_hash="not-a-real-password-hash", role=UserRole.STUDENT,
    )
    db_session.add(user)
    db_session.commit()
    gateway = RoleScriptedGateway({
        AgentRole.planner: [{
            "action": "plan", "complexity": "L2", "success_criteria": ["调用工具"],
            "steps": [{"step_id": "discover", "objective": "发现岗位", "allowed_skills": ["job-discovery"]}],
        }],
        AgentRole.executor: [{
            "action": "call_tool", "tool_name": "missing-tool", "tool_input": {},
        }],
        AgentRole.verifier: [],
    })
    runtime = AgentRuntime(
        planner=PlannerAgent(gateway=gateway, tools=ToolRegistry()),
        executor=ExecutorAgent(gateway=gateway, tools=ToolRegistry()),
        verifier=VerifierAgent(gateway=gateway, tools=ToolRegistry()), agent_version="pev-test",
    )

    result = runtime.run(
        db_session, user_id=user.id,
        task=AgentTaskRequest(
            goal="找岗位", allowed_skills=["job-discovery"],
                budget={"max_agent_turns": 2, "max_tool_calls": 2, "max_replans": 0},
        ),
    )

    events = run_repository.list_events(db_session, result.run_id)
    failures = [event for event in events if event.event_type == "executor_tool_failed"]
    assert (result.status, result.error_code) == (
        RunStatus.failed,
        "agent_turn_budget_exhausted",
    )
    assert [event.payload_json for event in failures] == [{
        "sequence": 1, "tool": "missing-tool", "error_code": "unknown_tool",
    }]


def test_runtime_retains_successful_evidence_when_executor_later_hits_turn_limit(db_session) -> None:
    """A failed run still preserves the public JD evidence it safely captured before stopping."""
    user = User(
        id="user-a", account="user-a@example.test", nickname="user-a",
        password_hash="not-a-real-password-hash", role=UserRole.STUDENT,
    )
    db_session.add(user)
    db_session.commit()
    gateway = RoleScriptedGateway({
        AgentRole.planner: [{
            "action": "plan", "complexity": "L2", "success_criteria": ["抓取公开 JD"],
            "steps": [{"step_id": "discover", "objective": "抓取岗位", "allowed_skills": ["job-discovery"]}],
        }],
        AgentRole.executor: [{
            "action": "call_tool", "tool_name": "fetch-job", "tool_input": {},
        }],
        AgentRole.verifier: [],
    })
    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="fetch-job", skill_name="job-discovery", input_model=EmptyInput,
        output_model=FetchedJobOutput, allowed_roles=frozenset({AgentRole.executor}),
        handler=lambda _context, _payload: {
            "title": "AI Agent 开发工程师", "source_url": "https://jobs.example/agent",
            "content_hash": "f" * 64, "visible_text": "岗位职责：开发 Agent。",
        },
    ))
    runtime = AgentRuntime(
        planner=PlannerAgent(gateway=gateway, tools=registry),
        executor=ExecutorAgent(gateway=gateway, tools=registry),
        verifier=VerifierAgent(gateway=gateway, tools=registry), agent_version="pev-test",
    )

    result = runtime.run(
        db_session, user_id=user.id,
        task=AgentTaskRequest(
            goal="找岗位", allowed_skills=["job-discovery"],
                budget={"max_agent_turns": 2, "max_tool_calls": 2, "max_replans": 0},
        ),
    )

    artifacts = list(db_session.scalars(
        select(AgentArtifact).where(AgentArtifact.run_id == result.run_id)
    ))
    assert (result.status, result.error_code) == (
        RunStatus.failed,
        "agent_turn_budget_exhausted",
    )
    assert [(artifact.artifact_type, artifact.source_url) for artifact in artifacts] == [
        ("public_job_page", "https://jobs.example/agent"),
    ]


def test_runtime_degrades_planner_wall_clock_to_waiting_for_user(db_session) -> None:
    """Wall-clock exhaustion at the planner degrades to recoverable waiting_user, not failed."""
    user = User(
        id="user-a", account="user-a@example.test", nickname="user-a",
        password_hash="not-a-real-password-hash", role=UserRole.STUDENT,
    )
    db_session.add(user)
    db_session.commit()
    run = run_repository.create_run(
        db_session, user_id=user.id, goal="找岗位", allowed_skills=["job-discovery"],
        context_summary={}, budget_json={}, agent_version="pev-test",
    )
    runtime = object.__new__(AgentRuntime)
    result = runtime._finish_planner_non_plan(
        db_session, run.id, run,
        PlannerResult(status="failed", error_code="wall_clock_budget_exhausted"),
    )
    db_session.commit()

    assert (result.status, result.error_code) == (
        RunStatus.waiting_user,
        "wall_clock_budget_exhausted",
    )
    assert "运行时间预算耗尽" in (result.summary or "")
    events = run_repository.list_events(db_session, run.id)
    assert events[-1].event_type == "planner_budget_exhausted"
    assert events[-1].payload_json["question"] == result.summary
    # The error_code is persisted on the run row for observability.
    refreshed = db_session.get(AgentRun, run.id)
    assert refreshed.error_code == "wall_clock_budget_exhausted"
    assert refreshed.status is RunStatus.waiting_user


def test_runtime_degrades_executor_wall_clock_to_waiting_for_user(db_session) -> None:
    """Executor wall-clock exhaustion mid-step pauses the run recoverably instead of failing."""
    user = User(
        id="user-a", account="user-a@example.test", nickname="user-a",
        password_hash="not-a-real-password-hash", role=UserRole.STUDENT,
    )
    db_session.add(user)
    db_session.commit()
    run, task, plan, plan_step, step = _create_running_step(
        db_session, user, requires_verification=False
    )
    gateway = RoleScriptedGateway({
        AgentRole.planner: [],
        AgentRole.executor: [{"action": "complete", "summary": "提取完成"}],
        AgentRole.verifier: [],
    })
    result = _runtime_for_gateway(gateway)._run_step(
        db=db_session, run_id=run.id, task=task, plan=plan, plan_step=plan_step,
        persisted_step=step, context=ToolContext(user_id=user.id, run_id=run.id),
        trace=lambda *_args: None, tool_budget=ToolCallBudget(4), turn_budget=AgentTurnBudget(4),
        deadline=0.0,
    )
    db_session.commit()

    assert (result.status, result.error_code) == (
        RunStatus.waiting_user,
        "wall_clock_budget_exhausted",
    )
    assert step.status is StepStatus.failed
    assert step.error_code == "wall_clock_budget_exhausted"
    refreshed = db_session.get(AgentRun, run.id)
    assert refreshed.status is RunStatus.waiting_user
    assert refreshed.error_code == "wall_clock_budget_exhausted"
    events = run_repository.list_events(db_session, run.id)
    assert events[-1].event_type == "run_needs_user"


def test_runtime_preserves_completed_routing_artifact_on_executor_wall_clock(
    db_session,
) -> None:
    user = User(
        id="user-routing-wall",
        account="routing-wall@example.test",
        nickname="routing",
        password_hash="not-a-real-password-hash",
        role=UserRole.STUDENT,
    )
    db_session.add(user)
    db_session.commit()
    run, task, _plan, _plan_step, step = _create_running_step(
        db_session, user, requires_verification=False
    )
    route_url = "https://jobs.example/bigo"
    plan = ExecutionPlan(
        task=task,
        created_by=AgentRole.planner,
        complexity=ComplexityLevel.L3,
        success_criteria=["公司清单和岗位详情"],
        steps=[
            PlanStep(
                step_id="companies",
                objective="查询最近1天更新的公司记录",
                allowed_skills=["job-discovery"],
                outputs=[
                    {
                        "name": "recent_company_records",
                        "artifact_type": "job_search_results",
                    }
                ],
            ),
            PlanStep(
                step_id="jobs",
                objective="逐公司核实岗位",
                allowed_skills=["job-discovery"],
                depends_on=["companies"],
            ),
        ],
    )
    executor = MagicMock()
    executor.run.return_value = ExecutorResult(
        status="failed",
        error_code="wall_clock_budget_exhausted",
        observations=[
            ToolObservation(
                tool_name="query-career-sheet-records",
                status="succeeded",
                output={
                    "records": [
                        {
                            "company_name": "BIGO",
                            "apply_url": route_url,
                        }
                    ],
                    "source_url": "https://docs.example/recent-companies",
                    "content_hash": "r" * 64,
                },
            )
        ],
    )
    runtime = AgentRuntime(
        planner=MagicMock(),
        executor=executor,
        verifier=MagicMock(),
        agent_version="pev-test",
    )
    exhausted_tool_budget = ToolCallBudget(1)
    assert exhausted_tool_budget.try_consume()

    result = runtime._run_step(
        db=db_session,
        run_id=run.id,
        task=task,
        plan=plan,
        plan_step=plan.steps[0],
        persisted_step=step,
        context=ToolContext(user_id=user.id, run_id=run.id),
        trace=lambda *_args: None,
        tool_budget=exhausted_tool_budget,
        turn_budget=AgentTurnBudget(4),
    )

    assert result.status is RunStatus.running
    assert step.status is StepStatus.succeeded
    events = run_repository.list_events(db_session, run.id)
    assert events[-1].payload_json["reason"] == (
        "wall_clock_intermediate_routing_contract_met"
    )


def test_runtime_degrades_verifier_wall_clock_to_waiting_for_user(db_session) -> None:
    """R004: executor completed but verifier hits deadline -> recoverable waiting_user."""
    user = User(
        id="user-a", account="user-a@example.test", nickname="user-a",
        password_hash="not-a-real-password-hash", role=UserRole.STUDENT,
    )
    db_session.add(user)
    db_session.commit()
    run, task, plan, plan_step, step = _create_running_step(
        db_session, user, requires_verification=True
    )
    gateway = RoleScriptedGateway({
        AgentRole.planner: [],
        AgentRole.executor: [{"action": "complete", "summary": "提取完成"}],
        AgentRole.verifier: [],
    })
    runtime = _runtime_for_gateway(gateway)
    runtime._verifier = MagicMock()
    runtime._verifier.run.return_value = VerifierResult(
        decision=VerificationDecision.FAIL,
        feedback="Wall-clock budget exhausted before verification.",
        error_code="wall_clock_budget_exhausted",
    )

    result = runtime._run_step(
        db=db_session, run_id=run.id, task=task, plan=plan, plan_step=plan_step,
        persisted_step=step, context=ToolContext(user_id=user.id, run_id=run.id),
        trace=lambda *_args: None, tool_budget=ToolCallBudget(4), turn_budget=AgentTurnBudget(4),
    )
    db_session.commit()

    assert (result.status, result.error_code) == (
        RunStatus.waiting_user,
        "wall_clock_budget_exhausted",
    )
    assert step.status is StepStatus.failed
    assert step.error_code == "wall_clock_budget_exhausted"
    refreshed = db_session.get(AgentRun, run.id)
    assert refreshed.status is RunStatus.waiting_user
    assert refreshed.error_code == "wall_clock_budget_exhausted"
    events = run_repository.list_events(db_session, run.id)
    assert events[-1].event_type == "run_needs_user"


def test_runtime_resume_after_verifier_wall_clock_recomputes_deadline_and_proceeds(db_session) -> None:
    """Resume after verifier wall-clock gives a fresh deadline so the verifier can run."""
    user = User(
        id="user-a", account="user-a@example.test", nickname="user-a",
        password_hash="not-a-real-password-hash", role=UserRole.STUDENT,
    )
    db_session.add(user)
    db_session.commit()
    plan_decision = {
        "action": "plan", "complexity": "L3", "success_criteria": ["有证据"],
        "steps": [{
            "step_id": "discover", "objective": "提取公开 JD",
            "allowed_skills": ["job-discovery"], "requires_verification": True,
        }],
    }
    gateway = RoleScriptedGateway({
        AgentRole.planner: [plan_decision, plan_decision],
        AgentRole.executor: [
            {"action": "complete", "summary": "已提取"},
            {"action": "complete", "summary": "已提取"},
        ],
        AgentRole.verifier: [],
    })
    runtime = AgentRuntime(
        planner=PlannerAgent(gateway=gateway, tools=ToolRegistry()),
        executor=ExecutorAgent(gateway=gateway, tools=ToolRegistry()),
        verifier=MagicMock(),
        agent_version="pev-test",
    )
    runtime._verifier.run.return_value = VerifierResult(
        decision=VerificationDecision.FAIL,
        feedback="Wall-clock budget exhausted before verification.",
        error_code="wall_clock_budget_exhausted",
    )
    task = AgentTaskRequest(
        goal="找岗位", allowed_skills=["job-discovery"],
        budget=AgentBudget(max_agent_turns=6, max_tool_calls=6, max_replans=0),
    )

    waiting = runtime.run(db_session, user_id=user.id, task=task)
    db_session.commit()

    assert (waiting.status, waiting.error_code) == (
        RunStatus.waiting_user,
        "wall_clock_budget_exhausted",
    )

    # Resume: the verifier now returns PASS with the freshly recomputed deadline.
    runtime._verifier.run.return_value = VerifierResult(
        decision=VerificationDecision.PASS,
    )
    resumed = runtime.resume(
        db_session, user_id=user.id, run_id=waiting.run_id, task=task,
    )
    db_session.commit()

    assert resumed.status is RunStatus.succeeded
    # The fresh plan + step ran to completion after resume.
    assert run_repository.count_plans(db_session, waiting.run_id) == 2
    events = run_repository.list_events(db_session, waiting.run_id)
    assert "run_resumed" in [event.event_type for event in events]
    assert events[-1].event_type == "run_succeeded"


def test_runtime_resume_after_wall_clock_does_not_reset_turn_or_tool_budget(db_session) -> None:
    """Only the wall-clock window refreshes on resume; turn/tool budgets keep their consumed counts."""
    user = User(
        id="user-a", account="user-a@example.test", nickname="user-a",
        password_hash="not-a-real-password-hash", role=UserRole.STUDENT,
    )
    db_session.add(user)
    db_session.commit()
    plan_decision = {
        "action": "plan", "complexity": "L3", "success_criteria": ["有证据"],
        "steps": [{
            "step_id": "discover", "objective": "提取公开 JD",
            "allowed_skills": ["job-discovery"], "requires_verification": True,
        }],
    }
    gateway = RoleScriptedGateway({
        AgentRole.planner: [plan_decision, plan_decision],
        AgentRole.executor: [
            {"action": "call_tool", "tool_name": "fetch-job", "tool_input": {}},
            {"action": "complete", "summary": "已提取"},
            {"action": "call_tool", "tool_name": "fetch-job", "tool_input": {}},
            {"action": "complete", "summary": "已提取"},
        ],
        AgentRole.verifier: [],
    })
    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="fetch-job", skill_name="job-discovery", input_model=EmptyInput,
        output_model=JobOutput, allowed_roles=frozenset({AgentRole.executor}),
        handler=lambda _context, _payload: {"title": "AI Agent 开发工程师"},
    ))
    runtime = AgentRuntime(
        planner=PlannerAgent(gateway=gateway, tools=registry),
        executor=ExecutorAgent(gateway=gateway, tools=registry),
        verifier=MagicMock(),
        agent_version="pev-test",
    )
    runtime._verifier.run.return_value = VerifierResult(
        decision=VerificationDecision.FAIL,
        feedback="Wall-clock budget exhausted before verification.",
        error_code="wall_clock_budget_exhausted",
    )
    task = AgentTaskRequest(
        goal="找岗位", allowed_skills=["job-discovery"],
        budget=AgentBudget(max_agent_turns=6, max_tool_calls=6, max_replans=0),
    )

    waiting = runtime.run(db_session, user_id=user.id, task=task)
    db_session.commit()

    assert waiting.status is RunStatus.waiting_user
    turns_before_resume = run_repository.count_turns(db_session, waiting.run_id)
    tools_before_resume = run_repository.count_tool_decisions(db_session, waiting.run_id)
    assert turns_before_resume > 0
    assert tools_before_resume > 0

    # Resume: the verifier returns PASS so the step completes.
    runtime._verifier.run.return_value = VerifierResult(
        decision=VerificationDecision.PASS,
    )
    resumed = runtime.resume(
        db_session, user_id=user.id, run_id=waiting.run_id, task=task,
    )
    db_session.commit()

    assert resumed.status is RunStatus.succeeded
    # Turns and tool calls are cumulative across resume (not reset to 0):
    # the first run consumed turns/tools, and the resume consumed more on top.
    assert run_repository.count_turns(db_session, waiting.run_id) > turns_before_resume
    assert run_repository.count_tool_decisions(db_session, waiting.run_id) > tools_before_resume
    # The resume planner saw fewer remaining turns than a fresh run would
    # (max - consumed_before_resume - 1 for its own consume), proving the
    # turn budget was not reset. First-run planner had remaining = max - 1.
    first_run_remaining = gateway.states[AgentRole.planner][0]["remaining_agent_turns"]
    resume_remaining = gateway.states[AgentRole.planner][1]["remaining_agent_turns"]
    assert resume_remaining < first_run_remaining
    # The resume executor's first call_tool saw fewer remaining tool calls
    # than the first run's call_tool, proving the tool budget was not reset.
    first_run_tool_remaining = gateway.states[AgentRole.executor][0]["remaining_tool_calls"]
    resume_tool_remaining = gateway.states[AgentRole.executor][2]["remaining_tool_calls"]
    assert resume_tool_remaining < first_run_tool_remaining

