"""Candidate B: smartsheet-bridge resilience for the PEV runtime.

Covers the round-3 gap where ``query-career-sheet-records`` fails with
``sheet_call_failed`` on a Tencent daily quota exhaustion (MCP error 400007
"access limit") and the harness cannot tell "rate limited" from "transient
transport failure":

- rate-limit markers in the mcporter output classify as ``sheet_rate_limited``
  with NO retry and an executor-facing authorized-fallback message;
- transient transport/parse failures retry exactly once before
  ``sheet_call_failed``;
- a retry that succeeds on the second attempt returns records;
- an identical re-issue after a stable failure is deduped as
  ``duplicate_tool_call`` WITHOUT incrementing ``total_wasted_turns`` and
  WITHOUT consuming budget;
- an identical re-issue after a success still dedups as today;
- blocked codes (``login_required``) and transient failures are NOT deduped,
  so legitimate retries and blocked-flow handoffs keep today's behavior.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

from backend.app.domain.agent_runtime import AgentRole, ComplexityLevel
from backend.app.services.agent_runtime.executor.execution_state import (
    input_hash,
    load_stable_failed_calls,
    snapshot_execution_state,
)
from backend.app.services.agent_runtime.executor_agent import ExecutorAgent
from backend.app.services.agent_runtime.skill_definition import SkillRegistry
from tests.unit.deepagents_testkit import DeepGateway, scripted_executor_model
from backend.app.services.agent_runtime.schemas import (
    AgentTaskRequest,
    ExecutionPlan,
    PlanStep,
)
from backend.app.services.agent_runtime.tool_budget import ToolCallBudget
from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.agent_runtime.tool_registry import ToolDefinition, ToolRegistry
from backend.app.services.career_skills import career_sheets
from backend.app.services.career_skills.career_sheets import (
    SheetQueryError,
    _SHEET_RATE_LIMITED_MESSAGE,
    _default_list_records_impl,
)
from backend.app.services.career_skills.registry import build_career_tool_registry


# ---------------------------------------------------------------------------
# bridge error classification + bounded retry
# ---------------------------------------------------------------------------
def test_rate_limit_marker_in_stderr_raises_sheet_rate_limited_without_retry(monkeypatch) -> None:
    calls = {"count": 0}
    sleeps: list[float] = []

    def failing_run(_cmd, **_kwargs) -> SimpleNamespace:
        calls["count"] += 1
        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr='mcporter: MCP error 400007 "access limit" (今日访问限制已达上限)',
        )

    monkeypatch.setattr(career_sheets.shutil, "which", lambda *a: "mcporter")
    monkeypatch.setattr(career_sheets.subprocess, "run", failing_run)
    monkeypatch.setattr(career_sheets.time, "sleep", lambda s: sleeps.append(s))
    with pytest.raises(SheetQueryError) as excinfo:
        _default_list_records_impl("f", "s", 50, 0)
    assert excinfo.value.code == "sheet_rate_limited"
    # Rate limits are NEVER retried: the daily quota will not recover in-run.
    assert calls["count"] == 1
    assert sleeps == []
    # The surfaced message carries the authorized fallback for the executor.
    assert "search-public-job-pages" in str(excinfo.value)


def test_rate_limit_marker_in_stdout_with_unparsable_payload_is_rate_limited(monkeypatch) -> None:
    """A rate-limit marker is detected even when the output is not valid JSON."""
    calls = {"count": 0}

    def failing_run(_cmd, **_kwargs) -> SimpleNamespace:
        calls["count"] += 1
        return SimpleNamespace(returncode=0, stdout="quota exceeded for the day", stderr="")

    monkeypatch.setattr(career_sheets.shutil, "which", lambda *a: "mcporter")
    monkeypatch.setattr(career_sheets.subprocess, "run", failing_run)
    monkeypatch.setattr(career_sheets.time, "sleep", lambda _s: None)
    with pytest.raises(SheetQueryError) as excinfo:
        _default_list_records_impl("f", "s", 50, 0)
    assert excinfo.value.code == "sheet_rate_limited"
    assert calls["count"] == 1


def test_transient_timeout_retries_exactly_once_then_sheet_call_failed(monkeypatch) -> None:
    calls = {"count": 0}
    sleeps: list[float] = []

    def raise_timeout(*_a, **_k):
        calls["count"] += 1
        raise career_sheets.subprocess.TimeoutExpired("mcporter", 30)

    monkeypatch.setattr(career_sheets.shutil, "which", lambda *a: "mcporter")
    monkeypatch.setattr(career_sheets.subprocess, "run", raise_timeout)
    monkeypatch.setattr(career_sheets.time, "sleep", lambda s: sleeps.append(s))
    with pytest.raises(SheetQueryError) as excinfo:
        _default_list_records_impl("f", "s", 50, 0)
    assert excinfo.value.code == "sheet_call_failed"
    # Original attempt + exactly ONE bounded retry, with one backoff sleep.
    assert calls["count"] == 2
    assert sleeps == [1.5]


def test_transient_spawn_failure_retries_exactly_once_then_sheet_call_failed(monkeypatch) -> None:
    calls = {"count": 0}

    def raise_oserror(*_a, **_k):
        calls["count"] += 1
        raise OSError("spawn failed")

    monkeypatch.setattr(career_sheets.shutil, "which", lambda *a: "mcporter")
    monkeypatch.setattr(career_sheets.subprocess, "run", raise_oserror)
    monkeypatch.setattr(career_sheets.time, "sleep", lambda _s: None)
    with pytest.raises(SheetQueryError) as excinfo:
        _default_list_records_impl("f", "s", 50, 0)
    assert excinfo.value.code == "sheet_call_failed"
    assert calls["count"] == 2


def test_plain_bad_exit_code_retries_once_then_sheet_call_failed(monkeypatch) -> None:
    calls = {"count": 0}

    def failing_run(_cmd, **_kwargs) -> SimpleNamespace:
        calls["count"] += 1
        return SimpleNamespace(returncode=3, stdout="traceback...", stderr="")

    monkeypatch.setattr(career_sheets.shutil, "which", lambda *a: "mcporter")
    monkeypatch.setattr(career_sheets.subprocess, "run", failing_run)
    monkeypatch.setattr(career_sheets.time, "sleep", lambda _s: None)
    with pytest.raises(SheetQueryError) as excinfo:
        _default_list_records_impl("f", "s", 50, 0)
    assert excinfo.value.code == "sheet_call_failed"
    assert calls["count"] == 2


def test_transient_failure_retries_and_succeeds_on_second_attempt(monkeypatch) -> None:
    """A transient failure that clears on retry returns the parsed records."""
    calls = {"count": 0}
    sleeps: list[float] = []

    def flaky_run(_cmd, **_kwargs) -> SimpleNamespace:
        calls["count"] += 1
        if calls["count"] == 1:
            return SimpleNamespace(returncode=1, stdout="", stderr="connection reset")
        return SimpleNamespace(
            returncode=0, stdout='{"records": [{"a": 1}], "has_more": false}', stderr=""
        )

    monkeypatch.setattr(career_sheets.shutil, "which", lambda *a: "mcporter")
    monkeypatch.setattr(career_sheets.subprocess, "run", flaky_run)
    monkeypatch.setattr(career_sheets.time, "sleep", lambda s: sleeps.append(s))
    result = _default_list_records_impl("f", "s", 50, 0)
    assert result == {"records": [{"a": 1}], "has_more": False}
    assert calls["count"] == 2
    assert sleeps == [1.5]


# ---------------------------------------------------------------------------
# failure-path fallback authorization (tool description + surfaced message)
# ---------------------------------------------------------------------------
def test_query_sheet_tool_surfaces_rate_limit_with_authorized_fallback(monkeypatch) -> None:
    """The ToolObservation the executor sees names the public-search fallback."""

    def rate_limited(*_args, **_kwargs):
        raise SheetQueryError("sheet_rate_limited", _SHEET_RATE_LIMITED_MESSAGE)

    monkeypatch.setattr(career_sheets, "_list_records_impl", rate_limited)
    observation = build_career_tool_registry().invoke(
        role=AgentRole.executor,
        name="query-career-sheet-records",
        context=ToolContext(user_id="user-a", run_id="run-a"),
        payload={"company_keywords": ["字节"]},
        allowed_skills=frozenset({"job-discovery"}),
    )
    assert observation.error_code == "sheet_rate_limited"
    assert observation.error_message is not None
    assert "search-public-job-pages" in observation.error_message


def test_query_sheet_tool_description_authorizes_search_fallback_on_failure() -> None:
    catalog = build_career_tool_registry().tool_catalog(
        role=AgentRole.executor, allowed_skills=frozenset({"job-discovery"})
    )
    sheet_tool = next(
        tool for tool in catalog if tool["name"] == "query-career-sheet-records"
    )
    # The failure path is explicitly authorized, mirroring the 0-records path.
    assert "sheet_rate_limited" in sheet_tool["description"]
    assert "sheet_call_failed" in sheet_tool["description"]
    assert "search-public-job-pages" in sheet_tool["description"]


# ---------------------------------------------------------------------------
# executor: stable-failure dedup
# ---------------------------------------------------------------------------
class SheetInput(BaseModel):
    query: str


class SheetOutput(BaseModel):
    records: list[dict[str, Any]] = []


class BlockedError(Exception):
    """A blocked-flow failure (login wall) with a stable blocked error code."""

    def __init__(self) -> None:
        super().__init__("blocked")
        self.code = "login_required"


def _discovery_task(**updates) -> AgentTaskRequest:
    return AgentTaskRequest(goal="抓取 JD", allowed_skills=["job-discovery"], **updates)


def _single_step_plan(task: AgentTaskRequest) -> ExecutionPlan:
    return ExecutionPlan(
        task=task,
        created_by=AgentRole.planner,
        complexity=ComplexityLevel.L2,
        success_criteria=["JD"],
        steps=[PlanStep(step_id="discover", objective="抓取", allowed_skills=["job-discovery"])],
    )


def _sheet_registry(*, handler) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="query-sheet",
            skill_name="job-discovery",
            input_model=SheetInput,
            output_model=SheetOutput,
            allowed_roles=frozenset({AgentRole.executor}),
            handler=handler,
        )
    )
    return registry


def _deep_gateway(script: list[dict]) -> DeepGateway:
    return DeepGateway(scripted_executor_model(script))


def test_executor_identical_reissue_after_stable_failure_circuit_breaks() -> None:
    """A repeat of an identical sheet_rate_limited call trips the run-wide
    circuit breaker (the Deep ledger marks the route unavailable), so the
    model cannot burn budget re-issuing the doomed call; the step hands to
    the user with the availability question instead."""
    invocations = {"count": 0}

    def rate_limited_handler(_context, _payload):
        invocations["count"] += 1
        raise SheetQueryError("sheet_rate_limited", _SHEET_RATE_LIMITED_MESSAGE)

    gateway = _deep_gateway(
        [
            {"action": "call_tool", "tool_name": "query-sheet", "tool_input": {"query": "字节"}},
            {"action": "call_tool", "tool_name": "query-sheet", "tool_input": {"query": "字节"}},
            {"action": "complete", "summary": "已切换到公开搜索"},
        ]
    )
    task = _discovery_task()
    result = ExecutorAgent(
        gateway=gateway, tools=_sheet_registry(handler=rate_limited_handler), skills=SkillRegistry()
    ).run(
        task=task,
        plan=_single_step_plan(task),
        step=_single_step_plan(task).steps[0],
        context=ToolContext(user_id="user-a", run_id="run-a"),
        # Only the first real call is budgeted; the duplicate must be deduped
        # without consuming budget, or this run would fail.
        tool_budget=ToolCallBudget(1),
    )

    assert result.status == "needs_user"
    assert result.error_code == "executor_stalled"
    assert invocations["count"] == 1
    assert [obs.error_code for obs in result.observations] == [
        "sheet_rate_limited",
    ]
    # The rate-limit observation itself carries the authorized fallback hint.
    assert "search-public-job-pages" in (result.observations[0].error_message or "")
    # The rate-limited route is persisted run-wide unavailable.
    assert result.execution_state["unavailable_tools"] == ["query-sheet"]


def test_executor_circuit_breaks_different_payloads_after_sheet_outage() -> None:
    """Changing sheet keywords cannot bypass a run-wide quota outage."""
    invocations = {"count": 0}

    def rate_limited_handler(_context, _payload):
        invocations["count"] += 1
        raise SheetQueryError("sheet_rate_limited", _SHEET_RATE_LIMITED_MESSAGE)

    gateway = _deep_gateway(
        [
            {"action": "call_tool", "tool_name": "query-sheet", "tool_input": {"query": "字节"}},
            {"action": "call_tool", "tool_name": "query-sheet", "tool_input": {"query": "腾讯"}},
        ]
    )
    task = _discovery_task()
    result = ExecutorAgent(
        gateway=gateway, tools=_sheet_registry(handler=rate_limited_handler), skills=SkillRegistry()
    ).run(
        task=task,
        plan=_single_step_plan(task),
        step=_single_step_plan(task).steps[0],
        context=ToolContext(user_id="user-a", run_id="run-a"),
        tool_budget=ToolCallBudget(2),
    )

    assert result.status == "needs_user"
    assert result.error_code == "executor_stalled"
    assert invocations["count"] == 1
    assert [obs.error_code for obs in result.observations] == ["sheet_rate_limited"]
    assert result.execution_state["unavailable_tools"] == ["query-sheet"]


def test_executor_identical_reissue_after_success_still_dedups_as_today() -> None:
    """The succeeded-call dedup is unchanged: it still increments the waste cap."""
    invocations = {"count": 0}

    def handler(_context, _payload):
        invocations["count"] += 1
        return {"records": [{"a": 1}]}

    gateway = _deep_gateway(
        [
            {"action": "call_tool", "tool_name": "query-sheet", "tool_input": {"query": "字节"}},
            {"action": "call_tool", "tool_name": "query-sheet", "tool_input": {"query": "字节"}},
            {"action": "complete", "summary": "完成"},
        ]
    )
    task = _discovery_task()
    result = ExecutorAgent(
        gateway=gateway, tools=_sheet_registry(handler=handler), skills=SkillRegistry()
    ).run(
        task=task,
        plan=_single_step_plan(task),
        step=_single_step_plan(task).steps[0],
        context=ToolContext(user_id="user-a", run_id="run-a"),
        tool_budget=ToolCallBudget(1),
    )

    assert result.status == "succeeded"
    assert invocations["count"] == 1
    assert [obs.error_code for obs in result.observations] == [None, "duplicate_tool_call"]
    # A duplicate of a SUCCEEDED call counts as wasted in the Deep ledger too.
    assert result.execution_state["total_wasted_turns"] == 1
    assert result.execution_state["stable_failed_calls"] == []


def test_executor_does_not_dedup_transient_failure_reissue() -> None:
    """tool_execution_failed is transient: an identical re-issue stays legal."""
    invocations = {"count": 0}

    def flaky(_context, _payload):
        invocations["count"] += 1
        if invocations["count"] == 1:
            raise RuntimeError("transient failure")
        return {"records": [{"a": 1}]}

    gateway = _deep_gateway(
        [
            {"action": "call_tool", "tool_name": "query-sheet", "tool_input": {"query": "字节"}},
            {"action": "call_tool", "tool_name": "query-sheet", "tool_input": {"query": "字节"}},
            {"action": "complete", "summary": "重试后成功"},
        ]
    )
    task = _discovery_task()
    result = ExecutorAgent(
        gateway=gateway, tools=_sheet_registry(handler=flaky), skills=SkillRegistry()
    ).run(
        task=task,
        plan=_single_step_plan(task),
        step=_single_step_plan(task).steps[0],
        context=ToolContext(user_id="user-a", run_id="run-a"),
        tool_budget=ToolCallBudget(2),
    )

    assert result.status == "succeeded"
    assert invocations["count"] == 2
    assert [obs.error_code for obs in result.observations] == ["tool_execution_failed", None]
    assert result.execution_state["stable_failed_calls"] == []


def test_executor_does_not_dedup_blocked_code_reissue() -> None:
    """login_required is a blocked flow, not a doomed repeat: no dedup."""
    invocations = {"count": 0}

    def blocked_handler(_context, _payload):
        invocations["count"] += 1
        raise BlockedError()

    gateway = _deep_gateway(
        [
            {"action": "call_tool", "tool_name": "query-sheet", "tool_input": {"query": "字节"}},
            {"action": "call_tool", "tool_name": "query-sheet", "tool_input": {"query": "字节"}},
            {"action": "complete", "summary": "已标记需人工核验"},
        ]
    )
    task = _discovery_task()
    result = ExecutorAgent(
        gateway=gateway, tools=_sheet_registry(handler=blocked_handler), skills=SkillRegistry()
    ).run(
        task=task,
        plan=_single_step_plan(task),
        step=_single_step_plan(task).steps[0],
        context=ToolContext(user_id="user-a", run_id="run-a"),
        tool_budget=ToolCallBudget(2),
    )

    assert result.status == "succeeded"
    assert invocations["count"] == 2
    assert [obs.error_code for obs in result.observations] == ["login_required", "login_required"]
    assert result.execution_state["stable_failed_calls"] == []


def test_executor_hands_repeating_stable_failure_to_user() -> None:
    """Re-issuing a stable-failed call hands the step to the user: the Deep
    ledger's run-wide circuit breaker stops the doomed call immediately."""
    invocations = {"count": 0}

    def rate_limited_handler(_context, _payload):
        invocations["count"] += 1
        raise SheetQueryError("sheet_rate_limited", _SHEET_RATE_LIMITED_MESSAGE)

    gateway = _deep_gateway(
        [
            {"action": "call_tool", "tool_name": "query-sheet", "tool_input": {"query": "字节"}},
            {"action": "call_tool", "tool_name": "query-sheet", "tool_input": {"query": "字节"}},
            {"action": "call_tool", "tool_name": "query-sheet", "tool_input": {"query": "字节"}},
            {"action": "call_tool", "tool_name": "query-sheet", "tool_input": {"query": "字节"}},
        ]
    )
    task = _discovery_task()
    result = ExecutorAgent(
        gateway=gateway, tools=_sheet_registry(handler=rate_limited_handler), skills=SkillRegistry()
    ).run(
        task=task,
        plan=_single_step_plan(task),
        step=_single_step_plan(task).steps[0],
        context=ToolContext(user_id="user-a", run_id="run-a"),
        tool_budget=ToolCallBudget(1),
    )

    assert result.status == "needs_user"
    assert result.error_code == "executor_stalled"
    assert invocations["count"] == 1
    assert [obs.error_code for obs in result.observations] == [
        "sheet_rate_limited",
    ]


