from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from backend.app.services.agent_runtime.schemas import AgentTaskRequest
from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.deepagents_runtime.agents import VerifierDecision
from backend.app.services.deepagents_runtime.budgets import DeepAgentsBudgets
from backend.app.services.deepagents_runtime.harness import (
    DeepAgentsHarness,
    InvalidModelResponseError,
    _extract_structured,
    _is_non_progress,
    _project_tool_observations,
    _sole_skill,
)
from backend.app.services.deepagents_runtime.state import build_initial_state
from backend.app.services.deepagents_runtime.tools.adapters import (
    DuplicateCallTracker,
    bind_tool_context,
    build_skill_tools,
    tool_context,
)
from tests.unit.deepagents_testkit import ScriptedModel

PLAN_JSON = json.dumps(
    {
        "task": {
            "goal": "帮我找后端岗位",
            "allowed_skills": ["job-discovery", "job-matching"],
            "context": {"candidate_urls": ["https://example.com/jobs"]},
            "budget": {
                "max_agent_turns": 12,
                "max_tool_calls": 24,
                "max_replans": 2,
                "max_wall_clock_seconds": 300,
            },
        },
        "created_by": "planner",
        "complexity": "L1",
        "success_criteria": ["找到至少 1 个匹配岗位"],
        "steps": [
            {
                "step_id": "discover",
                "objective": "提取岗位列表",
                "allowed_skills": ["job-discovery"],
                "success_criteria": [],
                "requires_verification": True,
            },
            {
                "step_id": "match",
                "objective": "排序匹配",
                "allowed_skills": ["job-matching"],
                "success_criteria": [],
                "requires_verification": True,
            },
        ],
    },
    ensure_ascii=False,
)
VERIFIER_PASS_JSON = json.dumps(
    {"decision": "PASS", "rationale": "ok"}, ensure_ascii=False
)
VERIFIER_NEED_USER_JSON = json.dumps(
    {"decision": "NEED_USER", "rationale": "需要更多信息"}, ensure_ascii=False
)
REPLAN_JSON = json.dumps({"decision": "REPLAN", "rationale": "insufficient"}, ensure_ascii=False)
RETRY_JSON = json.dumps({"decision": "RETRY_EXECUTOR", "rationale": "补证据"}, ensure_ascii=False)
FAIL_JSON = json.dumps({"decision": "FAIL", "rationale": "不可达"}, ensure_ascii=False)


@tool
def stub_discovery_tool(payload: str) -> str:
    """Test stub: return one piece of tool-produced evidence as observation JSON."""
    return json.dumps(
        {
            "tool_name": "stub",
            "status": "succeeded",
            "output": {
                "source_url": "https://example.com/jobs",
                "content_hash": "abc123",
                "candidates": [{"title": "后端工程师"}],
            },
        }
    )


def _scripted_factory(scripted: dict[str, list[str] | list[AIMessage]]):
    """Return a model_factory consuming one ScriptedModel per role."""

    def factory(role: str) -> ScriptedModel:
        return ScriptedModel(responses=list(scripted[role]))

    return factory


def _request(**overrides) -> AgentTaskRequest:
    values = dict(
        goal="帮我找后端岗位",
        allowed_skills=["job-discovery", "job-matching"],
        context={"candidate_urls": ["https://example.com/jobs"]},
    )
    values.update(overrides)
    return AgentTaskRequest(**values)


def _node_state(**overrides: object) -> dict[str, object]:
    budgets = DeepAgentsBudgets(
        max_agent_turns=12, max_tool_calls=24, max_replans=2, max_wall_clock_seconds=300
    )
    budgets.start_window()
    state = build_initial_state(
        run_id="run-node",
        user_id="user-node",
        goal="g",
        allowed_skills=["job-discovery"],
        context={},
        budgets=budgets,
    )
    state.update(overrides)
    return state


def _exploding_factory(role: str):
    raise AssertionError(f"model must not be called for role {role}")


