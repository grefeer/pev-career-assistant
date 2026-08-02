"""Unit tests for the interview-prep domain rules."""

from __future__ import annotations

from backend.app.domain.interview_prep import (
    AGENT_VERSION_MAX_LENGTH,
    ERROR_CODE_MAX_LENGTH,
    LAST_ERROR_MAX_LENGTH,
    TERMINAL_STATUSES,
    InterviewPrepKitStatus,
    is_terminal,
)


def test_is_terminal_true_for_ready_and_failed() -> None:
    assert is_terminal(InterviewPrepKitStatus.ready) is True
    assert is_terminal(InterviewPrepKitStatus.failed) is True


def test_is_terminal_false_for_generating() -> None:
    assert is_terminal(InterviewPrepKitStatus.generating) is False


def test_terminal_statuses_contains_ready_and_failed() -> None:
    assert TERMINAL_STATUSES == frozenset(
        {InterviewPrepKitStatus.ready, InterviewPrepKitStatus.failed}
    )


def test_field_length_constants() -> None:
    assert LAST_ERROR_MAX_LENGTH == 2000
    assert ERROR_CODE_MAX_LENGTH == 64
    assert AGENT_VERSION_MAX_LENGTH == 32