def test_executor_dedups_stable_failure_across_invocations() -> None:
    """A stable failure persisted in execution_state dedups in a re-invocation."""
    invocations = {"count": 0}

    def rate_limited_handler(_context, _payload):
        invocations["count"] += 1
        raise SheetQueryError("sheet_rate_limited", _SHEET_RATE_LIMITED_MESSAGE)

    prior_state = snapshot_execution_state(
        succeeded_calls=[],
        prior_succeeded_calls=[],
        consecutive_stalls=0,
        total_wasted_turns=1,
        stable_failed_calls=[("query-sheet", {"query": "字节"})],
        prior_stable_failed_calls=[],
    )
    task = _discovery_task(execution_state=prior_state)
    gateway = _deep_gateway(
        [
            {"action": "call_tool", "tool_name": "query-sheet", "tool_input": {"query": "字节"}},
            {"action": "complete", "summary": "已切换到公开搜索"},
        ]
    )

    result = ExecutorAgent(
        gateway=gateway, tools=_sheet_registry(handler=rate_limited_handler), skills=SkillRegistry()
    ).run(
        task=task,
        plan=_single_step_plan(task),
        step=_single_step_plan(task).steps[0],
        context=ToolContext(user_id="user-a", run_id="run-a"),
        tool_budget=ToolCallBudget(1),
    )

    assert result.status == "succeeded"
    assert invocations["count"] == 0  # never re-hit the doomed call
    assert [obs.error_code for obs in result.observations] == ["duplicate_tool_call"]
    # The carried waste (1) plus the deduped re-issue (1) = 2 in the Deep
    # ledger; the doomed call itself was never re-invoked.
    assert result.execution_state["total_wasted_turns"] == 2




