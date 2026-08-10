"""Candidate C (round4): termination logic + dead-link search authorization.

Round-4 behaviors under test:

* N3 RETRY-cap -> bounded replan - a verifier RETRY_EXECUTOR exhaustion over a
  satisfied deterministic step contract (tool-backed deliverable, no blocked
  evidence, replan budget remaining, once-per-run marker absent) converts to a
  bounded REPLAN instead of an unnecessary human hand-off. Exhaustion with
  blocked evidence (login/captcha/anti-bot) or an already-fired marker keeps
  the waiting_user hand-off.
* N4 isomorphic-replan guard - when the replanned plan repeats the exact step
  sequence that already failed to converge AND the replan budget is exhausted
  (max_replans >= 2), the run terminates honestly as waiting_user instead of
  re-executing the identical structure (C008 wall-clock oscillation). A
  structurally different plan always executes; a budget of 1 keeps the guard
  off (the loop is already bounded to two planner passes).
* W3 verifier-feedback scoping - verifier feedback fragments naming a tool
  outside the current step's skill catalog are filtered from the Executor's
  decision state (never from stored feedback), so a scoped-out demand cannot
  push the executor toward a tool_skill_forbidden drift.
* W2 soft-404 dead links + search authorization - a fetched page that is a
  soft-404 (页面不存在/职位已下线/职位不存在/页面已经过期 in title or body
  head, or "404" in title, with almost no JD body) fails as ``dead_link`` (a
  neutral failure, never needs_manual_review); public search is authorized
  only after EVERY candidate URL failed (fetch error or dead link) - partial
  or blocked failures never authorize search.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from pydantic import BaseModel
from sqlalchemy import select

from backend.app.db.models import AgentStep, User, UserRole
from backend.app.domain.agent_runtime import (
    AgentRole,
    RunStatus,
    StepStatus,
)
from backend.app.repositories import agent_runtime as run_repository
from backend.app.services.agent_runtime.evidence_gate import is_blocked_error
from backend.app.services.agent_runtime.executor_agent import (
    ExecutorAgent,
    _scope_feedback_to_step_catalog,
)
from backend.app.services.agent_runtime.planner_agent import PlannerAgent
from backend.app.services.agent_runtime.runtime import AgentRuntime
from backend.app.services.agent_runtime.schemas import (
    AgentBudget,
    AgentTaskRequest,
)
from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.agent_runtime.tool_registry import ToolDefinition, ToolRegistry
from backend.app.services.agent_runtime.verifier_agent import VerifierAgent
from backend.app.services.career_skills.job_discovery import (
    FetchPublicJobPageInput,
    PublicJobFetchError,
    fetch_public_job_page,
)

_RETRY_REPLAN_MARKER = "<retry_replan>"


class EmptyInput(BaseModel):
    pass


class FetchPagesInput(BaseModel):
    urls: list[str]


class FetchPagesOutput(BaseModel):
    pages: list[dict[str, object]]
    failures: list[dict[str, str]] = []


class SearchInput(BaseModel):
    query: str | None = None


class SearchOutput(BaseModel):
    results: list[dict[str, str]]


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

    def __init__(self, scripts: dict[AgentRole, list[dict[str, object]]]) -> None:
        self.scripts = scripts
        self.states: dict[AgentRole, list[dict[str, object]]] = {
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
        state: dict[str, object],
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


def _plan_decision(allowed_skills: list[str]) -> dict[str, object]:
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
        db_session.scalars(select(AgentStep).where(AgentStep.run_id == run_id))
    )


def _events(db_session, run_id: str) -> list[str]:
    return [
        event.event_type for event in run_repository.list_events(db_session, run_id)
    ]


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


def _register_tailoring_tool(registry: ToolRegistry) -> None:
    """The universe-side tailoring tool: in the universe, out of job-matching scope."""
    registry.register(
        ToolDefinition(
            name="build-resume-tailoring-brief",
            skill_name="resume-tailoring",
            input_model=EmptyInput,
            output_model=MatchOutput,
            allowed_roles=frozenset({AgentRole.executor}),
            handler=lambda _context, _payload: {
                "source_url": "https://jobs.example/b",
                "matches": [],
            },
        )
    )


def _register_discovery_tools(registry: ToolRegistry, *, wechat_handler=None) -> None:
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


def _blocked_wechat_handler(_context, _payload) -> dict[str, str]:
    """Mirror the OCR-gated WeChat tool: succeeded output, blocked marker."""
    return {
        "url": "https://mp.weixin.qq.com/s/x",
        "status": "needs_manual_review",
        "reason": "ocr_disabled",
    }


def _register_search_tool(registry: ToolRegistry) -> None:
    registry.register(
        ToolDefinition(
            name="search-public-job-pages",
            skill_name="job-discovery",
            input_model=SearchInput,
            output_model=SearchOutput,
            allowed_roles=frozenset({AgentRole.executor}),
            handler=lambda _context, _payload: {
                "results": [
                    {"title": "AI 应用开发", "url": "https://jobs.example/s1"}
                ]
            },
        )
    )


def _register_batch_fetch(
    registry: ToolRegistry, *, pages: list[dict[str, object]], failures: list[dict[str, str]]
) -> None:
    registry.register(
        ToolDefinition(
            name="fetch-public-job-pages",
            skill_name="job-discovery",
            input_model=FetchPagesInput,
            output_model=FetchPagesOutput,
            allowed_roles=frozenset({AgentRole.executor}),
            handler=lambda _context, _payload: {"pages": pages, "failures": failures},
        )
    )


# ---------------------------------------------------------------------------
# N3 - RETRY-cap over a satisfied contract -> bounded REPLAN
# ---------------------------------------------------------------------------


def test_retry_cap_with_contract_met_routes_to_bounded_replan(db_session) -> None:
    """RETRY exhaustion over a tool-backed, unblocked deliverable replans (N3)."""
    user = _user("user-c-1")
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
            {"action": "complete", "summary": "匹配完成"},
            {"action": "complete", "summary": "匹配完成"},
        ],
        AgentRole.verifier: [
            {"action": "decide", "verification_decision": "RETRY_EXECUTOR", "feedback": "缺少匹配证据"},
            {"action": "decide", "verification_decision": "RETRY_EXECUTOR", "feedback": "缺少匹配证据"},
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
                max_agent_turns=16, max_tool_calls=8, max_replans=2
            ),
        ),
    )

    # The third RETRY (retries=3 > max_replans=2) over the satisfied contract
    # converted to a bounded replan; the replanned identical step passed.
    assert result.status is RunStatus.succeeded
    assert result.summary == "匹配完成"
    assert _events(db_session, result.run_id).count("plan_created") == 2
    assert _events(db_session, result.run_id).count("verification_replan") == 1
    steps = _steps(db_session, result.run_id)
    assert {step.error_code for step in steps} == {"replan_required", None}
    skipped = [step for step in steps if step.error_code == "replan_required"]
    assert skipped[0].status is StepStatus.skipped
    # The converted summary (marker included) reached the replanned Planner.
    planner_context = gateway.states[AgentRole.planner][1]["context"]
    assert planner_context["verifier_feedback"] == [
        f"缺少匹配证据 {_RETRY_REPLAN_MARKER}"
    ]


def test_retry_cap_conversion_is_once_per_run(db_session) -> None:
    """The N3 marker makes the conversion fire once; a repeat exhaustion waits."""
    user = _user("user-c-2")
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
            {"action": "complete", "summary": "匹配完成"},
            {"action": "complete", "summary": "匹配完成"},
            {"action": "complete", "summary": "匹配完成"},
            {"action": "complete", "summary": "匹配完成"},
        ],
        AgentRole.verifier: [
            {"action": "decide", "verification_decision": "RETRY_EXECUTOR", "feedback": "缺少匹配证据"},
            {"action": "decide", "verification_decision": "RETRY_EXECUTOR", "feedback": "缺少匹配证据"},
            {"action": "decide", "verification_decision": "RETRY_EXECUTOR", "feedback": "缺少匹配证据"},
            {"action": "decide", "verification_decision": "RETRY_EXECUTOR", "feedback": "缺少匹配证据"},
            {"action": "decide", "verification_decision": "RETRY_EXECUTOR", "feedback": "缺少匹配证据"},
            {"action": "decide", "verification_decision": "RETRY_EXECUTOR", "feedback": "缺少匹配证据"},
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
                max_agent_turns=16, max_tool_calls=8, max_replans=2
            ),
        ),
    )

    # First exhaustion converted to a replan; the second identical one (marker
    # already present in verifier_feedback) stays a human hand-off even though
    # the replan budget would still allow another conversion.
    assert result.status is RunStatus.waiting_user
    assert "缺少匹配证据" in (result.summary or "")
    assert _events(db_session, result.run_id).count("verification_replan") == 1
    assert _events(db_session, result.run_id).count("plan_created") == 2
    steps = _steps(db_session, result.run_id)
    assert len(steps) == 2
    assert {step.error_code for step in steps} == {"replan_required", "need_user"}
    planner_context = gateway.states[AgentRole.planner][1]["context"]
    assert planner_context["verifier_feedback"] == [
        f"缺少匹配证据 {_RETRY_REPLAN_MARKER}"
    ]


def test_retry_cap_keeps_waiting_user_when_blocked_evidence_present(db_session) -> None:
    """Blocked evidence always keeps the human hand-off (N3 never fires)."""
    user = _user("user-c-3")
    db_session.add(user)
    db_session.commit()
    gateway = RoleScriptedGateway({
        AgentRole.planner: [_plan_decision(["job-discovery"])],
        AgentRole.executor: [
            {"action": "call_tool", "tool_name": "fetch-wechat-article", "tool_input": {"url": "https://mp.weixin.qq.com/s/x"}},
            {"action": "complete", "summary": "页面被限制"},
        ],
        AgentRole.verifier: [
            {"action": "decide", "verification_decision": "RETRY_EXECUTOR", "feedback": "缺少页面证据"},
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

    # The very first RETRY over blocked evidence downgrades to a human
    # hand-off; the replan path is never reached.
    assert result.status is RunStatus.waiting_user
    assert _events(db_session, result.run_id).count("verification_retry_downgraded") == 1
    assert _events(db_session, result.run_id).count("verification_replan") == 0
    assert _events(db_session, result.run_id).count("plan_created") == 1


# ---------------------------------------------------------------------------
# N4 - isomorphic-replan guard (budget-boundary gated)
# ---------------------------------------------------------------------------


def test_isomorphic_replan_at_budget_boundary_terminates_honestly(db_session) -> None:
    """A fully repeated plan at the last replan stops instead of re-executing."""
    user = _user("user-c-4")
    db_session.add(user)
    db_session.commit()
    gateway = RoleScriptedGateway({
        AgentRole.planner: [
            _plan_decision(["job-matching"]),
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
            {"action": "decide", "verification_decision": "REPLAN", "feedback": "来源改版，需要重新规划提取路径。"},
            {"action": "decide", "verification_decision": "REPLAN", "feedback": "来源改版，需要重新规划提取路径。"},
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
                max_agent_turns=16, max_tool_calls=8, max_replans=2
            ),
        ),
    )

    # Replan #2 exhausts the budget (replans=2 >= max) and the planner again
    # returned the identical step sequence: the guard terminates honestly as
    # waiting_user instead of executing the repeated structure a third time.
    assert result.status is RunStatus.waiting_user
    assert result.summary == "来源改版，需要重新规划提取路径。"
    events = _events(db_session, result.run_id)
    assert events.count("plan_created") == 2  # the third plan was never persisted
    assert events.count("replan_isomorphic_guard") == 1
    # The executor ran exactly two invocations (call + complete each); the
    # repeated third plan was never executed.
    assert len(gateway.states[AgentRole.executor]) == 4


def test_heterogeneous_replan_still_executes(db_session) -> None:
    """A structurally different replan always executes (guard never fires)."""
    user = _user("user-c-5")
    db_session.add(user)
    db_session.commit()
    gateway = RoleScriptedGateway({
        AgentRole.planner: [
            _plan_decision(["job-matching"]),
            _plan_decision(["job-discovery"]),
        ],
        AgentRole.executor: [
            {"action": "call_tool", "tool_name": "match-observed-jobs", "tool_input": {}},
            {"action": "complete", "summary": "匹配完成"},
            {"action": "call_tool", "tool_name": "fetch-public-job-pages", "tool_input": {"urls": ["https://jobs.example/1"]}},
            {"action": "complete", "summary": "抓取完成"},
        ],
        AgentRole.verifier: [
            {"action": "decide", "verification_decision": "REPLAN", "feedback": "来源改版，需要重新规划提取路径。"},
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
            allowed_skills=["job-matching", "job-discovery"],
            budget=AgentBudget(
                max_agent_turns=8, max_tool_calls=8, max_replans=2
            ),
        ),
    )

    assert result.status is RunStatus.succeeded
    assert result.summary == "抓取完成"
    assert _events(db_session, result.run_id).count("plan_created") == 2
    assert _events(db_session, result.run_id).count("replan_isomorphic_guard") == 0
    assert len(gateway.states[AgentRole.executor]) == 4


def test_isomorphic_guard_stays_off_at_budget_one(db_session) -> None:
    """With max_replans=1 the identical replan still executes (loop is bounded)."""
    user = _user("user-c-6")
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
            {"action": "decide", "verification_decision": "REPLAN", "feedback": "来源改版，需要重新规划提取路径。"},
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
                max_agent_turns=8, max_tool_calls=8, max_replans=1
            ),
        ),
    )

    assert result.status is RunStatus.succeeded
    assert result.summary == "匹配完成"
    assert _events(db_session, result.run_id).count("plan_created") == 2
    assert _events(db_session, result.run_id).count("replan_isomorphic_guard") == 0


# ---------------------------------------------------------------------------
# W3 - verifier feedback step-scoped filtering
# ---------------------------------------------------------------------------


def test_feedback_filter_drops_out_of_scope_tool_fragments() -> None:
    """Fragments naming a scoped-out tool are dropped; in-domain content stays."""
    scoped_out = frozenset({"build-resume-tailoring-brief", "match-observed-jobs"})
    filtered = _scope_feedback_to_step_catalog(
        [
            "请调用 build-resume-tailoring-brief 完成定制。请先确认简历事实。",
            "来源改版，需要重新规划提取路径。",
        ],
        scoped_out_tool_names=scoped_out,
    )
    # Fragments are re-joined with "。", so a trailing boundary is normalized away.
    assert filtered == ["请先确认简历事实", "来源改版，需要重新规划提取路径"]


def test_feedback_filter_keeps_in_scope_tool_fragments() -> None:
    """A tool inside the step's catalog is not a filtered fragment."""
    scoped_out = frozenset({"build-resume-tailoring-brief"})
    # A single string (non-list) passes through untouched on the safety path.
    assert (
        _scope_feedback_to_step_catalog(
            "请调用 match-observed-jobs 完成匹配。", scoped_out_tool_names=scoped_out
        )
        == "请调用 match-observed-jobs 完成匹配。"
    )
    # A list entry is re-joined, normalizing the trailing boundary.
    assert (
        _scope_feedback_to_step_catalog(
            ["请调用 match-observed-jobs 完成匹配。"], scoped_out_tool_names=scoped_out
        )
        == ["请调用 match-observed-jobs 完成匹配"]
    )


