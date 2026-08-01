"""End-to-end service behavior for the real three-Agent PEV orchestration loop."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel
from sqlalchemy import select

from backend.app.db.models import AgentStep, AgentTurn, User, UserRole
from backend.app.domain.agent_runtime import AgentRole, RunStatus
from backend.app.repositories import agent_runtime as run_repository
from backend.app.services.agent_runtime.executor_agent import ExecutorAgent
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
