"""Candidate A: REPLAN contract gate for the adaptive PEV runtime.

Round-3 behaviors:

* NEED_USER contract gate - a verifier NEED_USER over a satisfied
  deterministic step contract (tool-backed deliverable, no blocked evidence,
  replan budget remaining, once-per-run marker absent) converts to a bounded
  REPLAN instead of dying at step 1 (R009/R018/R033/R013). NEED_USER with an
  unmet contract, blocked evidence, exhausted replans, or an already-fired
  marker keeps the waiting_user hand-off.
* RETRY + scoped-out tool - a verifier RETRY_EXECUTOR over an observation
  rejected as ``tool_skill_forbidden``/``unknown_tool`` routes to REPLAN
  instead of a provably unsatisfiable same-step re-invocation (R013).
* Verifier execution-state projection - the Executor's raw observations reach
  the Verifier only through the shared bounded projection (1,200-char
  visible_text excerpts, <= 10 pages, 48,000-char list budget), and the
  deterministic contract anchors ride in the Verifier state.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select

from backend.app.db.models import AgentStep, User, UserRole
from backend.app.domain.agent_runtime import (
    AgentRole,
    ComplexityLevel,
    RunStatus,
    StepStatus,
    VerificationDecision,
)
from backend.app.repositories import agent_runtime as run_repository
from backend.app.services.agent_runtime.executor_agent import ExecutorAgent
from backend.app.services.agent_runtime.planner_agent import PlannerAgent
from backend.app.services.agent_runtime.runtime import AgentRuntime
from backend.app.services.agent_runtime.schemas import (
    AgentBudget,
    AgentTaskRequest,
    ExecutionPlan,
    ExecutorResult,
    PlanStep,
    ToolObservation,
)
from backend.app.services.agent_runtime.tool_budget import ToolCallBudget
from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.agent_runtime.tool_registry import ToolDefinition, ToolRegistry
from backend.app.services.agent_runtime.turn_budget import AgentTurnBudget
from backend.app.services.agent_runtime.verifier_agent import VerifierAgent

_NEEDS_USER_REPLAN_MARKER = "<needs_user_replan>"


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
    """Succeeded direct-tool output that can still carry a blocked marker."""

    url: str
    status: str | None = None
    reason: str | None = None


class MatchOutput(BaseModel):
    source_url: str
    matches: list[dict[str, object]]


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


def _user(user_id: str) -> User:
    return User(
        id=user_id,
        account=f"{user_id}@example.test",
        nickname=user_id,
        password_hash="not-a-real-password-hash",
        role=UserRole.STUDENT,
    )


def _page_evidence(source: str = "https://jobs.example/1") -> dict[str, str]:
    return {
        "source_url": source,
        "content_hash": "a" * 64,
        "visible_text": "负责 AI Agent 开发，要求 Python 与 LangChain。",
    }


def _blocked_wechat_handler(_context, _payload) -> dict[str, object]:
    """Mirror the OCR-gated WeChat tool: succeeded output, blocked marker."""
    return {
        "url": "https://mp.weixin.qq.com/s/x",
        "status": "needs_manual_review",
        "reason": "ocr_disabled",
    }


def _register_match_tool(registry: ToolRegistry) -> None:
    registry.register(
        ToolDefinition(
            name="match-observed-jobs",
            skill_name="job-matching",
            input_model=EmptyInput,
            output_model=MatchOutput,
            allowed_roles=frozenset({AgentRole.executor}),
            handler=lambda _context, _payload: {
                "source_url": "https://jobs.example/a",
                "matches": [
                    {
                        "artifact_id": "observed:a",
                        "source_url": "https://jobs.example/a",
                        "score": 80,
                    }
                ],
            },
        )
    )


def _register_discovery_tools(
    registry: ToolRegistry, *, wechat_handler=None
) -> None:
    """Register the deliverable tool names used by the evidence contract."""
    registry.register(
        ToolDefinition(
            name="fetch-public-job-pages",
            skill_name="job-discovery",
            input_model=FetchPagesInput,
            output_model=FetchPagesOutput,
            allowed_roles=frozenset({AgentRole.executor}),
            handler=lambda _context, payload: {
                "pages": [_page_evidence(payload.urls[0])]
            },
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


def _plan_decision(allowed_skills: list[str]) -> dict[str, Any]:
    return {
        "action": "plan",
        "complexity": "L3",
        "success_criteria": ["完整 JD"],
        "steps": [
            {
                "step_id": "step-1",
                "objective": "完成步骤产出",
                "allowed_skills": allowed_skills,
                "requires_verification": True,
            }
        ],
    }


def _runtime_for_gateway(gateway: object, registry: ToolRegistry) -> AgentRuntime:
    return AgentRuntime(
        planner=PlannerAgent(gateway=gateway, tools=registry),
        executor=ExecutorAgent(gateway=gateway, tools=registry),
        verifier=VerifierAgent(gateway=gateway, tools=registry),
        agent_version="pev-test",
    )


def _steps(db_session, run_id: str) -> list[AgentStep]:
    return list(
        db_session.scalars(
            select(AgentStep).where(AgentStep.run_id == run_id)
        )
    )


def _events(db_session, run_id: str) -> list[str]:
    return [
        event.event_type for event in run_repository.list_events(db_session, run_id)
    ]


# ---------------------------------------------------------------------------
# Item 1 - NEED_USER contract gate -> bounded REPLAN
# ---------------------------------------------------------------------------


def test_needs_user_with_contract_met_converts_to_bounded_replan(db_session) -> None:
    """R009/R018/R033/R013: a NEED_USER over a tool-backed deliverable replans."""
    user = _user("user-c1")
    db_session.add(user)
    db_session.commit()
    gateway = RoleScriptedGateway({
        AgentRole.planner: [
            _plan_decision(["job-matching"]),
            _plan_decision(["job-matching"]),
        ],
        AgentRole.executor: [
            {"action": "call_tool", "tool_name": "match-observed-jobs", "tool_input": {}},
            {"action": "complete", "summary": "匹配完成"},
            {"action": "complete", "summary": "匹配完成"},
        ],
        AgentRole.verifier: [
            {"action": "decide", "verification_decision": "NEED_USER", "feedback": "请人工确认匹配结果"},
            {"action": "decide", "verification_decision": "PASS"},
        ],
    })
    registry = ToolRegistry()
    _register_match_tool(registry)

    result = _runtime_for_gateway(gateway, registry).run(
        db_session,
        user_id=user.id,
        task=AgentTaskRequest(
            goal="匹配岗位",
            allowed_skills=["job-matching"],
            budget=AgentBudget(
                max_agent_turns=8, max_tool_calls=8, max_replans=2
            ),
        ),
    )

    assert result.status is RunStatus.succeeded
    assert result.summary == "匹配完成"
    assert _events(db_session, result.run_id).count("plan_created") == 2
    assert _events(db_session, result.run_id).count("verification_replan") == 1
    steps = _steps(db_session, result.run_id)
    assert len(steps) == 2
    assert {step.error_code for step in steps} == {"replan_required", None}
    skipped = [step for step in steps if step.error_code == "replan_required"]
    assert skipped[0].status is StepStatus.skipped
    # The run loop appended the converted outcome (marker included) to
    # verifier_feedback, which the replanned Planner sees.
    planner_context = gateway.states[AgentRole.planner][1]["context"]
    assert planner_context["verifier_feedback"] == [
        f"请人工确认匹配结果 {_NEEDS_USER_REPLAN_MARKER}"
    ]
    # The Verifier receives the projected observations (never the anchor
    # contract booleans — the anchor feature was removed as a water source).
    verifier_state = gateway.states[AgentRole.verifier][0]
    assert "step_contract_met" not in verifier_state
    assert "has_blocked_evidence" not in verifier_state
    assert "succeeded_deliverable_tool_names" not in verifier_state
    assert "execution" in verifier_state


def test_needs_user_keeps_waiting_user_when_blocked_evidence_present(db_session) -> None:
    """Blocked evidence (OCR-off WeChat) always keeps the human hand-off."""
    user = _user("user-c2")
    db_session.add(user)
    db_session.commit()
    gateway = RoleScriptedGateway({
        AgentRole.planner: [_plan_decision(["job-discovery"])],
        AgentRole.executor: [
            {"action": "call_tool", "tool_name": "fetch-public-job-pages", "tool_input": {"urls": ["https://jobs.example/1"]}},
            {"action": "call_tool", "tool_name": "fetch-wechat-article", "tool_input": {"url": "https://mp.weixin.qq.com/s/x"}},
            {"action": "complete", "summary": "已收集部分页面"},
        ],
        AgentRole.verifier: [
            {"action": "decide", "verification_decision": "NEED_USER", "feedback": "请人工确认微信来源"},
        ],
    })
    registry = ToolRegistry()
    _register_discovery_tools(registry, wechat_handler=_blocked_wechat_handler)

    result = _runtime_for_gateway(gateway, registry).run(
        db_session,
        user_id=user.id,
        task=AgentTaskRequest(
            goal="找岗位",
            allowed_skills=["job-discovery"],
            budget=AgentBudget(
                max_agent_turns=8, max_tool_calls=8, max_replans=2
            ),
        ),
    )

    assert result.status is RunStatus.waiting_user
    assert result.summary == "请人工确认微信来源"
    steps = _steps(db_session, result.run_id)
    assert steps[0].error_code == "need_user"
    assert _events(db_session, result.run_id).count("verification_replan") == 0
    assert _events(db_session, result.run_id).count("plan_created") == 1
    verifier_state = gateway.states[AgentRole.verifier][0]
    assert "step_contract_met" not in verifier_state
    assert "has_blocked_evidence" not in verifier_state


def test_needs_user_keeps_waiting_user_when_contract_not_met(db_session) -> None:
    """A NEED_USER without a tool-backed deliverable stays a human hand-off."""
    user = _user("user-c3")
    db_session.add(user)
    db_session.commit()
    gateway = RoleScriptedGateway({
        AgentRole.planner: [_plan_decision(["job-discovery"])],
        AgentRole.executor: [{"action": "complete", "summary": "无证据完成"}],
        AgentRole.verifier: [
            {"action": "decide", "verification_decision": "NEED_USER", "feedback": "请提供岗位链接"},
        ],
    })

    result = _runtime_for_gateway(gateway, ToolRegistry()).run(
        db_session,
        user_id=user.id,
        task=AgentTaskRequest(
            goal="找岗位",
            allowed_skills=["job-discovery"],
            budget=AgentBudget(
                max_agent_turns=8, max_tool_calls=8, max_replans=2
            ),
        ),
    )

    assert result.status is RunStatus.waiting_user
    assert result.summary == "请提供岗位链接"
    steps = _steps(db_session, result.run_id)
    assert steps[0].error_code == "need_user"
    assert _events(db_session, result.run_id).count("verification_replan") == 0
    assert _events(db_session, result.run_id).count("plan_created") == 1
    verifier_state = gateway.states[AgentRole.verifier][0]
    assert "step_contract_met" not in verifier_state
    assert "has_blocked_evidence" not in verifier_state


def test_needs_user_conversion_is_once_per_run(db_session) -> None:
    """The marker makes the conversion fire once; a repeat NEED_USER waits."""
    user = _user("user-c4")
    db_session.add(user)
    db_session.commit()
    gateway = RoleScriptedGateway({
        AgentRole.planner: [
            _plan_decision(["job-matching"]),
            _plan_decision(["job-matching"]),
        ],
        AgentRole.executor: [
            {"action": "call_tool", "tool_name": "match-observed-jobs", "tool_input": {}},
            {"action": "complete", "summary": "匹配完成"},
            {"action": "call_tool", "tool_name": "match-observed-jobs", "tool_input": {}},
            {"action": "complete", "summary": "匹配完成"},
        ],
        AgentRole.verifier: [
            {"action": "decide", "verification_decision": "NEED_USER", "feedback": "请人工确认匹配结果"},
            {"action": "decide", "verification_decision": "NEED_USER", "feedback": "请人工再次确认匹配结果"},
        ],
    })
    registry = ToolRegistry()
    _register_match_tool(registry)

    result = _runtime_for_gateway(gateway, registry).run(
        db_session,
        user_id=user.id,
        task=AgentTaskRequest(
            goal="匹配岗位",
            allowed_skills=["job-matching"],
            budget=AgentBudget(
                max_agent_turns=8, max_tool_calls=8, max_replans=2
            ),
        ),
    )

    # First NEED_USER converted to a replan; the second identical one (marker
    # already present in verifier_feedback) stays a human hand-off even though
    # the replan budget would still allow another conversion.
    assert result.status is RunStatus.waiting_user
    assert result.summary == "请人工再次确认匹配结果"
    assert _events(db_session, result.run_id).count("verification_replan") == 1
    assert _events(db_session, result.run_id).count("plan_created") == 2
    steps = _steps(db_session, result.run_id)
    assert len(steps) == 2
    assert {step.error_code for step in steps} == {"replan_required", "need_user"}
    planner_context = gateway.states[AgentRole.planner][1]["context"]
    assert planner_context["verifier_feedback"] == [
        f"请人工确认匹配结果 {_NEEDS_USER_REPLAN_MARKER}"
    ]


def test_completion_gate_rejection_converts_to_bounded_replan(db_session) -> None:
    """An executor-declared success over an empty deliverable replans once."""
    user = _user("user-c7")
    db_session.add(user)
    db_session.commit()
    gateway = RoleScriptedGateway({
        AgentRole.planner: [
            {
                "action": "plan",
                "complexity": "L2",
                "success_criteria": ["有匹配报告"],
                "steps": [
                    {
                        "step_id": "step-1",
                        "objective": "完成匹配产出",
                        "allowed_skills": ["job-matching"],
                        "requires_verification": False,
                    }
                ],
            },
            {
                "action": "plan",
                "complexity": "L2",
                "success_criteria": ["有匹配报告"],
                "steps": [
                    {
                        "step_id": "step-2",
                        "objective": "完成匹配产出",
                        "allowed_skills": ["job-matching"],
                        "requires_verification": False,
                    }
                ],
            },
        ],
        AgentRole.executor: [
            {"action": "call_tool", "tool_name": "match-observed-jobs", "tool_input": {}},
            {"action": "complete", "summary": "匹配完成"},
            {"action": "call_tool", "tool_name": "match-observed-jobs", "tool_input": {}},
            {"action": "complete", "summary": "匹配完成"},
        ],
        AgentRole.verifier: [],
    })
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="match-observed-jobs",
            skill_name="job-matching",
            input_model=EmptyInput,
            output_model=MatchOutput,
            allowed_roles=frozenset({AgentRole.executor}),
            handler=lambda _context, _payload: {
                "source_url": "https://jobs.example/a",
                "matches": [],
            },
        )
    )

    result = _runtime_for_gateway(gateway, registry).run(
        db_session,
        user_id=user.id,
        task=AgentTaskRequest(
            goal="匹配岗位",
            allowed_skills=["job-matching"],
            budget=AgentBudget(max_agent_turns=8, max_tool_calls=8, max_replans=2),
        ),
    )

    # First gate rejection converted to a replan; the repeated rejection
    # (marker already present) stays a human hand-off.
    assert result.status is RunStatus.waiting_user
    assert result.summary == (
        "工具未产生可核验的交付物，当前总结不能视为完成。"
        "请提供可公开访问的岗位页面或补充必要信息后重试。"
    )
    assert _events(db_session, result.run_id).count("verification_replan") == 1
    steps = _steps(db_session, result.run_id)
    assert len(steps) == 2
    assert {step.error_code for step in steps} == {"replan_required", "need_user"}


def test_needs_user_conversion_tolerates_non_list_verifier_feedback(db_session) -> None:
    """A malformed (non-list) verifier_feedback context cannot block the gate."""
    user = _user("user-c5")
    db_session.add(user)
    db_session.commit()
    task = AgentTaskRequest(
        goal="匹配岗位",
        allowed_skills=["job-matching"],
        context={"verifier_feedback": "请人工确认匹配结果"},
        budget=AgentBudget(max_agent_turns=8, max_tool_calls=8, max_replans=2),
    )
    plan_step = PlanStep(
        step_id="step-1",
        objective="完成步骤产出",
        allowed_skills=["job-matching"],
        requires_verification=True,
    )
    plan = ExecutionPlan(
        task=task,
        created_by=AgentRole.planner,
        complexity=ComplexityLevel.L3,
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
    gateway = RoleScriptedGateway({
        AgentRole.planner: [],
        AgentRole.executor: [
            {"action": "call_tool", "tool_name": "match-observed-jobs", "tool_input": {}},
            {"action": "complete", "summary": "匹配完成"},
        ],
        AgentRole.verifier: [
            {"action": "decide", "verification_decision": "NEED_USER", "feedback": "请人工确认匹配结果"},
        ],
    })
    registry = ToolRegistry()
    _register_match_tool(registry)

    result = _runtime_for_gateway(gateway, registry)._run_step(
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
        replans=0,
    )

    assert result.error_code == "replan_required"
    assert step.status is StepStatus.skipped
    assert result.summary.endswith(_NEEDS_USER_REPLAN_MARKER)


def test_needs_user_keeps_waiting_user_when_replan_budget_exhausted(db_session) -> None:
    """With max_replans=0 the conversion never fires; the human hand-off stays."""
    user = _user("user-c5")
    db_session.add(user)
    db_session.commit()
    gateway = RoleScriptedGateway({
        AgentRole.planner: [_plan_decision(["job-matching"])],
        AgentRole.executor: [
            {"action": "call_tool", "tool_name": "match-observed-jobs", "tool_input": {}},
            {"action": "complete", "summary": "匹配完成"},
        ],
        AgentRole.verifier: [
            {"action": "decide", "verification_decision": "NEED_USER", "feedback": "请人工确认匹配结果"},
        ],
    })
    registry = ToolRegistry()
    _register_match_tool(registry)

    result = _runtime_for_gateway(gateway, registry).run(
        db_session,
        user_id=user.id,
        task=AgentTaskRequest(
            goal="匹配岗位",
            allowed_skills=["job-matching"],
            budget=AgentBudget(
                max_agent_turns=8, max_tool_calls=8, max_replans=0
            ),
        ),
    )

    assert result.status is RunStatus.waiting_user
    assert result.summary == "请人工确认匹配结果"
    assert _events(db_session, result.run_id).count("verification_replan") == 0
    assert _events(db_session, result.run_id).count("plan_created") == 1


# ---------------------------------------------------------------------------
# Item 2 - RETRY over a scoped-out tool -> REPLAN
# ---------------------------------------------------------------------------


def test_retry_with_forbidden_tool_routes_to_replan(db_session) -> None:
    """R013: RETRY over tool_skill_forbidden replans instead of re-invoking."""
    user = _user("user-c6")
    db_session.add(user)
    db_session.commit()
    gateway = RoleScriptedGateway({
        AgentRole.planner: [
            _plan_decision(["job-matching"]),
            _plan_decision(["job-matching"]),
        ],
        AgentRole.executor: [
            {"action": "call_tool", "tool_name": "fetch-public-job-pages", "tool_input": {"urls": ["https://jobs.example/1"]}},
            {"action": "complete", "summary": "信息不完整"},
            {"action": "complete", "summary": "匹配完成"},
        ],
        AgentRole.verifier: [
            {"action": "decide", "verification_decision": "RETRY_EXECUTOR", "feedback": "缺少匹配证据"},
            {"action": "decide", "verification_decision": "PASS"},
        ],
    })
    registry = ToolRegistry()
    _register_match_tool(registry)
    _register_discovery_tools(registry)

    result = _runtime_for_gateway(gateway, registry).run(
        db_session,
        user_id=user.id,
        task=AgentTaskRequest(
            goal="匹配岗位",
            allowed_skills=["job-matching"],
            budget=AgentBudget(
                max_agent_turns=8, max_tool_calls=8, max_replans=2
            ),
        ),
    )

    assert result.status is RunStatus.succeeded
    assert _events(db_session, result.run_id).count("plan_created") == 2
    assert _events(db_session, result.run_id).count("verification_replan") == 1
    steps = _steps(db_session, result.run_id)
    skipped = [step for step in steps if step.error_code == "replan_required"]
    assert len(skipped) == 1
    assert skipped[0].status is StepStatus.skipped
    # The executor was NOT re-invoked within the same step: two decisions in
    # the first invocation + one in the replanned step, never a third.
    assert len(gateway.states[AgentRole.executor]) == 3
    verifier_state = gateway.states[AgentRole.verifier][0]
    assert "step_contract_met" not in verifier_state
    assert "succeeded_deliverable_tool_names" not in verifier_state
    # The scope violation is detected before verifier invocation, so the
    # runtime supplies a deterministic replan reason rather than repeating
    # the verifier feedback after an impossible call.
    assert gateway.states[AgentRole.planner][1]["context"]["verifier_feedback"] == [
        "步骤 Skill 范围冲突，已停止重复工具调用并请求重规划。"
    ]


def test_retry_with_unknown_tool_routes_to_replan(db_session) -> None:
    """RETRY over an unknown_tool observation is equally unsatisfiable."""
    user = _user("user-c7")
    db_session.add(user)
    db_session.commit()
    gateway = RoleScriptedGateway({
        AgentRole.planner: [
            _plan_decision(["job-matching"]),
            _plan_decision(["job-matching"]),
        ],
        AgentRole.executor: [
            {"action": "call_tool", "tool_name": "no-such-tool", "tool_input": {}},
            {"action": "complete", "summary": "信息不完整"},
            {"action": "complete", "summary": "匹配完成"},
        ],
        AgentRole.verifier: [
            {"action": "decide", "verification_decision": "RETRY_EXECUTOR", "feedback": "缺少匹配证据"},
            {"action": "decide", "verification_decision": "PASS"},
        ],
    })
    registry = ToolRegistry()
    _register_match_tool(registry)

    result = _runtime_for_gateway(gateway, registry).run(
        db_session,
        user_id=user.id,
        task=AgentTaskRequest(
            goal="匹配岗位",
            allowed_skills=["job-matching"],
            budget=AgentBudget(
                max_agent_turns=8, max_tool_calls=8, max_replans=2
            ),
        ),
    )

    assert result.status is RunStatus.succeeded
    assert _events(db_session, result.run_id).count("plan_created") == 2
    assert _events(db_session, result.run_id).count("verification_replan") == 1
    skipped = [
        step
        for step in _steps(db_session, result.run_id)
        if step.error_code == "replan_required"
    ]
    assert len(skipped) == 1
    assert skipped[0].status is StepStatus.skipped
    assert len(gateway.states[AgentRole.executor]) == 3


def test_retry_keeps_normal_reinvocation_without_forbidden_observation(db_session) -> None:
    """A plain RETRY (no forbidden/unknown observation) still re-invokes."""
    user = _user("user-c8")
    db_session.add(user)
    db_session.commit()
    gateway = RoleScriptedGateway({
        AgentRole.planner: [_plan_decision(["job-matching"])],
        AgentRole.executor: [
            {"action": "call_tool", "tool_name": "match-observed-jobs", "tool_input": {}},
            {"action": "complete", "summary": "信息不完整"},
            {"action": "complete", "summary": "匹配完成"},
        ],
        AgentRole.verifier: [
            {"action": "decide", "verification_decision": "RETRY_EXECUTOR", "feedback": "补充来源标注。"},
            {"action": "decide", "verification_decision": "PASS"},
        ],
    })
    registry = ToolRegistry()
    _register_match_tool(registry)

    result = _runtime_for_gateway(gateway, registry).run(
        db_session,
        user_id=user.id,
        task=AgentTaskRequest(
            goal="匹配岗位",
            allowed_skills=["job-matching"],
            budget=AgentBudget(
                max_agent_turns=8, max_tool_calls=8, max_replans=2
            ),
        ),
    )

    assert result.status is RunStatus.succeeded
    assert _events(db_session, result.run_id).count("plan_created") == 1
    assert _events(db_session, result.run_id).count("verification_replan") == 0
    assert len(gateway.states[AgentRole.executor]) == 3
    assert gateway.states[AgentRole.executor][2]["verifier_feedback"] == [
        "补充来源标注。"
    ]


# ---------------------------------------------------------------------------
# Item 3 + 4 - Verifier execution-state projection and contract anchors
# ---------------------------------------------------------------------------


class VerifierScriptedGateway:
    """Deterministic model boundary double for direct VerifierAgent tests."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = responses
        self.states: list[dict[str, Any]] = []

    def decide(
        self,
        *,
        role: AgentRole,
        instruction: str,
        state: dict[str, Any],
        response_model: type[BaseModel],
    ) -> BaseModel:
        assert role is AgentRole.verifier
        self.states.append(state)
        return response_model.model_validate(self.responses.pop(0))