def test_feedback_filter_drops_fully_tool_naming_entries() -> None:
    """An entry whose only content names a scoped-out tool is dropped entirely."""
    scoped_out = frozenset({"build-resume-tailoring-brief"})
    filtered = _scope_feedback_to_step_catalog(
        ["请调用 build-resume-tailoring-brief 生成定制建议"],
        scoped_out_tool_names=scoped_out,
    )
    assert filtered == []


def test_feedback_filter_passes_through_non_list_and_non_string() -> None:
    """Malformed or non-string feedback never crashes the projection."""
    scoped_out = frozenset({"build-resume-tailoring-brief"})
    assert _scope_feedback_to_step_catalog("字符串", scoped_out_tool_names=scoped_out) == "字符串"
    assert _scope_feedback_to_step_catalog(
        ["保留", 42, None], scoped_out_tool_names=scoped_out
    ) == ["保留", 42, None]
    assert _scope_feedback_to_step_catalog(
        ["保留"], scoped_out_tool_names=frozenset()
    ) == ["保留"]


def test_feedback_filter_logs_drops(caplog) -> None:
    """Each filtered fragment is observable under the verifier_feedback_tool_filtered token."""
    with caplog.at_level(
        logging.WARNING, logger="backend.app.services.agent_runtime.executor_agent"
    ):
        _scope_feedback_to_step_catalog(
            ["请调用 build-resume-tailoring-brief 完成定制。请确认匹配结果。"],
            scoped_out_tool_names=frozenset({"build-resume-tailoring-brief"}),
        )
    assert "verifier_feedback_tool_filtered" in caplog.text


