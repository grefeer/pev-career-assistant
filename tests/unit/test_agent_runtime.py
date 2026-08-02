"""End-to-end service behavior for the real three-Agent PEV orchestration loop."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel
import pytest
from sqlalchemy import select

from backend.app.db.models import AgentArtifact, AgentPlan, AgentRun, AgentStep, AgentTurn, User, UserRole
from backend.app.domain.agent_runtime import AgentRole, RunStatus
from backend.app.repositories import agent_runtime as run_repository
from backend.app.services.agent_runtime.executor_agent import ExecutorAgent
from backend.app.services.agent_runtime.model_gateway import AgentModelGatewayError
from backend.app.services.agent_runtime.planner_agent import PlannerAgent
from backend.app.services.agent_runtime.runtime import AgentRuntime
from backend.app.services.agent_runtime.schemas import AgentTaskRequest
from backend.app.services.agent_runtime.tool_registry import ToolDefinition, ToolRegistry
from backend.app.services.agent_runtime.verifier_agent import VerifierAgent


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

    def decide(self, **_kwargs):  # noqa: ANN003
        raise AgentModelGatewayError("model_request_failed")


class CrashAfterFirstExecutorDecisionGateway:
    """Simulate process loss after a persisted Executor decision checkpoint."""

    def __init__(self) -> None:
        self._executor_decisions = 0

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
    assert events[-1].payload_json == {"error_code": "tool_budget_exhausted"}


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
    assert gateway.states[AgentRole.executor][1]["context"]["verifier_feedback"] == [
        "补充职责和任职要求。"
    ]


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
        "visible_text": "负责 Agent 平台、RAG 与工具调用。",
    }]


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
    assert events[-1].payload_json == {"error_code": "model_request_failed"}


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