# ---------------------------------------------------------------------------
# stable-failure dedup state persistence
# ---------------------------------------------------------------------------
def test_load_stable_failed_calls_drops_malformed_entries() -> None:
    task = _discovery_task(
        execution_state={
            "stable_failed_calls": [
                "junk",
                {"tool": "ok", "hash": "h" * 64},
                {"tool": 123, "hash": "h" * 64},
                {"hash": "h" * 64},
                {"tool": "empty-hash", "hash": ""},
                {"tool": "no-hash"},
            ],
        }
    )
    assert load_stable_failed_calls(task) == [{"tool": "ok", "hash": "h" * 64}]


def test_snapshot_execution_state_merges_prior_stable_failures_before_current() -> None:
    snapshot = snapshot_execution_state(
        succeeded_calls=[],
        prior_succeeded_calls=[],
        consecutive_stalls=0,
        total_wasted_turns=1,
        stable_failed_calls=[("query-sheet", {"query": "新"})],
        prior_stable_failed_calls=[{"tool": "query-sheet", "hash": "a" * 64}],
    )
    assert snapshot["stable_failed_calls"] == [
        {"tool": "query-sheet", "hash": "a" * 64},
        {"tool": "query-sheet", "hash": input_hash({"query": "新"})},
    ]


def test_snapshot_execution_state_caps_stable_failure_entries_keeping_most_recent() -> None:
    calls = [("query-sheet", {"query": f"q{i}"}) for i in range(45)]
    snapshot = snapshot_execution_state(
        succeeded_calls=[],
        prior_succeeded_calls=[],
        consecutive_stalls=0,
        total_wasted_turns=0,
        stable_failed_calls=calls,
        prior_stable_failed_calls=[],
    )
    entries = snapshot["stable_failed_calls"]
    assert len(entries) == 40
    assert entries[0]["hash"] == input_hash({"query": "q5"})
    assert entries[-1]["hash"] == input_hash({"query": "q44"})