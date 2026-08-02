"""Settings contracts for bounded, opt-in adaptive PEV execution."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tests.conftest import settings_override


def test_harness_is_the_default_personal_assistant_path_and_has_positive_budgets() -> None:
    """A new personal-assistant deployment defaults to its only PEV runtime path."""
    settings = settings_override()

    assert settings.agent_harness_enabled is True
    assert settings.agent_harness_max_agent_turns >= 1
    assert settings.agent_harness_max_tool_calls >= 1
    assert settings.agent_harness_max_replans >= 0


def test_harness_rejects_an_unexecutable_turn_budget() -> None:
    """A configuration error cannot reduce real Agents to zero-turn placeholders."""
    with pytest.raises(ValidationError):
        settings_override(agent_harness_max_agent_turns=0)
