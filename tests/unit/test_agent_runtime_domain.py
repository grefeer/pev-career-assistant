"""Behavioral contracts for the adaptive PEV runtime domain."""

from __future__ import annotations

import pytest

from backend.app.domain.agent_runtime import (
    AgentRole,
    ComplexityLevel,
    RunStatus,
    VerificationDecision,
    can_transition_run,
    require_valid_run_transition,
)


def test_run_can_enter_waiting_user_from_active_execution_only() -> None:
    """A missing user fact pauses an active run but never a completed one."""
    assert can_transition_run(RunStatus.running, RunStatus.waiting_user) is True
    assert can_transition_run(RunStatus.succeeded, RunStatus.waiting_user) is False


def test_terminal_run_cannot_be_reopened() -> None:
    """A cancelled or completed trace remains an immutable audit record."""
    with pytest.raises(ValueError, match="terminal"):
        require_valid_run_transition(RunStatus.cancelled, RunStatus.running)


def test_invalid_non_terminal_transition_is_rejected() -> None:
    """A non-terminal run may only move along its declared lifecycle edges."""
    with pytest.raises(ValueError, match="invalid Agent run transition"):
        require_valid_run_transition(RunStatus.queued, RunStatus.waiting_user)


def test_agent_roles_are_three_distinct_autonomous_roles() -> None:
    """The PEV contract must not collapse a role into an unnamed model node."""
    assert {role.value for role in AgentRole} == {"planner", "executor", "verifier"}


def test_complexity_levels_are_explicit_for_adaptive_budgeting() -> None:
    """Every request is planned even when it is assigned the lowest level."""
    assert [level.value for level in ComplexityLevel] == ["L1", "L2", "L3", "L4"]


def test_verifier_can_request_feedback_or_a_new_plan_without_claiming_success() -> None:
    """A verifier's recovery outcomes must remain machine-actionable."""
    decisions = {decision.value for decision in VerificationDecision}
    assert {"PASS", "RETRY_EXECUTOR", "REPLAN", "NEED_USER", "FAIL"} <= decisions
    assert "PASS" != VerificationDecision.REPLAN.value
