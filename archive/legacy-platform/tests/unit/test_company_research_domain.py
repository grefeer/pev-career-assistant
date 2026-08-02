"""Unit tests for company-research domain rules (transitions, terminals)."""

from __future__ import annotations

from backend.app.domain.company_research import (
    CompanyResearchStatus,
    is_terminal,
    is_valid_transition,
)


def test_queued_can_run_or_cancel() -> None:
    assert is_valid_transition(
        CompanyResearchStatus.queued, CompanyResearchStatus.running
    )
    assert is_valid_transition(
        CompanyResearchStatus.queued, CompanyResearchStatus.cancelled
    )


def test_running_can_resolve_to_any_terminal() -> None:
    for target in (
        CompanyResearchStatus.succeeded,
        CompanyResearchStatus.needs_manual_review,
        CompanyResearchStatus.failed,
        CompanyResearchStatus.cancelled,
    ):
        assert is_valid_transition(CompanyResearchStatus.running, target)


def test_invalid_transitions_rejected() -> None:
    # Cannot jump queued straight to succeeded (must run first).
    assert not is_valid_transition(
        CompanyResearchStatus.queued, CompanyResearchStatus.succeeded
    )
    # Cannot rewind a terminal state.
    assert not is_valid_transition(
        CompanyResearchStatus.succeeded, CompanyResearchStatus.running
    )
    assert not is_valid_transition(
        CompanyResearchStatus.failed, CompanyResearchStatus.queued
    )
    # Running cannot return to queued.
    assert not is_valid_transition(
        CompanyResearchStatus.running, CompanyResearchStatus.queued
    )


def test_terminal_detection() -> None:
    for status in (
        CompanyResearchStatus.succeeded,
        CompanyResearchStatus.needs_manual_review,
        CompanyResearchStatus.failed,
        CompanyResearchStatus.cancelled,
    ):
        assert is_terminal(status)
    assert not is_terminal(CompanyResearchStatus.queued)
    assert not is_terminal(CompanyResearchStatus.running)