def test_executor_state_receives_scoped_feedback(db_session) -> None:
    """The Executor decision state shows the scoped projection, not stored feedback."""
    user = _user("user-c-7")
    db_session.add(user)
    db_session.commit()
    gateway = RoleScriptedGateway({
        AgentRole.planner: [_plan_decision(["job-matching"])],
        AgentRole.executor: [
            {"action": "call_tool", "tool_name": "match-observed-jobs", "tool_input": {}},
            {"action": "complete", "summary": "匹配完成"},
        ],
        AgentRole.verifier: [
            {"action": "decide", "verification_decision": "PASS"},
        ],
    })
    registry = ToolRegistry()
    _register_match_tool(registry)
    _register_tailoring_tool(registry)

    result = _runtime_for_gateway(gateway, registry).run(
        db_session,
        user_id=user.id,
        task=AgentTaskRequest(
            goal="匹配岗位",
            allowed_skills=["job-matching"],
            context={
                "verifier_feedback": [
                    "请调用 build-resume-tailoring-brief 完成定制。请确认匹配结果。"
                ]
            },
            budget=AgentBudget(
                max_agent_turns=8, max_tool_calls=8, max_replans=2
            ),
        ),
    )

    assert result.status is RunStatus.succeeded
    executor_state = gateway.states[AgentRole.executor][0]
    assert executor_state["verifier_feedback"] == ["请确认匹配结果"]


