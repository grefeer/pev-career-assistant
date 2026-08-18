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

from typing import Any

from pydantic import BaseModel
from sqlalchemy import select

from backend.app.db.models import AgentStep, User, UserRole
from backend.app.domain.agent_runtime import (
    AgentRole,
    ComplexityLevel,
    RunStatus,
    StepStatus,
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
)
from backend.app.services.agent_runtime.skill_definition import (
    CompletionContract,
    SkillDefinition,
    SkillRegistry,
)
from backend.app.services.agent_runtime.tool_budget import ToolCallBudget
from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.agent_runtime.tool_registry import ToolDefinition, ToolRegistry
from backend.app.services.agent_runtime.turn_budget import AgentTurnBudget
from backend.app.services.career_skills.manifest import (
    build_career_skill_registry,
    skill_observation_is_semantically_valid,
)
from backend.app.services.career_skills.registry import build_career_tool_registry
from tests.unit.deepagents_testkit import scripted_executor_model

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
        # Stage 1.2: the executor runs on the production Deep path, so the
        # scripted executor decisions are exposed through a chat model for the
        # Deep loop instead of the removed legacy decide() seam. Tests that
        # never drive the executor (verifier-only or stub-executor flows)
        # leave _model None, which the Deep executor reports as unavailable.
        executor_script = scripts.get(AgentRole.executor) or []
        self._model = (
            scripted_executor_model(list(executor_script))
            if executor_script
            else None
        )

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
                        "evidence_excerpt": "匹配证据文本",
                    }
                ],
            },
        )
    )


def _match_contract_skills() -> SkillRegistry:
    """Strict job-matching contract with OPTIONAL verification.

    The completion-gate-rejection test drives the no-verification path
    (L2 plan, requires_verification=False); the production registry marks
    job-matching REQUIRED, which would force the verifier instead.
    """
    return SkillRegistry(
        [
            SkillDefinition(
                name="job-matching",
                completion_contract=CompletionContract(
                    deliverable_tools=frozenset({"match-observed-jobs"}),
                    semantic_check=lambda observation: skill_observation_is_semantically_valid(
                        observation.tool_name, observation.output
                    ),
                ),
            )
        ]
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


def _runtime_for_gateway(
    gateway: object,
    registry: ToolRegistry,
    *,
    skills: SkillRegistry | None = None,
) -> AgentRuntime:
    return AgentRuntime(
        planner=PlannerAgent(gateway=gateway, tools=registry),
        executor=ExecutorAgent(gateway=gateway, tools=registry, skills=SkillRegistry()),

        agent_version="pev-test",
        skills=skills or build_career_skill_registry(registry),
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
        AgentRole.planner: [_plan_decision(["job-matching"])],
        AgentRole.executor: [
            {"action": "call_tool", "tool_name": "match-observed-jobs", "tool_input": {}},
            {"action": "complete", "summary": "匹配完成"},
        ],
        AgentRole.verifier: [],
    })
    registry = ToolRegistry()
    _register_match_tool(registry)

    result = _runtime_for_gateway(gateway, registry, skills=build_career_skill_registry(build_career_tool_registry())).run(
        db_session,
        user_id=user.id,
        task=AgentTaskRequest(
            goal="匹配岗位",
            allowed_skills=["job-matching"],
            budget=AgentBudget(
                max_agent_turns=8, max_tool_calls=8, max_replans=2,
                max_auto_recoveries=0,
            ),
        ),
    )

    # A tool-backed, semantically valid deliverable makes the deterministic
    # gate PASS on the first pass; no replan and no verifier turn happen.
    assert result.status is RunStatus.succeeded
    assert result.summary == "匹配完成"
    assert _events(db_session, result.run_id).count("plan_created") == 1
    assert _events(db_session, result.run_id).count("verification_replan") == 0
    assert _events(db_session, result.run_id).count("verification_passed") == 1
    steps = _steps(db_session, result.run_id)
    assert len(steps) == 1
    assert steps[0].error_code is None


def test_needs_user_keeps_waiting_user_when_blocked_evidence_present(db_session) -> None:
    """Blocked evidence (OCR-off WeChat) always keeps the human hand-off."""
    user = _user("user-c2")
    db_session.add(user)
    db_session.commit()
    gateway = RoleScriptedGateway({
        AgentRole.planner: [_plan_decision(["job-discovery"])],
        AgentRole.executor: [
            {"action": "call_tool", "tool_name": "fetch-wechat-article", "tool_input": {"url": "https://mp.weixin.qq.com/s/x"}},
            {"action": "complete", "summary": "已收集部分页面"},
        ],
        AgentRole.verifier: [],
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
                max_agent_turns=8, max_tool_calls=8, max_replans=2,
                max_auto_recoveries=0,
            ),
        ),
    )

    # Blocked-only evidence (no deliverable) makes the gate pause with
    # NEED_USER; the human keeps the hand-off and no replan is spent.
    assert result.status is RunStatus.waiting_user
    assert "访问限制阻断" in (result.summary or "")
    steps = _steps(db_session, result.run_id)
    assert steps[0].error_code == "need_user"
    assert _events(db_session, result.run_id).count("verification_replan") == 0
    assert _events(db_session, result.run_id).count("plan_created") == 1