def test_happy_path_plans_executes_verifies_and_succeeds() -> None:
    harness = DeepAgentsHarness(
        model_factory=_scripted_factory(
            {
                "planner": [PLAN_JSON],
                "executor": [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "stub_discovery_tool",
                                "args": {"payload": "{}"},
                                "id": "call_1",
                            }
                        ],
                    ),
                    "evidence collected",
                ],
                "verifier": [VERIFIER_PASS_JSON],
            }
        ),
        tool_factory=lambda skill: [stub_discovery_tool] if skill == "job-discovery" else [],
    )
    final = harness.run(_request(), run_id="run-1")
    assert final["run_status"] == "succeeded"
    assert final["step_index"] == 2
    assert final["error_code"] is None
    # evidence bound from the tool observation output (source_url + content_hash)
    assert any(item.get("content_hash") == "abc123" for item in final["evidence_store"])
    roles = {d.get("role") for d in final["decisions"]}
    assert {"planner", "executor", "verifier"} <= roles


def test_verifier_replan_consumes_replan_budget_and_degrades_when_exhausted() -> None:
    harness = DeepAgentsHarness(
        model_factory=_scripted_factory(
            {
                "planner": [PLAN_JSON, PLAN_JSON, PLAN_JSON],
                "executor": ["executed"],
                "verifier": [REPLAN_JSON, REPLAN_JSON, REPLAN_JSON],
            }
        ),
        tool_factory=lambda skill: [],
    )
    request = _request()
    request.budget = request.budget.model_copy(update={"max_replans": 1})
    final = harness.run(request, run_id="run-replan")
    assert final["run_status"] == "waiting_user"
    assert final["error_code"] == "max_replans_exceeded"


def test_retry_exhaustion_degrades_to_waiting_user() -> None:
    harness = DeepAgentsHarness(
        model_factory=_scripted_factory(
            {
                "planner": [PLAN_JSON],
                "executor": ["executed", "executed again"],
                "verifier": [RETRY_JSON, RETRY_JSON],
            }
        ),
        tool_factory=lambda skill: [],
    )
    request = _request()
    request.budget = request.budget.model_copy(update={"max_replans": 1})
    final = harness.run(request, run_id="run-retry")
    assert final["run_status"] == "waiting_user"
    assert final["error_code"] == "retries_exceeded"
    assert final["step_index"] == 0  # same step, retried


def test_need_user_degrades_to_waiting_user() -> None:
    harness = DeepAgentsHarness(
        model_factory=_scripted_factory(
            {
                "planner": [PLAN_JSON],
                "executor": ["executed"],
                "verifier": [VERIFIER_NEED_USER_JSON],
            }
        ),
        tool_factory=lambda skill: [],
    )
    final = harness.run(_request(), run_id="run-need-user")
    assert final["run_status"] == "waiting_user"
    assert final["error_code"] == "needs_user"


def test_fail_marks_run_failed() -> None:
    harness = DeepAgentsHarness(
        model_factory=_scripted_factory(
            {
                "planner": [PLAN_JSON],
                "executor": ["executed"],
                "verifier": [FAIL_JSON],
            }
        ),
        tool_factory=lambda skill: [],
    )
    final = harness.run(_request(), run_id="run-fail")
    assert final["run_status"] == "failed"
    assert final["error_code"] == "verification_failed"


def test_wall_clock_exhaustion_degrades_before_planner() -> None:
    budgets = DeepAgentsBudgets(
        max_agent_turns=12, max_tool_calls=24, max_replans=2, max_wall_clock_seconds=300
    )
    budgets._window_started_at = 0.0  # ancient anchor => window exhausted

    harness = DeepAgentsHarness(model_factory=_exploding_factory, tool_factory=lambda skill: [])
    final = harness.run(_request(), run_id="run-wallclock", budgets=budgets)
    assert final["run_status"] == "waiting_user"
    assert final["error_code"] == "wall_clock_budget_exhausted"


def test_stall_breaker_after_three_non_progress_decisions() -> None:
    # Verifier REPLANs keep us on step 0; executor keeps producing no
    # ToolMessage => no progress => stalled counter hits 3 on the 3rd entry.
    harness = DeepAgentsHarness(
        model_factory=_scripted_factory(
            {
                "planner": [PLAN_JSON, PLAN_JSON, PLAN_JSON],
                "executor": ["no progress", "no progress", "no progress"],
                "verifier": [REPLAN_JSON, REPLAN_JSON, REPLAN_JSON],
            }
        ),
        tool_factory=lambda skill: [],
    )
    request = _request()
    request.budget = request.budget.model_copy(update={"max_replans": 3})
    final = harness.run(request, run_id="run-stall")
    assert final["run_status"] == "waiting_user"
    assert final["error_code"] == "stalled_no_progress"