# ---------------------------------------------------------------------------
# W2 - soft-404 dead links
# ---------------------------------------------------------------------------


def _no_op_public_url(url: str) -> None:
    return None


def _stub_requests(text: str, *, title: str | None = None) -> SimpleNamespace:
    if title is not None:
        html = f"<html><title>{title}</title><body>{text}</body></html>"
    else:
        html = text
    return SimpleNamespace(
        text=html,
        encoding="utf-8",
        apparent_encoding="utf-8",
        raise_for_status=lambda: None,
        is_redirect=False,
    )


def test_fetch_classifies_body_marker_soft_404_as_dead_link(monkeypatch) -> None:
    """A short page carrying 页面不存在 is a dead link, not content-insufficient."""
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery._assert_public_url",
        _no_op_public_url,
    )
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery.requests.get",
        lambda *args, **kwargs: _stub_requests("页面不存在"),
    )
    with pytest.raises(PublicJobFetchError) as excinfo:
        fetch_public_job_page(
            ToolContext(user_id="u", run_id="r"),
            FetchPublicJobPageInput(url="https://jobs.example/offline"),
        )
    assert excinfo.value.code == "dead_link"


def test_fetch_classifies_title_404_as_dead_link(monkeypatch) -> None:
    """A title carrying 404 with almost no body is a dead link."""
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery._assert_public_url",
        _no_op_public_url,
    )
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery.requests.get",
        lambda *args, **kwargs: _stub_requests("Not Found", title="404"),
    )
    with pytest.raises(PublicJobFetchError) as excinfo:
        fetch_public_job_page(
            ToolContext(user_id="u", run_id="r"),
            FetchPublicJobPageInput(url="https://jobs.example/gone"),
        )
    assert excinfo.value.code == "dead_link"