def _verifier_task() -> AgentTaskRequest:
    return AgentTaskRequest(goal="找岗位", allowed_skills=["job-discovery"])


def _verifier_plan(task: AgentTaskRequest) -> ExecutionPlan:
    return ExecutionPlan(
        task=task,
        created_by=AgentRole.planner,
        complexity=ComplexityLevel.L3,
        success_criteria=["完整 JD"],
        steps=[
            PlanStep(
                step_id="discover",
                objective="提取岗位",
                allowed_skills=["job-discovery"],
                requires_verification=True,
            )
        ],
    )


def test_verifier_execution_state_projection_is_bounded(db_session) -> None:
    """The Verifier never sees raw full-width Executor observations."""
    gateway = VerifierScriptedGateway(
        [{"action": "decide", "verification_decision": "PASS"}]
    )
    raw_text = "RAW_PAGE_BODY_" * 1000  # 14,000 chars, well over the excerpt
    observations = [
        ToolObservation(
            tool_name="fetch-public-job-pages",
            status="succeeded",
            output={
                "source_url": f"https://jobs.example/{index}",
                "content_hash": "b" * 64,
                "visible_text": raw_text,
            },
        )
        for index in range(50)
    ]
    # A batch observation with more pages than the projection cap.
    observations.append(
        ToolObservation(
            tool_name="fetch-public-job-pages",
            status="succeeded",
            output={
                "source_url": "https://jobs.example/batch",
                "content_hash": "c" * 64,
                "pages": [
                    {
                        "source_url": f"https://jobs.example/p{page}",
                        "content_hash": "d" * 64,
                        "visible_text": raw_text,
                    }
                    for page in range(20)
                ],
            },
        )
    )
    execution = ExecutorResult(
        status="succeeded", summary="完成", observations=observations
    )
    task = _verifier_task()
    plan = _verifier_plan(task)

    result = VerifierAgent(gateway=gateway, tools=ToolRegistry()).run(
        task=task,
        plan=plan,
        step=plan.steps[0],
        execution=execution,
        context=ToolContext(user_id="user-a", run_id="run-a"),
    )

    assert result.decision is VerificationDecision.PASS
    state = gateway.states[0]
    projected = state["execution"]["observations"]
    assert isinstance(projected, list)
    # The 48,000-char list budget held: older observations collapsed to
    # identifier-only summary lines, only the newest stay full.
    assert len(json.dumps(state["execution"], ensure_ascii=False)) <= 48_000
    assert len(projected) == len(observations)
    # The full-width raw page body never reaches the model in one piece.
    assert raw_text not in json.dumps(state, ensure_ascii=False)
    # Summarized lines carry identity only - never output/visible_text/pages.
    assert all("output" not in entry for entry in projected[:-5])
    assert all(entry["tool_name"] == "fetch-public-job-pages" for entry in projected)
    # The newest observations stay full but per-observation bounded.
    recent = projected[-5:]
    assert all(
        isinstance(entry["output"], dict)
        and len(entry["output"]["visible_text"]) <= 1_200
        for entry in recent[:-1]
    )
    assert len(recent[-1]["output"]["pages"]) == 10  # capped, not 20
    # The Verifier state carries no contract anchors (feature removed); only
    # the projected execution observations and tool history remain.
    assert "step_contract_met" not in state
    assert "has_blocked_evidence" not in state
    assert "succeeded_deliverable_tool_names" not in state
    assert all(
        call["tool_name"] == "fetch-public-job-pages"
        for call in state["execution_tool_calls"]
    )


def test_verifier_projection_excludes_verifier_own_observation_dump(db_session) -> None:
    """The verifier's own tool calls stay separate from the projected execution."""
    gateway = VerifierScriptedGateway(
        [{"action": "decide", "verification_decision": "PASS"}]
    )
    execution = ToolObservation(
        tool_name="fetch-public-job-pages",
        status="failed",
        error_code="public_fetch_failed",
    )
    executor_result = ExecutorResult(
        status="succeeded",
        summary="完成",
        observations=[execution],
    )
    task = _verifier_task()
    plan = _verifier_plan(task)

    result = VerifierAgent(gateway=gateway, tools=ToolRegistry()).run(
        task=task,
        plan=plan,
        step=plan.steps[0],
        execution=executor_result,
        context=ToolContext(user_id="user-a", run_id="run-a"),
    )

    assert result.decision is VerificationDecision.PASS
    state = gateway.states[0]
    assert state["my_tool_calls"] == []
    assert state["execution"]["observations"][0]["status"] == "failed"
    assert state["execution"]["observations"][0]["error_code"] == (
        "public_fetch_failed"
    )
    assert "step_contract_met" not in state