def test_needs_user_keeps_waiting_user_when_contract_not_met(db_session) -> None:
    """A NEED_USER without a tool-backed deliverable stays a human hand-off."""
    user = _user("user-c3")
    db_session.add(user)
    db_session.commit()
    gateway = RoleScriptedGateway({
        AgentRole.planner: [_plan_decision(["job-discovery"])],
        AgentRole.executor: [
            {"action": "complete", "summary": "无证据完成"},
            {"action": "complete", "summary": "无证据完成"},
            {"action": "complete", "summary": "无证据完成"},
        ],
    })

    result = _runtime_for_gateway(gateway, ToolRegistry()).run(
        db_session,
        user_id=user.id,
        task=AgentTaskRequest(
            goal="找岗位",
            allowed_skills=["job-discovery"],
            budget=AgentBudget(
                max_agent_turns=8, max_tool_calls=8, max_replans=2,
                max_auto_recoveries=0,
            ),
        ),
    )

    # No tool evidence at all: the deterministic gate RETRYs and the repeated
    # no-progress fingerprint hands the step to the human without a replan.
    assert result.status is RunStatus.waiting_user
    assert "未满足确定性交付契约" in (result.summary or "")
    steps = _steps(db_session, result.run_id)
    assert steps[0].error_code == "no_progress_duplicate"
    assert _events(db_session, result.run_id).count("verification_replan") == 0
    assert _events(db_session, result.run_id).count("plan_created") == 1


def test_gate_retry_loop_hands_off_on_empty_report(db_session) -> None:
    """A gate RETRY over an unchanged fingerprint hands the step to the human."""
    user = _user("user-c4")
    db_session.add(user)
    db_session.commit()
    gateway = RoleScriptedGateway({
        AgentRole.planner: [_plan_decision(["job-matching"])],
        AgentRole.executor: [
            {"action": "call_tool", "tool_name": "match-observed-jobs", "tool_input": {}},
            {"action": "complete", "summary": "匹配完成"},
            {"action": "complete", "summary": "匹配完成"},
            {"action": "complete", "summary": "匹配完成"},
        ],
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
            budget=AgentBudget(
                max_agent_turns=8, max_tool_calls=8, max_replans=2,
                max_auto_recoveries=0,
            ),
        ),
    )

    # The empty match report cannot satisfy the semantic contract: the gate
    # RETRYs, the second retry repeats the same evidence fingerprint, and the
    # runtime hands the step to the human without spending a replan.
    assert result.status is RunStatus.waiting_user
    assert "未满足确定性交付契约" in (result.summary or "")
    assert _events(db_session, result.run_id).count("verification_replan") == 0
    assert _events(db_session, result.run_id).count("plan_created") == 1
    steps = _steps(db_session, result.run_id)
    assert len(steps) == 1
    assert steps[0].error_code == "no_progress_duplicate"


class StubInvalidResponseExecutor:
    """Executor stub returning the Deep Executor's unparseable-terminal hand-off."""

    def run(self, **kwargs: Any) -> ExecutorResult:
        return ExecutorResult(
            status="needs_user",
            user_question="模型未返回可解析的终态，请补充岗位正文或重试。",
            error_code="deep_executor_invalid_response",
        )