def test_fetch_classifies_title_marker_soft_404_as_dead_link(monkeypatch) -> None:
    """A title carrying 职位已下线 with a shell body is a dead link."""
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery._assert_public_url",
        _no_op_public_url,
    )
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery.requests.get",
        lambda *args, **kwargs: _stub_requests("该职位已关闭", title="职位已下线 - 腾讯招聘"),
    )
    with pytest.raises(PublicJobFetchError) as excinfo:
        fetch_public_job_page(
            ToolContext(user_id="u", run_id="r"),
            FetchPublicJobPageInput(url="https://jobs.example/closed"),
        )
    assert excinfo.value.code == "dead_link"


def test_fetch_ignores_soft_404_markers_with_real_content(monkeypatch) -> None:
    """A real JD body overrides incidental marker strings (never a dead link)."""
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery._assert_public_url",
        _no_op_public_url,
    )
    jd_text = (
        "岗位职责：负责大模型应用开发与部署，要求熟悉 Python 与 LangChain。"
        "任职要求：三年以上相关经验，具备良好的沟通能力。"
    )
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery.requests.get",
        lambda *args, **kwargs: _stub_requests(
            "职位已下线。\n" + jd_text * 10, title="AI 应用开发"
        ),
    )
    result = fetch_public_job_page(
        ToolContext(user_id="u", run_id="r"),
        FetchPublicJobPageInput(url="https://jobs.example/real"),
    )
    assert result.visible_text and "职位已下线" in result.visible_text
    assert result.source_url == "https://jobs.example/real"