def test_default_tool_factory_path_stalls_cleanly() -> None:
    # tool_factory=None => the tools/adapters build_skill_tools default path
    # wraps the real registry tools; the scripted executor never calls a tool,
    # so no progress is made and the stall breaker hands the run to the human.
    harness = DeepAgentsHarness(
        model_factory=_scripted_factory(
            {
                "planner": [PLAN_JSON, PLAN_JSON, PLAN_JSON],
                "executor": ["no progress", "no progress", "no progress"],
                "verifier": [REPLAN_JSON, REPLAN_JSON, REPLAN_JSON],
            }
        ),
    )
    request = _request()
    request.budget = request.budget.model_copy(update={"max_replans": 3})
    final = harness.run(request, run_id="run-default-adapters")
    assert final["run_status"] == "waiting_user"
    assert final["error_code"] == "stalled_no_progress"


def test_resume_continues_from_checkpoint() -> None:
    verifier_calls = {"n": 0}

    def factory(role: str) -> ScriptedModel:
        if role == "verifier":
            verifier_calls["n"] += 1
            if verifier_calls["n"] == 1:
                return ScriptedModel(responses=[VERIFIER_NEED_USER_JSON])
            return ScriptedModel(responses=[VERIFIER_PASS_JSON])
        if role == "planner":
            return ScriptedModel(responses=[PLAN_JSON, PLAN_JSON])
        return ScriptedModel(responses=["executed", "executed again"])

    harness = DeepAgentsHarness(
        model_factory=factory,
        tool_factory=lambda skill: [],
        checkpointer=InMemorySaver(),
    )
    request = _request()
    request.budget = request.budget.model_copy(update={"max_replans": 2})
    first = harness.run(request, run_id="run-resume")
    assert first["run_status"] == "waiting_user"
    assert first["error_code"] == "needs_user"
    first_decision_count = len(first["decisions"])

    resumed = harness.resume("run-resume")
    assert resumed["run_status"] == "succeeded"
    assert len(resumed["decisions"]) > first_decision_count  # counters never reset


def test_multi_skill_step_degrades_to_invalid_model_response() -> None:
    # A step allowing two skills violates the one-skill-per-step invariant;
    # the harness rejects the plan and degrades instead of crashing.
    multi_skill = json.loads(PLAN_JSON)
    multi_skill["steps"][0]["allowed_skills"] = ["job-discovery", "job-matching"]
    harness = DeepAgentsHarness(
        model_factory=_scripted_factory(
            {
                "planner": [json.dumps(multi_skill, ensure_ascii=False)],
                "executor": ["executed"],
                "verifier": [VERIFIER_PASS_JSON],
            }
        ),
        tool_factory=lambda skill: [],
    )
    final = harness.run(_request(), run_id="run-multi-skill")
    assert final["run_status"] == "waiting_user"
    assert final["error_code"] == "invalid_model_response"


def test_plan_with_forbidden_skill_degrades_to_invalid_model_response() -> None:
    # validate_plan_authority rejects steps using skills outside the run's
    # allowed set (skill-authority invariant).
    forbidden = json.loads(PLAN_JSON)
    forbidden["steps"][0]["allowed_skills"] = ["resume-tailoring"]
    harness = DeepAgentsHarness(
        model_factory=_scripted_factory(
            {
                "planner": [json.dumps(forbidden, ensure_ascii=False)],
                "executor": ["executed"],
                "verifier": [VERIFIER_PASS_JSON],
            }
        ),
        tool_factory=lambda skill: [],
    )
    final = harness.run(_request(), run_id="run-forbidden-skill")
    assert final["run_status"] == "waiting_user"
    assert final["error_code"] == "invalid_model_response"


def test_malformed_planner_output_degrades_to_invalid_model_response() -> None:
    harness = DeepAgentsHarness(
        model_factory=_scripted_factory(
            {
                "planner": ["this is not json at all"],
                "executor": ["executed"],
                "verifier": [VERIFIER_PASS_JSON],
            }
        ),
        tool_factory=lambda skill: [],
    )
    final = harness.run(_request(), run_id="run-malformed-plan")
    assert final["run_status"] == "waiting_user"
    assert final["error_code"] == "invalid_model_response"


