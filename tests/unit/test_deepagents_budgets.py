from __future__ import annotations

import pytest

from backend.app.services.agent_runtime.schemas import AgentBudget
from backend.app.services.deepagents_runtime.budgets import DeepAgentsBudgets
from tests.conftest import settings_override


def test_from_settings_maps_harness_limits() -> None:
    settings = settings_override(
        agent_harness_max_agent_turns=7,
        agent_harness_max_tool_calls=9,
        agent_harness_max_replans=3,
        agent_harness_max_wall_clock_seconds=120,
    )
    budgets = DeepAgentsBudgets.from_settings(settings)
    assert budgets.max_agent_turns == 7
    assert budgets.max_tool_calls == 9
    assert budgets.max_replans == 3
    assert budgets.max_wall_clock_seconds == 120
    assert budgets.turns_used == 0


def test_from_agent_budget_maps_request_budget() -> None:
    request_budget = AgentBudget(
        max_agent_turns=5, max_tool_calls=10, max_replans=1, max_wall_clock_seconds=60
    )
    budgets = DeepAgentsBudgets.from_agent_budget(request_budget)
    assert budgets.max_agent_turns == 5
    assert budgets.max_replans == 1


def test_turn_and_tool_budgets_are_hard_ceilings() -> None:
    budgets = DeepAgentsBudgets(
        max_agent_turns=2, max_tool_calls=1, max_replans=1, max_wall_clock_seconds=60
    )
    assert budgets.try_consume_turn()
    assert budgets.try_consume_turn()
    assert not budgets.try_consume_turn()
    assert budgets.try_consume_tool()
    assert not budgets.try_consume_tool()


def test_replan_budget_exhausts() -> None:
    budgets = DeepAgentsBudgets(
        max_agent_turns=10, max_tool_calls=10, max_replans=1, max_wall_clock_seconds=60
    )
    assert budgets.try_consume_replan()
    assert not budgets.try_consume_replan()


def test_wall_clock_window_refreshes_on_resume() -> None:
    budgets = DeepAgentsBudgets(
        max_agent_turns=10, max_tool_calls=10, max_replans=1, max_wall_clock_seconds=60
    )
    budgets.start_window()
    assert budgets.elapsed_seconds() < 60
    assert not budgets.window_exhausted()
    budgets._window_started_at = 0.0  # simulate an ancient start => elapsed ~ infinity
    assert budgets.window_exhausted()
    budgets.refresh_window()
    assert not budgets.window_exhausted()


def test_start_window_is_idempotent() -> None:
    budgets = DeepAgentsBudgets(
        max_agent_turns=10, max_tool_calls=10, max_replans=1, max_wall_clock_seconds=60
    )
    budgets.start_window()
    anchor = budgets._window_started_at
    budgets.start_window()  # already started -> anchor must not move
    assert budgets._window_started_at == anchor


def test_dict_roundtrip_preserves_counters_and_window() -> None:
    budgets = DeepAgentsBudgets(
        max_agent_turns=10, max_tool_calls=10, max_replans=1, max_wall_clock_seconds=60
    )
    budgets.try_consume_turn()
    budgets.try_consume_tool()
    payload = budgets.to_dict()
    restored = DeepAgentsBudgets.from_dict(payload)
    assert restored.turns_used == 1
    assert restored.tool_calls_used == 1
    assert restored.to_dict() == payload
    assert restored.elapsed_seconds() == 0.0  # window never started -> 0.0 branch


def test_dict_roundtrip_preserves_started_window_anchor() -> None:
    budgets = DeepAgentsBudgets(
        max_agent_turns=10, max_tool_calls=10, max_replans=1, max_wall_clock_seconds=60
    )
    budgets.start_window()
    payload = budgets.to_dict()
    restored = DeepAgentsBudgets.from_dict(payload)
    assert restored.to_dict() == payload
    assert restored.window_exhausted() is False


def test_non_positive_maximum_rejected() -> None:
    with pytest.raises(ValueError):
        DeepAgentsBudgets(
            max_agent_turns=0, max_tool_calls=1, max_replans=1, max_wall_clock_seconds=60
        )