def test_dead_link_is_not_a_blocked_error() -> None:
    """dead_link is a neutral failure: it never enters needs_manual_review."""
    assert not is_blocked_error("dead_link")


# ---------------------------------------------------------------------------
# W2 - search authorization (only after EVERY candidate URL failed)
# ---------------------------------------------------------------------------

_CANDIDATE_URLS = ["https://a.example/job", "https://b.example/job"]


def _search_gateway() -> RoleScriptedGateway:
    return RoleScriptedGateway({
        AgentRole.planner: [_plan_decision(["job-discovery"])],
        AgentRole.executor: [
            {
                "action": "call_tool",
                "tool_name": "fetch-public-job-pages",
                "tool_input": {"urls": _CANDIDATE_URLS},
            },
            {
                "action": "call_tool",
                "tool_name": "search-public-job-pages",
                "tool_input": {},
            },
            {"action": "complete", "summary": "处理完成"},
        ],
        AgentRole.verifier: [
            {"action": "decide", "verification_decision": "PASS"},
        ],
    })


def _run_search_case(
    db_session,
    gateway: RoleScriptedGateway,
    *,
    failures: list[dict[str, str]],
    user_id: str,
) -> AgentRuntime:
    registry = ToolRegistry()
    _register_batch_fetch(registry, pages=[], failures=failures)
    _register_search_tool(registry)
    return _runtime_for_gateway(gateway, registry).run(
        db_session,
        user_id=user_id,
        task=AgentTaskRequest(
            goal="找岗位",
            allowed_skills=["job-discovery"],
            context={"candidate_urls": _CANDIDATE_URLS},
            budget=AgentBudget(
                max_agent_turns=8, max_tool_calls=8, max_replans=2
            ),
        ),
    )