def test_deep_executor_invalid_response_converts_to_bounded_replan(
    db_session,
) -> None:
    """An unparseable terminal with an unmet contract replans once per run."""
    user = _user("user-c8")
    db_session.add(user)
    db_session.commit()
    gateway = RoleScriptedGateway({
        AgentRole.planner: [
            _plan_decision(["job-matching"]),
            _plan_decision(["job-matching"]),
        ],
        AgentRole.executor: [],
        AgentRole.verifier: [],
    })
    registry = ToolRegistry()
    _register_match_tool(registry)
    runtime = AgentRuntime(
        planner=PlannerAgent(gateway=gateway, tools=registry),
        executor=StubInvalidResponseExecutor(),

        agent_version="pev-test",
        skills=SkillRegistry(),
    )

    result = runtime.run(
        db_session,
        user_id=user.id,
        task=AgentTaskRequest(
            goal="匹配岗位",
            allowed_skills=["job-matching"],
            budget=AgentBudget(
                max_agent_turns=8, max_tool_calls=8, max_replans=2,
                max_auto_recoveries=0,
            ),
        ),
    )

    # First invalid response converted to a replan; the repeated one (marker
    # already present) stays a human hand-off.
    assert result.status is RunStatus.waiting_user
    assert result.summary == "模型未返回可解析的终态，请补充岗位正文或重试。"
    assert _events(db_session, result.run_id).count("verification_replan") == 1
    steps = _steps(db_session, result.run_id)
    assert len(steps) == 2
    assert {step.error_code for step in steps} == {"replan_required", "need_user"}


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

    result = _runtime_for_gateway(gateway, registry, skills=_match_contract_skills()).run(
        db_session,
        user_id=user.id,
        task=AgentTaskRequest(
            goal="匹配岗位",
            allowed_skills=["job-matching"],
            budget=AgentBudget(
                max_agent_turns=8, max_tool_calls=8, max_replans=2,
                max_auto_recoveries=0,
            ),
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
            {"action": "complete", "summary": "匹配完成"},
            {"action": "complete", "summary": "匹配完成"},
        ],
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

    # A malformed (non-list) verifier_feedback context cannot crash the gate
    # retry loop; the repeated no-progress fingerprint hands the step off.
    assert result.error_code == "no_progress_duplicate"
    assert step.status is StepStatus.failed


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
            {"action": "complete", "summary": "匹配完成"},
            {"action": "complete", "summary": "匹配完成"},
        ],
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
            budget=AgentBudget(
                max_agent_turns=8, max_tool_calls=8, max_replans=0,
                max_auto_recoveries=0,
            ),
        ),
    )

    # With no replan budget the gate RETRY cannot convert; the repeated
    # no-progress fingerprint keeps the human hand-off.
    assert result.status is RunStatus.waiting_user
    assert "未满足确定性交付契约" in (result.summary or "")
    assert _events(db_session, result.run_id).count("verification_replan") == 0
    assert _events(db_session, result.run_id).count("plan_created") == 1


# ---------------------------------------------------------------------------
# Item 2 - RETRY over a scoped-out tool -> REPLAN
# ---------------------------------------------------------------------------


def test_out_of_scope_tool_call_never_reaches_the_registry(db_session) -> None:
    """The Deep catalog is skill-scoped: a job-discovery tool is structurally
    invisible inside a job-matching step, so no forbidden observation is ever
    produced (the legacy R013 replan seam is unreachable)."""
    user = _user("user-c6")
    db_session.add(user)
    db_session.commit()
    fetch_calls = {"count": 0}

    def counting_fetch(_context, _payload):
        fetch_calls["count"] += 1
        return {"pages": []}

    gateway = RoleScriptedGateway({
        AgentRole.planner: [_plan_decision(["job-matching"])],
        AgentRole.executor: [
            {"action": "call_tool", "tool_name": "fetch-public-job-pages", "tool_input": {"urls": ["https://jobs.example/1"]}},
            {"action": "complete", "summary": "信息不完整"},
            {"action": "complete", "summary": "匹配完成"},
            {"action": "complete", "summary": "匹配完成"},
        ],
    })
    registry = ToolRegistry()
    _register_match_tool(registry)
    registry.register(ToolDefinition(
        name="fetch-public-job-pages", skill_name="job-discovery", input_model=FetchPagesInput,
        output_model=FetchPagesOutput, allowed_roles=frozenset({AgentRole.executor}),
        handler=counting_fetch,
    ))

    result = _runtime_for_gateway(gateway, registry).run(
        db_session,
        user_id=user.id,
        task=AgentTaskRequest(
            goal="匹配岗位",
            allowed_skills=["job-matching"],
            budget=AgentBudget(
                max_agent_turns=8, max_tool_calls=8, max_replans=2,
                max_auto_recoveries=0,
            ),
        ),
    )

    assert result.status is RunStatus.waiting_user
    assert _events(db_session, result.run_id).count("plan_created") == 1
    assert _events(db_session, result.run_id).count("verification_replan") == 0
    # The scoped-out fetch was never invoked and produced no observation.
    assert fetch_calls["count"] == 0


def test_unregistered_tool_call_never_reaches_the_registry(db_session) -> None:
    """An unregistered tool name cannot produce an unknown_tool observation on
    the Deep path: the wrapped catalog only exposes registered tools, so the
    run continues with the model's own recovery decision."""
    user = _user("user-c7")
    db_session.add(user)
    db_session.commit()
    gateway = RoleScriptedGateway({
        AgentRole.planner: [_plan_decision(["job-matching"])],
        AgentRole.executor: [
            {"action": "call_tool", "tool_name": "no-such-tool", "tool_input": {}},
            {"action": "complete", "summary": "信息不完整"},
            {"action": "complete", "summary": "匹配完成"},
            {"action": "complete", "summary": "匹配完成"},
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
                max_agent_turns=8, max_tool_calls=8, max_replans=2,
                max_auto_recoveries=0,
            ),
        ),
    )

    assert result.status is RunStatus.waiting_user
    assert _events(db_session, result.run_id).count("plan_created") == 1
    assert _events(db_session, result.run_id).count("verification_replan") == 0


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


# Item 3 + 4 - Verifier execution-state projection and contract anchors
# ---------------------------------------------------------------------------
