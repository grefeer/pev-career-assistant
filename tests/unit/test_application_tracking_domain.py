"""Unit tests for the application-tracking domain rules (state machine)."""

from __future__ import annotations

import pytest

from backend.app.domain.application_tracking import (
    APPLY_URL_MAX_LENGTH,
    COMPANY_NAME_MAX_LENGTH,
    NOTE_MAX_LENGTH,
    SOURCE_MAX_LENGTH,
    TITLE_MAX_LENGTH,
    ApplicationStatus,
    TERMINAL_STATUSES,
    allowed_transitions,
    is_terminal,
    is_valid_transition,
)


def test_status_values() -> None:
    assert ApplicationStatus.saved.value == "saved"
    assert ApplicationStatus.applied.value == "applied"
    assert ApplicationStatus.screening.value == "screening"
    assert ApplicationStatus.interview.value == "interview"
    assert ApplicationStatus.offer.value == "offer"
    assert ApplicationStatus.rejected.value == "rejected"
    assert ApplicationStatus.withdrawn.value == "withdrawn"


@pytest.mark.parametrize(
    "status,expected_terminal",
    [
        (ApplicationStatus.saved, False),
        (ApplicationStatus.applied, False),
        (ApplicationStatus.screening, False),
        (ApplicationStatus.interview, False),
        (ApplicationStatus.offer, True),
        (ApplicationStatus.rejected, True),
        (ApplicationStatus.withdrawn, True),
    ],
)
def test_is_terminal(status: ApplicationStatus, expected_terminal: bool) -> None:
    assert is_terminal(status) is expected_terminal


def test_terminal_statuses_set() -> None:
    assert TERMINAL_STATUSES == frozenset(
        {
            ApplicationStatus.offer,
            ApplicationStatus.rejected,
            ApplicationStatus.withdrawn,
        }
    )


@pytest.mark.parametrize(
    "from_status,expected",
    [
        (ApplicationStatus.saved, {ApplicationStatus.applied, ApplicationStatus.withdrawn}),
        (
            ApplicationStatus.applied,
            {
                ApplicationStatus.screening,
                ApplicationStatus.rejected,
                ApplicationStatus.withdrawn,
            },
        ),
        (
            ApplicationStatus.screening,
            {
                ApplicationStatus.interview,
                ApplicationStatus.rejected,
                ApplicationStatus.withdrawn,
            },
        ),
        (
            ApplicationStatus.interview,
            {
                ApplicationStatus.offer,
                ApplicationStatus.rejected,
                ApplicationStatus.withdrawn,
            },
        ),
        (ApplicationStatus.offer, {ApplicationStatus.withdrawn}),
        (ApplicationStatus.rejected, set()),
        (ApplicationStatus.withdrawn, set()),
    ],
)
def test_allowed_transitions(
    from_status: ApplicationStatus, expected: set[ApplicationStatus]
) -> None:
    assert set(allowed_transitions(from_status)) == expected


@pytest.mark.parametrize(
    "from_status,to_status,expected",
    [
        # Happy forward path.
        (ApplicationStatus.saved, ApplicationStatus.applied, True),
        (ApplicationStatus.applied, ApplicationStatus.screening, True),
        (ApplicationStatus.screening, ApplicationStatus.interview, True),
        (ApplicationStatus.interview, ApplicationStatus.offer, True),
        # Withdrawn reachable from every non-terminal state + offer.
        (ApplicationStatus.saved, ApplicationStatus.withdrawn, True),
        (ApplicationStatus.applied, ApplicationStatus.withdrawn, True),
        (ApplicationStatus.screening, ApplicationStatus.withdrawn, True),
        (ApplicationStatus.interview, ApplicationStatus.withdrawn, True),
        (ApplicationStatus.offer, ApplicationStatus.withdrawn, True),
        # Rejected reachable from the active pipeline states.
        (ApplicationStatus.applied, ApplicationStatus.rejected, True),
        (ApplicationStatus.screening, ApplicationStatus.rejected, True),
        (ApplicationStatus.interview, ApplicationStatus.rejected, True),
        # Illegal skips.
        (ApplicationStatus.saved, ApplicationStatus.offer, False),
        (ApplicationStatus.saved, ApplicationStatus.interview, False),
        (ApplicationStatus.applied, ApplicationStatus.offer, False),
        # No transitions out of terminal states.
        (ApplicationStatus.offer, ApplicationStatus.applied, False),
        (ApplicationStatus.offer, ApplicationStatus.rejected, False),
        (ApplicationStatus.rejected, ApplicationStatus.applied, False),
        (ApplicationStatus.rejected, ApplicationStatus.withdrawn, False),
        (ApplicationStatus.withdrawn, ApplicationStatus.applied, False),
    ],
)
def test_is_valid_transition(
    from_status: ApplicationStatus, to_status: ApplicationStatus, expected: bool
) -> None:
    assert is_valid_transition(from_status, to_status) is expected


def test_field_length_constants() -> None:
    assert COMPANY_NAME_MAX_LENGTH == 200
    assert TITLE_MAX_LENGTH == 200
    assert APPLY_URL_MAX_LENGTH == 1024
    assert SOURCE_MAX_LENGTH == 64
    assert NOTE_MAX_LENGTH == 2000