def _search_observations(gateway: RoleScriptedGateway) -> list[dict[str, object]]:
    """Observations as seen by the LAST executor decision (all recorded)."""
    return gateway.states[AgentRole.executor][-1]["observations"]


def test_search_stays_forbidden_while_any_candidate_unfailed(db_session) -> None:
    """A partial failure never authorizes search (one candidate remains usable)."""
    user = _user("user-c-8")
    db_session.add(user)
    db_session.commit()
    gateway = _search_gateway()
    result = _run_search_case(
        db_session,
        gateway,
        failures=[{"source_url": "https://a.example/job", "error_code": "public_fetch_failed"}],
        user_id=user.id,
    )

    assert result.status is RunStatus.succeeded
    observations = _search_observations(gateway)
    search_obs = [obs for obs in observations if obs["tool_name"] == "search-public-job-pages"]
    assert len(search_obs) == 1
    assert search_obs[0]["error_code"] == "candidate_urls_already_supplied"


def test_search_stays_forbidden_when_candidates_blocked(db_session) -> None:
    """Blocked candidates never authorize search (security hard gate)."""
    user = _user("user-c-9")
    db_session.add(user)
    db_session.commit()
    gateway = _search_gateway()
    result = _run_search_case(
        db_session,
        gateway,
        failures=[
            {"source_url": "https://a.example/job", "error_code": "login_required"},
            {"source_url": "https://b.example/job", "error_code": "captcha"},
        ],
        user_id=user.id,
    )

    assert result.status is RunStatus.succeeded
    observations = _search_observations(gateway)
    search_obs = [obs for obs in observations if obs["tool_name"] == "search-public-job-pages"]
    assert len(search_obs) == 1
    assert search_obs[0]["error_code"] == "candidate_urls_already_supplied"


def test_search_authorized_after_all_candidates_failed(db_session) -> None:
    """Every candidate failed (fetch error + dead link): search is authorized."""
    user = _user("user-c-10")
    db_session.add(user)
    db_session.commit()
    gateway = _search_gateway()
    result = _run_search_case(
        db_session,
        gateway,
        failures=[
            {"source_url": "https://a.example/job", "error_code": "public_fetch_failed"},
            {"source_url": "https://b.example/job", "error_code": "dead_link"},
        ],
        user_id=user.id,
    )

    assert result.status is RunStatus.succeeded
    observations = _search_observations(gateway)
    search_obs = [obs for obs in observations if obs["tool_name"] == "search-public-job-pages"]
    assert len(search_obs) == 1
    assert search_obs[0]["status"] == "succeeded"


def test_search_without_candidates_is_authorized(db_session) -> None:
    """No candidate URLs at all: search keeps today's allowed behavior."""
    user = _user("user-c-11")
    db_session.add(user)
    db_session.commit()
    gateway = RoleScriptedGateway({
        AgentRole.planner: [_plan_decision(["job-discovery"])],
        AgentRole.executor: [
            {
                "action": "call_tool",
                "tool_name": "search-public-job-pages",
                "tool_input": {},
            },
            {"action": "complete", "summary": "搜索完成"},
        ],
        AgentRole.verifier: [
            {"action": "decide", "verification_decision": "PASS"},
        ],
    })
    registry = ToolRegistry()
    _register_search_tool(registry)

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

    assert result.status is RunStatus.succeeded
    observations = _search_observations(gateway)
    search_obs = [obs for obs in observations if obs["tool_name"] == "search-public-job-pages"]
    assert len(search_obs) == 1
    assert search_obs[0]["status"] == "succeeded"
