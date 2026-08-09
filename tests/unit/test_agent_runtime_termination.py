"""Termination-and-retry-state rescues for the adaptive PEV runtime.

Round-1/B behaviors:

* B2 - a verifier ``invalid_model_response`` terminates the step as succeeded
  when the step's deliverable contract is already tool-backed and no blocked
  evidence exists; otherwise the step still goes to the human.
* B3 - an executor ``needs_user`` hand-off after the deliverable was persisted
  terminates as succeeded (post-deliverable stall); blocked evidence keeps
  the human hand-off.
* B4 - a verifier RETRY_EXECUTOR loop over blocked evidence that cannot
  satisfy the deliverable contract downgrades to ONE clean waiting_user
  hand-off instead of re-invoking the executor.
* B5 - the executor's succeeded-call dedup set and waste counters survive
  verifier RETRY re-invocations (cross-retry dedup, no re-spent waste budget).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from backend.app.db.models import User, UserRole
from backend.app.domain.agent_runtime import (
    AgentRole,
    ComplexityLevel,
    RunStatus,
    StepStatus,
)
from backend.app.repositories import agent_runtime as run_repository
from backend.app.services.agent_runtime.executor_agent import ExecutorAgent
from backend.app.services.agent_runtime.model_gateway import AgentModelGatewayError
from backend.app.services.agent_runtime.planner_agent import PlannerAgent
from backend.app.services.agent_runtime.runtime import AgentRuntime
from backend.app.services.agent_runtime.schemas import (
    AgentBudget,
    AgentTaskRequest,
    ExecutionPlan,
    PlanStep,
)
from backend.app.services.agent_runtime.tool_budget import ToolCallBudget
from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.agent_runtime.tool_registry import ToolDefinition, ToolRegistry
from backend.app.services.agent_runtime.turn_budget import AgentTurnBudget
from backend.app.services.agent_runtime.verifier_agent import VerifierAgent


class EmptyInput(BaseModel):
    pass


class FetchPagesInput(BaseModel):
    urls: list[str]


class FetchPagesOutput(BaseModel):
    pages: list[dict[str, object]]
    failures: list[dict[str, str]] = []


class WechatInput(BaseModel):
    url: str


class WechatOutput(BaseModel):
    """Succeeded direct-tool output that still carries a blocked marker."""

    url: str
    status: str | None = None
    reason: str | None = None


class MatchOutput(BaseModel):
    source_url: str
    matches: list[dict[str, object]]


def _page_evidence(source: str = "https://jobs.example/1") -> dict[str, str]:
    return {
        "source_url": source,
        "content_hash": "a" * 64,
        "visible_text": "负责 AI Agent 开发，要求 Python 与 LangChain。",
    }


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


class InvalidModelGateway:
    """Provider double whose completions never parse into the response model."""

    @property
    def last_usage(self) -> dict[str, Any] | None:
        return None

    def decide(self, **_kwargs):  # noqa: ANN003
        raise AgentModelGatewayError("invalid_model_response")


def _user(user_id: str) -> User:
    return User(
        id=user_id,
        account=f"{user_id}@example.test",
        nickname=user_id,
        password_hash="not-a-real-password-hash",
        role=UserRole.STUDENT,
    )


def _runtime_for_gateway(gateway: object, registry: ToolRegistry) -> AgentRuntime:
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
    allowed_skills: list[str],
    requires_verification: bool,
    budget: AgentBudget | None = None,
):
    task = AgentTaskRequest(
        goal="验证运行时终止路径",
        allowed_skills=allowed_skills,
        budget=budget
        or AgentBudget(
            max_agent_turns=8, max_tool_calls=8, max_replans=2
        ),
    )
    plan_step = PlanStep(
        step_id="step-1",
        objective="完成步骤产出",
        allowed_skills=allowed_skills,
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


def _run_step(runtime: AgentRuntime, db_session, run, user, task, plan, plan_step, step):
    return runtime._run_step(
        db=db_session,
        run_id=run.id,
        task=task,
        plan=plan,
        plan_step=plan_step,
        persisted_step=step,
        context=ToolContext(user_id=user.id, run_id=run.id),
        trace=lambda *_args: None,
        tool_budget=ToolCallBudget(8),
        turn_budget=AgentTurnBudget(8),
    )


def _register_discovery_tools(
    registry: ToolRegistry,
    *,
    pages_handler=None,
    wechat_handler=None,
) -> None:
    """Register the real deliverable tool names used by the evidence contract."""
    registry.register(
        ToolDefinition(
            name="fetch-public-job-pages",
            skill_name="job-discovery",
            input_model=FetchPagesInput,
            output_model=FetchPagesOutput,
            allowed_roles=frozenset({AgentRole.executor}),
            handler=pages_handler
            or (lambda _context, payload: {"pages": [_page_evidence(payload.urls[0])]}),
        )
    )
    registry.register(
        ToolDefinition(
            name="fetch-wechat-article",
            skill_name="job-discovery",
            input_model=WechatInput,
            output_model=WechatOutput,
            allowed_roles=frozenset({AgentRole.executor}),
            handler=wechat_handler
            or (lambda _context, _payload: {"url": "https://mp.weixin.qq.com/s/x"}),
        )
    )


def _blocked_wechat_handler(_context, _payload) -> dict[str, object]:
    """Mirror the OCR-gated WeChat tool: succeeded output, blocked marker."""
    return {
        "url": "https://mp.weixin.qq.com/s/x",
        "status": "needs_manual_review",
        "reason": "ocr_disabled",
    }


# ---------------------------------------------------------------------------
# B2 - verifier invalid_model_response rescue
# ---------------------------------------------------------------------------


def test_b2_rescues_verifier_invalid_model_when_contract_met(db_session) -> None:
    """A deliverable-backed step survives verifier transport degradation."""
    user = _user("user-b2")
    db_session.add(user)
    db_session.commit()
    run, task, plan, plan_step, step = _create_running_step(
        db_session, user, allowed_skills=["job-matching"], requires_verification=True
    )
    gateway = RoleScriptedGateway({
        AgentRole.planner: [],
        AgentRole.executor: [
            {"action": "call_tool", "tool_name": "match-observed-jobs", "tool_input": {}},
            {"action": "complete", "summary": "匹配完成"},
        ],
        AgentRole.verifier: [],
    })
    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="match-observed-jobs", skill_name="job-matching", input_model=EmptyInput,
        output_model=MatchOutput, allowed_roles=frozenset({AgentRole.executor}),
        handler=lambda _context, _payload: {
            "source_url": "https://jobs.example/a",
            "matches": [{"artifact_id": "observed:a", "source_url": "https://jobs.example/a", "score": 80}],
        },
    ))
    runtime = _runtime_for_gateway(gateway, registry)
    runtime._verifier = VerifierAgent(gateway=InvalidModelGateway(), tools=ToolRegistry())

    result = _run_step(runtime, db_session, run, user, task, plan, plan_step, step)

    assert result.status is RunStatus.running
    assert result.summary == "匹配完成"
    assert step.status is StepStatus.succeeded
    events = run_repository.list_events(db_session, run.id)
    assert [event.event_type for event in events] == [
        "executor_skill_artifact",
        "step_succeeded",
        "verifier_rescue_succeeded",
    ]
    assert events[-1].payload_json["reason"] == "invalid_model_response_contract_met"


def test_b2_keeps_human_handoff_when_blocked_evidence_exists(db_session) -> None:
    """Blocked evidence (OCR-off WeChat) never auto-passes, even with a deliverable."""
    user = _user("user-b2b")
    db_session.add(user)
    db_session.commit()
    run, task, plan, plan_step, step = _create_running_step(
        db_session, user, allowed_skills=["job-discovery"], requires_verification=True
    )
    gateway = RoleScriptedGateway({
        AgentRole.planner: [],
        AgentRole.executor: [
            {"action": "call_tool", "tool_name": "fetch-public-job-pages", "tool_input": {"urls": ["https://jobs.example/1"]}},
            {"action": "call_tool", "tool_name": "fetch-wechat-article", "tool_input": {"url": "https://mp.weixin.qq.com/s/x"}},
            {"action": "complete", "summary": "已收集部分页面"},
        ],
        AgentRole.verifier: [],
    })
    registry = ToolRegistry()
    _register_discovery_tools(registry, wechat_handler=_blocked_wechat_handler)
    runtime = _runtime_for_gateway(gateway, registry)
    runtime._verifier = VerifierAgent(gateway=InvalidModelGateway(), tools=ToolRegistry())

    result = _run_step(runtime, db_session, run, user, task, plan, plan_step, step)

    assert result.status is RunStatus.waiting_user
    assert step.error_code == "need_user"
    assert "人工确认" in (result.summary or "")
    assert "verifier_rescue_succeeded" not in [
        event.event_type for event in run_repository.list_events(db_session, run.id)
    ]


# ---------------------------------------------------------------------------
# B3 - executor needs_user rescue when the deliverable is already persisted
# ---------------------------------------------------------------------------


def test_b3_rescues_post_deliverable_needs_user_handoff(db_session) -> None:
    """The Q071/R028 pattern: deliverable persisted, then a stall hand-off."""
    user = _user("user-b3")
    db_session.add(user)
    db_session.commit()
    run, task, plan, plan_step, step = _create_running_step(
        db_session, user, allowed_skills=["job-discovery"], requires_verification=False
    )
    gateway = RoleScriptedGateway({
        AgentRole.planner: [],
        AgentRole.executor: [
            {"action": "call_tool", "tool_name": "fetch-public-job-pages", "tool_input": {"urls": ["https://jobs.example/1"]}},
            {"action": "need_user", "user_question": "请确认已收集的岗位产出。"},
        ],
        AgentRole.verifier: [],
    })
    registry = ToolRegistry()
    _register_discovery_tools(registry)

    result = _run_step(
        _runtime_for_gateway(gateway, registry), db_session, run, user, task, plan, plan_step, step
    )

    assert result.status is RunStatus.running
    assert step.status is StepStatus.succeeded
    events = run_repository.list_events(db_session, run.id)
    assert events[-1].event_type == "executor_rescue_succeeded"
    assert events[-1].payload_json["reason"] == "needs_user_deliverable_persisted"


def test_b3_keeps_human_handoff_without_deliverable_or_with_blocked_evidence(db_session) -> None:
    """needs_user stays a human hand-off when the contract is unmet or blocked."""
    user = _user("user-b3b")
    db_session.add(user)
    db_session.commit()
    run, task, plan, plan_step, step = _create_running_step(
        db_session, user, allowed_skills=["job-discovery"], requires_verification=False
    )
    gateway = RoleScriptedGateway({
        AgentRole.planner: [],
        AgentRole.executor: [
            {"action": "call_tool", "tool_name": "fetch-public-job-pages", "tool_input": {"urls": ["https://jobs.example/1"]}},
            {"action": "call_tool", "tool_name": "fetch-wechat-article", "tool_input": {"url": "https://mp.weixin.qq.com/s/x"}},
            {"action": "need_user", "user_question": "请确认已收集的岗位产出。"},
        ],
        AgentRole.verifier: [],
    })
    registry = ToolRegistry()
    _register_discovery_tools(registry, wechat_handler=_blocked_wechat_handler)

    result = _run_step(
        _runtime_for_gateway(gateway, registry), db_session, run, user, task, plan, plan_step, step
    )

    assert result.status is RunStatus.waiting_user
    assert step.error_code == "need_user"
    assert "executor_rescue_succeeded" not in [
        event.event_type for event in run_repository.list_events(db_session, run.id)
    ]


# ---------------------------------------------------------------------------
# B4 - blocked RETRY downgrade to one clean human hand-off
# ---------------------------------------------------------------------------


def test_b4_downgrades_blocked_retry_loop_to_waiting_user(db_session) -> None:
    """Blocked evidence + unmet contract: one hand-off, executor NOT re-invoked."""
    user = _user("user-b4")
    db_session.add(user)
    db_session.commit()
    run, task, plan, plan_step, step = _create_running_step(
        db_session, user, allowed_skills=["job-discovery"], requires_verification=True
    )
    gateway = RoleScriptedGateway({
        AgentRole.planner: [],
        AgentRole.executor: [
            {"action": "call_tool", "tool_name": "fetch-wechat-article", "tool_input": {"url": "https://mp.weixin.qq.com/s/x"}},
            {"action": "complete", "summary": "仅剩微信链接"},
        ],
        AgentRole.verifier: [
            {"action": "decide", "verification_decision": "RETRY_EXECUTOR", "feedback": "缺少可核验来源"},
        ],
    })
    registry = ToolRegistry()
    _register_discovery_tools(registry, wechat_handler=_blocked_wechat_handler)

    result = _run_step(
        _runtime_for_gateway(gateway, registry), db_session, run, user, task, plan, plan_step, step
    )

    assert result.status is RunStatus.waiting_user
    assert step.error_code == "need_user"
    # Executor was invoked exactly once: its script holds two decisions and a
    # re-invocation would have popped from an empty list and raised.
    assert len(gateway.states[AgentRole.executor]) == 2
    events = run_repository.list_events(db_session, run.id)
    assert events[-1].event_type == "run_needs_user"
    downgrade = [e for e in events if e.event_type == "verification_retry_downgraded"]
    assert len(downgrade) == 1
    assert downgrade[0].payload_json["reason"] == "blocked_evidence"
    assert "阻断" in (result.summary or "")


def test_b4_keeps_retry_loop_when_contract_met_despite_blocked_failure(db_session) -> None:
    """Evidence captured + one blocked failure: the retry stays actionable."""
    user = _user("user-b4b")
    db_session.add(user)
    db_session.commit()
    run, task, plan, plan_step, step = _create_running_step(
        db_session, user, allowed_skills=["job-discovery"], requires_verification=True
    )
    gateway = RoleScriptedGateway({
        AgentRole.planner: [],
        AgentRole.executor: [
            {"action": "call_tool", "tool_name": "fetch-public-job-pages", "tool_input": {"urls": ["https://jobs.example/1"]}},
            {"action": "complete", "summary": "信息不完整"},
            {"action": "complete", "summary": "信息已补齐"},
        ],
        AgentRole.verifier: [
            {"action": "decide", "verification_decision": "RETRY_EXECUTOR", "feedback": "补充职责和任职要求。"},
            {"action": "decide", "verification_decision": "PASS"},
        ],
    })
    registry = ToolRegistry()
    _register_discovery_tools(registry, wechat_handler=_blocked_wechat_handler)
    # The batch output carries evidence AND a nested blocked failure.
    registry._definitions["fetch-public-job-pages"] = ToolDefinition(
        name="fetch-public-job-pages", skill_name="job-discovery", input_model=FetchPagesInput,
        output_model=FetchPagesOutput, allowed_roles=frozenset({AgentRole.executor}),
        handler=lambda _context, payload: {
            "pages": [_page_evidence(payload.urls[0])],
            "failures": [{"source_url": "https://mp.weixin.qq.com/s/x", "error_code": "wechat_ocr_disabled"}],
        },
    )

    result = _run_step(
        _runtime_for_gateway(gateway, registry), db_session, run, user, task, plan, plan_step, step
    )

    assert result.status is RunStatus.running
    assert step.status is StepStatus.succeeded
    assert len(gateway.states[AgentRole.executor]) == 3
    assert "verification_retry_downgraded" not in [
        event.event_type for event in run_repository.list_events(db_session, run.id)
    ]


# ---------------------------------------------------------------------------
# B5 - cross-invocation state survives verifier RETRY
# ---------------------------------------------------------------------------


def test_b5_dedups_an_identical_call_across_retry_invocations(db_session) -> None:
    """A verifier RETRY cannot re-issue an identical succeeded call (handler runs once)."""
    user = _user("user-b5")
    db_session.add(user)
    db_session.commit()
    run, task, plan, plan_step, step = _create_running_step(
        db_session, user, allowed_skills=["job-discovery"], requires_verification=True
    )
    handler_calls: list[dict[str, object]] = []

    def counting_handler(_context, payload):
        handler_calls.append(payload.urls)
        return {"pages": [_page_evidence(payload.urls[0])]}

    gateway = RoleScriptedGateway({
        AgentRole.planner: [],
        AgentRole.executor: [
            {"action": "call_tool", "tool_name": "fetch-public-job-pages", "tool_input": {"urls": ["https://jobs.example/1"]}},
            {"action": "complete", "summary": "信息不完整"},
            {"action": "call_tool", "tool_name": "fetch-public-job-pages", "tool_input": {"urls": ["https://jobs.example/1"]}},
            {"action": "complete", "summary": "信息已补齐"},
        ],
        AgentRole.verifier: [
            {"action": "decide", "verification_decision": "RETRY_EXECUTOR", "feedback": "补充来源标注。"},
            {"action": "decide", "verification_decision": "PASS"},
        ],
    })
    registry = ToolRegistry()
    _register_discovery_tools(registry, pages_handler=counting_handler)

    result = _run_step(
        _runtime_for_gateway(gateway, registry), db_session, run, user, task, plan, plan_step, step
    )

    assert result.status is RunStatus.running
    assert step.status is StepStatus.succeeded
    # The identical re-issue across the RETRY boundary was deduped: the real
    # handler ran only in the first invocation.
    assert len(handler_calls) == 1
    # Invocation 2's first decision state advertises the prior succeeded call.
    assert gateway.states[AgentRole.executor][2]["already_succeeded_calls"][0]["tool"] == (
        "fetch-public-job-pages"
    )
    # Invocation 2's second decision sees the duplicate observation.
    assert gateway.states[AgentRole.executor][3]["observations"][-1]["error_code"] == (
        "duplicate_tool_call"
    )


def test_b5_carries_total_waste_counters_across_retry_invocations(db_session) -> None:
    """Wasted turns are NOT reset by a verifier RETRY (C005: no budget tripling)."""
    user = _user("user-b5b")
    db_session.add(user)
    db_session.commit()
    run, task, plan, plan_step, step = _create_running_step(
        db_session, user, allowed_skills=["job-discovery"], requires_verification=True
    )

    def failing_handler(_context, _payload) -> None:
        raise RuntimeError("provider down")

    gateway = RoleScriptedGateway({
        AgentRole.planner: [],
        AgentRole.executor: [
            {"action": "call_tool", "tool_name": "fetch-public-job-pages", "tool_input": {"urls": ["https://jobs.example/1"]}},
            {"action": "call_tool", "tool_name": "fetch-public-job-pages", "tool_input": {"urls": ["https://jobs.example/1"]}},
            {"action": "complete", "summary": "尚未完成"},
            {"action": "call_tool", "tool_name": "fetch-public-job-pages", "tool_input": {"urls": ["https://jobs.example/1"]}},
        ],
        AgentRole.verifier: [
            {"action": "decide", "verification_decision": "RETRY_EXECUTOR", "feedback": "证据不足。"},
        ],
    })
    registry = ToolRegistry()
    _register_discovery_tools(registry, pages_handler=failing_handler)

    result = _run_step(
        _runtime_for_gateway(gateway, registry), db_session, run, user, task, plan, plan_step, step
    )

    # Two wasted turns in invocation 1 + one in invocation 2 = 3 >= the cap:
    # the third failed call hands the step to the human immediately, without
    # any further re-invocation (script exhausted after 4 decisions).
    assert result.status is RunStatus.waiting_user
    assert step.error_code == "need_user"
    assert "累计多次无效" in (result.summary or "")
    assert len(gateway.states[AgentRole.executor]) == 4
