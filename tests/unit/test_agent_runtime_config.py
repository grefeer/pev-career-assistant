"""Settings contracts for bounded, opt-in adaptive PEV execution."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tests.conftest import settings_override


def test_harness_is_explicitly_opt_in_and_has_positive_hard_budgets() -> None:
    """Existing deployments must not silently switch from their legacy runtime."""
    settings = settings_override()

    assert settings.agent_harness_enabled is False
    assert settings.agent_harness_max_agent_turns >= 1
    assert settings.agent_harness_max_tool_calls >= 1
    assert settings.agent_harness_max_replans >= 0


def test_harness_rejects_an_unexecutable_turn_budget() -> None:
    """A configuration error cannot reduce real Agents to zero-turn placeholders."""
    with pytest.raises(ValidationError):
        settings_override(agent_harness_max_agent_turns=0)
