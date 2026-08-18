"""Candidate C (round4): termination logic + dead-link search authorization.

Round-4 behaviors under test:

* N3 retry progress gate - a verifier RETRY_EXECUTOR with an unchanged
  evidence fingerprint is handed to the human before another executor or
  planner loop can spend budget. New evidence may still trigger a bounded
  replan.
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
  only after EVERY candidate URL failed or was blocked. Blocked domains are
  filtered from the alternate-host search results.
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
)
from backend.app.repositories import agent_runtime as run_repository
from backend.app.services.career_skills.manifest import (
    build_career_skill_registry,
    career_error_policy,
)
from backend.app.services.agent_runtime.executor.execution_policy import (
    scope_feedback_to_step_catalog,
)
from backend.app.services.agent_runtime.executor_agent import ExecutorAgent
from backend.app.services.agent_runtime.planner_agent import PlannerAgent
from backend.app.services.agent_runtime.runtime import AgentRuntime
from backend.app.services.agent_runtime.schemas import (
    AgentBudget,
    AgentTaskRequest,
)
from backend.app.services.agent_runtime.skill_definition import SkillRegistry
from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.agent_runtime.tool_registry import ToolDefinition, ToolRegistry
from backend.app.services.career_skills.job_discovery import (
    FetchPublicJobPageInput,
    PublicJobFetchError,
    fetch_public_job_page,
)
from tests.unit.deepagents_testkit import scripted_executor_model

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
        executor=ExecutorAgent(gateway=gateway, tools=registry, skills=SkillRegistry()),

        agent_version="pev-test",
        skills=build_career_skill_registry(registry),
    )


def _steps(db_session, run_id: str) -> list[AgentStep]:
    return list(
        db_session.scalars(select(AgentStep).where(AgentStep.run_id == run_id))
    )


def _events(db_session, run_id: str) -> list[str]:
    return [
        event.event_type for event in run_repository.list_events(db_session, run_id)
    ]


def _register_match_tool_meaningful(registry: ToolRegistry) -> None:
    """Match handler whose report satisfies the deterministic semantic contract."""
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


_SEARCH_INVOCATIONS: list[dict[str, object]] = []


def _register_search_tool(registry: ToolRegistry) -> None:
    def search_handler(_context, _payload) -> dict[str, object]:

        _SEARCH_INVOCATIONS.append({"query": getattr(_payload, "query", None)})
        return {
            "results": [
                {"title": "AI 应用开发", "url": "https://jobs.example/s1"}
            ]
        }

    registry.register(
        ToolDefinition(
            name="search-public-job-pages",
            skill_name="job-discovery",
            input_model=SearchInput,
            output_model=SearchOutput,
            allowed_roles=frozenset({AgentRole.executor}),
            handler=search_handler,
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


def test_gate_retry_loop_hands_off_after_repeated_no_progress(db_session) -> None:
    """The deterministic gate RETRY loop hands off on an unchanged fingerprint."""
    user = _user("user-c-1")
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
    _register_match_tool(registry)

    result = _runtime_for_gateway(gateway, registry).run(
        db_session,
        user_id=user.id,
        task=AgentTaskRequest(
            goal="匹配岗位",
            allowed_skills=["job-matching"],
            budget=AgentBudget(
                max_agent_turns=16, max_tool_calls=8, max_replans=2,
                max_auto_recoveries=0,
            ),
        ),
    )

    # The match report lacks the semantic evidence excerpt, so the gate keeps
    # RETRY_EXECUTOR; the second retry carries the same evidence fingerprint,
    # so the runtime hands off before spending a replan on an unchanged path.
    assert result.status is RunStatus.waiting_user
    assert "未满足确定性交付契约" in (result.summary or "")
    assert _events(db_session, result.run_id).count("plan_created") == 1
    assert _events(db_session, result.run_id).count("verification_replan") == 0
    steps = _steps(db_session, result.run_id)
    assert len(steps) == 1
    assert steps[0].error_code == "no_progress_duplicate"


def test_gate_passes_contract_satisfying_deliverable(db_session) -> None:
    """A semantically meaningful match report makes the gate PASS directly."""
    user = _user("user-c-2")
    db_session.add(user)
    db_session.commit()
    gateway = RoleScriptedGateway({
        AgentRole.planner: [_plan_decision(["job-matching"])],
        AgentRole.executor: [
            {"action": "call_tool", "tool_name": "match-observed-jobs", "tool_input": {}},
            {"action": "complete", "summary": "匹配完成"},
        ],
    })
    registry = ToolRegistry()
    _register_match_tool_meaningful(registry)

    result = _runtime_for_gateway(gateway, registry).run(
        db_session,
        user_id=user.id,
        task=AgentTaskRequest(
            goal="匹配岗位",
            allowed_skills=["job-matching"],
            budget=AgentBudget(
                max_agent_turns=16, max_tool_calls=8, max_replans=2,
                max_auto_recoveries=0,
            ),
        ),
    )

    # The evidence excerpt satisfies the deterministic semantic contract, so
    # the gate closes the step on the first pass without any verifier turn.
    assert result.status is RunStatus.succeeded
    assert _events(db_session, result.run_id).count("plan_created") == 1
    assert _events(db_session, result.run_id).count("verification_passed") == 1


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

    # Blocked evidence makes the gate pause directly with NEED_USER; the
    # replan path is never reached and the human keeps the hand-off.
    assert result.status is RunStatus.waiting_user
    assert _events(db_session, result.run_id).count("verification_retry_downgraded") == 0
    assert _events(db_session, result.run_id).count("verification_replan") == 0
    assert _events(db_session, result.run_id).count("plan_created") == 1


# ---------------------------------------------------------------------------
# W3 - verifier feedback step-scoped filtering
# ---------------------------------------------------------------------------


def test_feedback_filter_drops_out_of_scope_tool_fragments() -> None:
    """Fragments naming a scoped-out tool are dropped; in-domain content stays."""
    scoped_out = frozenset({"build-resume-tailoring-brief", "match-observed-jobs"})
    filtered = scope_feedback_to_step_catalog(
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
        scope_feedback_to_step_catalog(
            "请调用 match-observed-jobs 完成匹配。", scoped_out_tool_names=scoped_out
        )
        == "请调用 match-observed-jobs 完成匹配。"
    )
    # A list entry is re-joined, normalizing the trailing boundary.
    assert (
        scope_feedback_to_step_catalog(
            ["请调用 match-observed-jobs 完成匹配。"], scoped_out_tool_names=scoped_out
        )
        == ["请调用 match-observed-jobs 完成匹配"]
    )


def test_feedback_filter_drops_fully_tool_naming_entries() -> None:
    """An entry whose only content names a scoped-out tool is dropped entirely."""
    scoped_out = frozenset({"build-resume-tailoring-brief"})
    filtered = scope_feedback_to_step_catalog(
        ["请调用 build-resume-tailoring-brief 生成定制建议"],
        scoped_out_tool_names=scoped_out,
    )
    assert filtered == []


def test_feedback_filter_passes_through_non_list_and_non_string() -> None:
    """Malformed or non-string feedback never crashes the projection."""
    scoped_out = frozenset({"build-resume-tailoring-brief"})
    assert scope_feedback_to_step_catalog("字符串", scoped_out_tool_names=scoped_out) == "字符串"
    assert scope_feedback_to_step_catalog(
        ["保留", 42, None], scoped_out_tool_names=scoped_out
    ) == ["保留", 42, None]
    assert scope_feedback_to_step_catalog(
        ["保留"], scoped_out_tool_names=frozenset()
    ) == ["保留"]


def test_feedback_filter_logs_drops(caplog) -> None:
    """Each filtered fragment is observable under the verifier_feedback_tool_filtered token."""
    with caplog.at_level(
        logging.WARNING, logger="backend.app.services.agent_runtime.executor_agent"
    ):
        scope_feedback_to_step_catalog(
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
    _register_match_tool_meaningful(registry)
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
    assert not career_error_policy().is_blocked("dead_link")


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
            {"action": "complete", "summary": "处理完成"},
            {"action": "complete", "summary": "处理完成"},
        ],
    })


def _run_search_case(
    db_session,
    gateway: RoleScriptedGateway,
    *,
    failures: list[dict[str, str]],
    user_id: str,
) -> AgentRuntime:
    _SEARCH_INVOCATIONS.clear()
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
                max_agent_turns=8, max_tool_calls=8, max_replans=2,
                max_auto_recoveries=0,
            ),
        ),
    )


def _search_observations(gateway: RoleScriptedGateway) -> list[dict[str, object]]:
    """The search tool invocations observed on the Deep path."""
    return list(_SEARCH_INVOCATIONS)


def test_search_stays_forbidden_while_any_candidate_unfailed(db_session) -> None:
    """A partial failure never authorizes search (one candidate remains usable).

    The Deep ledger marks a candidate processed as soon as a fetch attempt
    covered it, so this test's fetch must deliberately skip the second
    candidate: only then does the W2 gate keep search forbidden.
    """
    user = _user("user-c-8")
    db_session.add(user)
    db_session.commit()
    gateway = RoleScriptedGateway({
        AgentRole.planner: [_plan_decision(["job-discovery"])],
        AgentRole.executor: [
            {
                "action": "call_tool",
                "tool_name": "fetch-public-job-pages",
                "tool_input": {"urls": ["https://a.example/job"]},
            },
            {"action": "call_tool", "tool_name": "search-public-job-pages", "tool_input": {}},
            {"action": "complete", "summary": "处理完成"},
        ],
        AgentRole.verifier: [
            {"action": "decide", "verification_decision": "PASS"},
        ],
    })
    result = _run_search_case(
        db_session,
        gateway,
        failures=[{"source_url": "https://a.example/job", "error_code": "public_fetch_failed"}],
        user_id=user.id,
    )

    assert result.status is RunStatus.waiting_user
    # The candidate gate rejected the executor's search (payload with no
    # query) before its handler ran; only the runtime's own rescue search
    # (which carries a real query) may have executed.
    assert not any(inv.get("query") is None for inv in _search_observations(gateway))


def test_search_falls_back_when_all_candidates_are_blocked(db_session) -> None:
    """Blocked candidates authorize a safe alternate-host search fallback."""
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

    # Blocked candidates keep the human hand-off (the strict completion gate
    # never closes a step over blocked evidence without a persisted deliverable),
    # while the W2 authorization itself is observable: the fallback search ran
    # and succeeded instead of being rejected as candidate_urls_already_supplied.
    assert result.status is RunStatus.waiting_user
    # W2 authorization is observable: the executor's fallback search
    # (payload without a query) reached the handler.
    assert any(inv.get("query") is None for inv in _search_observations(gateway))


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

    assert result.status is RunStatus.waiting_user
    # Every candidate failed, so the executor's fallback search reached the
    # handler (payload without a query).
    assert any(inv.get("query") is None for inv in _search_observations(gateway))


def test_search_without_candidates_is_authorized(db_session) -> None:
    """No candidate URLs at all: search keeps today's allowed behavior."""
    user = _user("user-c-11")
    db_session.add(user)
    db_session.commit()
    _SEARCH_INVOCATIONS.clear()
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
    # The deterministic recovery may fetch URLs from the search result, so the
    # fetch tool must be registered to avoid an unknown_tool R013 replan.
    _register_batch_fetch(registry, pages=[], failures=[])

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

    assert result.status is RunStatus.waiting_user
    # No candidate URLs: the executor's search reached the handler.
    assert any(inv.get("query") is None for inv in _search_observations(gateway))