def test_malformed_verifier_output_degrades_to_invalid_model_response() -> None:
    harness = DeepAgentsHarness(
        model_factory=_scripted_factory(
            {
                "planner": [PLAN_JSON],
                "executor": ["executed"],
                "verifier": ["this is not json at all"],
            }
        ),
        tool_factory=lambda skill: [],
    )
    final = harness.run(_request(), run_id="run-malformed-verdict")
    assert final["run_status"] == "waiting_user"
    assert final["error_code"] == "invalid_model_response"


def test_turn_budget_exhaustion_degrades_executor() -> None:
    # max_agent_turns=1: the planner's single model call spends the budget,
    # the executor's first model call trips TurnBudgetMiddleware.
    harness = DeepAgentsHarness(
        model_factory=_scripted_factory(
            {
                "planner": [PLAN_JSON],
                "executor": ["executed"],
                "verifier": [VERIFIER_PASS_JSON],
            }
        ),
        tool_factory=lambda skill: [],
    )
    request = _request()
    request.budget = request.budget.model_copy(update={"max_agent_turns": 1})
    final = harness.run(request, run_id="run-turn-executor")
    assert final["run_status"] == "waiting_user"
    assert final["error_code"] == "agent_turn_budget_exhausted"


def test_turn_budget_exhaustion_degrades_verifier() -> None:
    # max_agent_turns=2: planner + executor spend it, the verifier trips.
    harness = DeepAgentsHarness(
        model_factory=_scripted_factory(
            {
                "planner": [PLAN_JSON],
                "executor": ["executed"],
                "verifier": [VERIFIER_PASS_JSON],
            }
        ),
        tool_factory=lambda skill: [],
    )
    request = _request()
    request.budget = request.budget.model_copy(update={"max_agent_turns": 2})
    final = harness.run(request, run_id="run-turn-verifier")
    assert final["run_status"] == "waiting_user"
    assert final["error_code"] == "agent_turn_budget_exhausted"


def test_planner_node_turn_budget_exhaustion_degrades() -> None:
    harness = DeepAgentsHarness(
        model_factory=lambda role: ScriptedModel(responses=["unused"]),
        tool_factory=lambda skill: [],
    )
    budgets = DeepAgentsBudgets(
        max_agent_turns=1, max_tool_calls=24, max_replans=2, max_wall_clock_seconds=300
    )
    budgets.turns_used = 1  # already spent => middleware raises immediately
    state = build_initial_state(
        run_id="r", user_id="u", goal="g", allowed_skills=["job-discovery"],
        context={}, budgets=budgets,
    )
    result = harness._planner_node(state)
    assert result["run_status"] == "waiting_user"
    assert result["error_code"] == "agent_turn_budget_exhausted"


def test_terminal_resume_is_noop_in_planner_node() -> None:
    # Only waiting_user is recoverable; re-entering the planner for a
    # terminal run must not consume model calls or budgets.
    harness = DeepAgentsHarness(model_factory=_exploding_factory, tool_factory=lambda skill: [])
    state = _node_state(run_status="succeeded", error_code=None)
    result = harness._planner_node(state)
    assert "finished_at" in result
    assert "run_status" not in result


def test_executor_window_exhaustion_degrades() -> None:
    harness = DeepAgentsHarness(model_factory=_exploding_factory, tool_factory=lambda skill: [])
    state = _node_state()
    state["budget"]["window_started_at"] = 0.0
    result = harness._executor_node(state)
    assert result["run_status"] == "waiting_user"
    assert result["error_code"] == "wall_clock_budget_exhausted"


def test_verifier_window_exhaustion_degrades() -> None:
    harness = DeepAgentsHarness(model_factory=_exploding_factory, tool_factory=lambda skill: [])
    state = _node_state()
    state["budget"]["window_started_at"] = 0.0
    result = harness._verifier_node(state)
    assert result["run_status"] == "waiting_user"
    assert result["error_code"] == "wall_clock_budget_exhausted"


