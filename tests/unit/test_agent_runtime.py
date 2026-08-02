"""End-to-end service behavior for the real three-Agent PEV orchestration loop."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel
from sqlalchemy import select

from backend.app.db.models import AgentArtifact, AgentStep, AgentTurn, User, UserRole
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
