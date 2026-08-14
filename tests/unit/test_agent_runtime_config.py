"""Settings contracts for bounded, opt-in adaptive PEV execution."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tests.conftest import settings_override
from backend.app.services.agent_runtime.tool_budget import ToolCallBudget
from backend.app.services.agent_runtime.turn_budget import AgentTurnBudget


def test_harness_is_the_default_personal_assistant_path_and_has_positive_budgets() -> None:
    """A new personal-assistant deployment defaults to its only PEV runtime path."""
    settings = settings_override()

    assert settings.agent_harness_enabled is True
    assert settings.agent_harness_max_agent_turns >= 1
    assert settings.agent_harness_max_tool_calls >= 1
    assert settings.agent_harness_max_replans >= 0
    assert settings.agent_harness_max_wall_clock_seconds >= 10


def test_harness_rejects_an_unexecutable_turn_budget() -> None:
    """A configuration error cannot reduce real Agents to zero-turn placeholders."""
    with pytest.raises(ValidationError):
        settings_override(agent_harness_max_agent_turns=0)


@pytest.mark.parametrize("budget_type", [ToolCallBudget, AgentTurnBudget])
def test_shared_budgets_reject_invalid_maximum_and_stop_at_the_exact_cap(budget_type) -> None:
    with pytest.raises(ValueError, match="positive"):
        budget_type(0)
    budget = budget_type(2, used=1)
    assert budget.remaining == 1
    assert budget.try_consume() is True
    assert budget.remaining == 0
    assert budget.try_consume() is False


def test_tool_budget_can_atomically_reserve_one_call_for_runtime_recovery() -> None:
    budget = ToolCallBudget(3)

    assert budget.try_consume(reserve=1) is True
    assert budget.try_consume(reserve=1) is True
    assert budget.try_consume(reserve=1) is False
    assert budget.remaining == 1
    assert budget.try_consume() is True
    assert budget.remaining == 0