def test_projection_handles_malformed_and_nested_observations() -> None:
    messages = [
        ToolMessage(content="not json", tool_call_id="t1", name="stub"),
        ToolMessage(
            content=json.dumps(
                {"tool_name": "f", "status": "failed", "error_code": "blocked"},
                ensure_ascii=False,
            ),
            tool_call_id="t2",
            name="stub",
        ),
        ToolMessage(
            content=json.dumps(
                {
                    "tool_name": "s",
                    "status": "succeeded",
                    "output": {
                        "source_url": "https://example.com/jobs",
                        "content_hash": "h1",
                        "candidates": [{"title": "后端工程师"}],
                        "nested": {"source_url": "https://example.com/jobs", "content_hash": "h1"},
                    },
                },
                ensure_ascii=False,
            ),
            tool_call_id="t3",
            name="stub",
        ),
    ]
    decisions, evidence = _project_tool_observations(messages)
    # malformed payload skipped; failed payload yields a decision but no evidence
    assert len(decisions) == 2
    assert decisions[0]["status"] == "failed"
    # only the first dict carrying (source_url, content_hash) becomes evidence;
    # the nested duplicate is deduped, list entries without the keys are skipped
    assert len(evidence) == 1
    assert evidence[0]["content_hash"] == "h1"


def test_is_non_progress_classification() -> None:
    assert not _is_non_progress({"status": "succeeded", "error_code": None})
    assert _is_non_progress({"status": "failed", "error_code": "boom"})
    assert _is_non_progress({"status": "succeeded", "error_code": "blocked"})
    assert _is_non_progress({"status": "succeeded", "error_code": "duplicate_tool_call"})


def test_extract_structured_branches() -> None:
    # pydantic instance passthrough
    decision = VerifierDecision(decision="PASS", rationale="ok")
    assert _extract_structured({"structured_response": decision}, VerifierDecision) is decision
    # raw dict validated into the schema
    parsed = _extract_structured(
        {"structured_response": {"decision": "PASS", "rationale": "ok"}}, VerifierDecision
    )
    assert isinstance(parsed, VerifierDecision)
    assert parsed.decision == "PASS"
    # absent channel degrades via InvalidModelResponseError
    try:
        _extract_structured({}, VerifierDecision)
    except InvalidModelResponseError:
        pass
    else:
        raise AssertionError("expected InvalidModelResponseError")


def test_sole_skill_rejects_multi_skill_step() -> None:
    assert _sole_skill(["job-discovery"]) == "job-discovery"
    try:
        _sole_skill(["job-discovery", "job-matching"])
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for multi-skill step")


def test_finalize_without_decisions_marks_failed() -> None:
    harness = DeepAgentsHarness(model_factory=_exploding_factory, tool_factory=lambda skill: [])
    result = harness._finalize_node({"run_status": None, "decisions": []})
    assert result["run_status"] == "failed"
    assert result["error_code"] == "verification_failed"


def test_duplicate_call_tracker_dedups_consecutive_identical() -> None:
    tracker = DuplicateCallTracker()
    payload = {"artifact_id": "a1"}
    assert not tracker.is_duplicate("extract", payload)
    assert tracker.is_duplicate("extract", payload)  # same call again
    assert not tracker.is_duplicate("extract", {"artifact_id": "a2"})


def test_build_skill_tools_returns_catalog_tools_and_binds_context() -> None:
    # P2 seam: build_skill_tools wraps every registry tool of the skill and
    # the tool_context() getter exposes the ContextVar bound by the harness.
    tools = build_skill_tools(
        skill_name="job-discovery",
        budgets=DeepAgentsBudgets(
            max_agent_turns=12, max_tool_calls=24, max_replans=2, max_wall_clock_seconds=300
        ),
        tracker=DuplicateCallTracker(),
    )
    names = {tool.name for tool in tools}
    assert "fetch-public-job-pages" in names
    assert "extract-observed-job-details" in names
    ctx = ToolContext(user_id="u", run_id="r", metadata={"k": "v"})
    with pytest.raises(RuntimeError):
        tool_context()  # unbound outside an executor invocation
    with bind_tool_context(ctx):
        assert tool_context() is ctx
    with pytest.raises(RuntimeError):
        tool_context()  # reset after the bind exits
