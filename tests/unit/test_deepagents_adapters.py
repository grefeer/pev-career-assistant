from __future__ import annotations

import json

from backend.app.services.agent_runtime.schemas import AgentTaskRequest
from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.career_skills.registry import build_career_tool_registry
from backend.app.services.deepagents_runtime.budgets import DeepAgentsBudgets
from backend.app.services.deepagents_runtime.tools.adapters import (
    DuplicateCallTracker,
    bind_tool_context,
    build_skill_tools,
    tool_context,
)
from tests.unit.deepagents_testkit import ScriptedModel


def _context_factory() -> ToolContext:
    return ToolContext(
        user_id="user-1",
        run_id="run-1",
        metadata={"observed_public_evidence": []},
    )


def _budgets() -> DeepAgentsBudgets:
    return DeepAgentsBudgets(
        max_agent_turns=12, max_tool_calls=24, max_replans=2, max_wall_clock_seconds=300
    )


def test_skill_tools_cover_registry_catalog() -> None:
    registry = build_career_tool_registry()
    catalog = registry.tool_catalog(
        role="executor", allowed_skills=frozenset({"job-discovery"})
    )
    tools = build_skill_tools(
        skill_name="job-discovery",
        context_factory=_context_factory,
        budgets=_budgets(),
        tracker=DuplicateCallTracker(),
    )
    assert {tool.name for tool in tools} == {entry["name"] for entry in catalog}


def test_skill_scoping_excludes_other_skills() -> None:
    tools = build_skill_tools(
        skill_name="job-matching",
        context_factory=_context_factory,
        budgets=_budgets(),
        tracker=DuplicateCallTracker(),
    )
    assert {tool.name for tool in tools} == {"match-observed-jobs"}


def test_build_skill_tools_accepts_explicit_registry() -> None:
    registry = build_career_tool_registry()
    tools = build_skill_tools(
        skill_name="job-discovery",
        context_factory=_context_factory,
        budgets=_budgets(),
        tracker=DuplicateCallTracker(),
        registry=registry,
    )
    catalog = registry.tool_catalog(
        role="executor", allowed_skills=frozenset({"job-discovery"})
    )
    assert {tool.name for tool in tools} == {entry["name"] for entry in catalog}


def test_tool_context_binds_and_resets_per_invocation() -> None:
    ctx = _context_factory()
    with bind_tool_context(ctx):
        assert tool_context() is ctx
    # after the bind exits the getter refuses instead of leaking state
    try:
        tool_context()
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected RuntimeError outside a bound invocation")


def test_adapter_folds_handler_failure_to_observation() -> None:
    tools = build_skill_tools(
        skill_name="job-discovery",
        context_factory=_context_factory,
        budgets=_budgets(),
        tracker=DuplicateCallTracker(),
    )
    by_name = {tool.name: tool for tool in tools}
    result = by_name["extract-observed-job-details"].invoke(
        {"payload": json.dumps({"artifact_id": "missing"})}
    )
    obs = json.loads(result)
    assert obs["status"] == "failed"  # unknown artifact -> failed observation, not crash
    assert obs["error_code"] is not None


def test_tool_budget_exhaustion_returns_observation_not_exception() -> None:
    budgets = _budgets()
    budgets.tool_calls_used = budgets.max_tool_calls
    tools = build_skill_tools(
        skill_name="job-discovery",
        context_factory=_context_factory,
        budgets=budgets,
        tracker=DuplicateCallTracker(),
    )
    result = tools[0].invoke({"payload": "{}"})
    obs = json.loads(result)
    assert obs["status"] == "failed"
    assert obs["error_code"] == "tool_budget_exhausted"


def test_duplicate_consecutive_call_rejected() -> None:
    tracker = DuplicateCallTracker()
    payload = {"artifact_id": "a"}
    assert not tracker.is_duplicate("extract", payload)
    assert tracker.is_duplicate("extract", payload)  # same call again
    assert not tracker.is_duplicate("extract", {"artifact_id": "b"})


def test_handler_folds_consecutive_duplicate_to_observation() -> None:
    tools = build_skill_tools(
        skill_name="job-discovery",
        context_factory=_context_factory,
        budgets=_budgets(),
        tracker=DuplicateCallTracker(),
    )
    payload = json.dumps({"artifact_id": "a1"})
    tools[0].invoke({"payload": payload})  # first call consumes the tracker slot
    result = tools[0].invoke({"payload": payload})  # identical call -> thrash
    obs = json.loads(result)
    assert obs["status"] == "failed"
    assert obs["error_code"] == "duplicate_tool_call"


def test_invalid_json_payload_folded_to_observation() -> None:
    tools = build_skill_tools(
        skill_name="job-discovery",
        context_factory=_context_factory,
        budgets=_budgets(),
        tracker=DuplicateCallTracker(),
    )
    result = tools[0].invoke({"payload": "{not json"})
    obs = json.loads(result)
    assert obs["status"] == "failed"


def test_harness_executor_uses_registry_adapters_by_default() -> None:
    """tool_factory=None -> harness wires build_skill_tools over the real
    registry (covers the default adapter path + bind_tool_context)."""
    from langchain_core.messages import AIMessage

    from backend.app.services.deepagents_runtime.harness import DeepAgentsHarness

    plan_json = json.dumps(
        {
            "task": {
                "goal": "帮我找后端岗位",
                "allowed_skills": ["job-discovery"],
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
                }
            ],
        },
        ensure_ascii=False,
    )
    harness = DeepAgentsHarness(
        # The pinned deepagents stack cannot bind plain FakeListChatModel
        # (bind_tools raises NotImplementedError), so the scripted executor
        # replays via ScriptedModel like every other harness test.
        model_factory=lambda role: ScriptedModel(
            responses={
                "planner": [plan_json],
                "executor": [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "fetch-public-job-pages",
                                "args": {"payload": json.dumps({"urls": []})},
                                "id": "call_1",
                            }
                        ],
                    ),
                    "evidence collected",
                ],
                "verifier": [
                    json.dumps({"decision": "PASS", "rationale": "ok"}, ensure_ascii=False)
                ],
            }[role]
        )
        # tool_factory omitted -> real adapters over the registry
    )
    final = harness.run(
        AgentTaskRequest(
            goal="帮我找后端岗位",
            allowed_skills=["job-discovery"],
            context={"candidate_urls": ["https://example.com/jobs"]},
        ),
        run_id="run-adapters",
    )
    assert final["run_status"] == "succeeded"
    tool_decisions = [d for d in final["decisions"] if d.get("role") == "executor"]
    assert tool_decisions and tool_decisions[0]["tool"] == "fetch-public-job-pages"
