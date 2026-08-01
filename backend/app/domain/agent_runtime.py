"""Pure lifecycle contracts for the adaptive Planner–Executor–Verifier runtime.

The runtime orchestrates three autonomous agents, but the persistence lifecycle
is deliberately deterministic.  This module contains no database, HTTP, model
or tool imports so services and repositories can share the same guardrails.
"""

from __future__ import annotations

import sys

if sys.version_info >= (3, 11):
    from enum import StrEnum
else:  # pragma: no cover - Python 3.12 is the supported runtime.
    from enum import Enum

    class StrEnum(str, Enum):  # pragma: no cover
        """Minimal StrEnum compatibility for older local tooling."""

        pass


class AgentRole(StrEnum):
    """The three autonomous roles in every PEV run."""

    planner = "planner"
    executor = "executor"
    verifier = "verifier"


class ComplexityLevel(StrEnum):
    """Planner-selected operating level, from a one-step plan to a deep loop."""

    L1 = "L1"
    L2 = "L2"
    L3 = "L3"
    L4 = "L4"


class RunStatus(StrEnum):
    """Persistent lifecycle for a user-scoped Agent run."""

    queued = "queued"
    running = "running"
    waiting_user = "waiting_user"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"


class StepStatus(StrEnum):
    """Lifecycle of a planned outcome while Executor works on it."""

    planned = "planned"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    skipped = "skipped"


class VerificationDecision(StrEnum):
    """Verifier outcomes consumed by the harness, never guessed by it."""

    PASS = "PASS"
    RETRY_EXECUTOR = "RETRY_EXECUTOR"
    REPLAN = "REPLAN"
    NEED_USER = "NEED_USER"
    FAIL = "FAIL"


TERMINAL_RUN_STATUSES = frozenset(
    {RunStatus.succeeded, RunStatus.failed, RunStatus.cancelled}
)

_ALLOWED_RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.queued: frozenset({RunStatus.running, RunStatus.cancelled}),
    RunStatus.running: frozenset(
        {
            RunStatus.waiting_user,
            RunStatus.succeeded,
            RunStatus.failed,
            RunStatus.cancelled,
        }
    ),
    RunStatus.waiting_user: frozenset(
        {RunStatus.running, RunStatus.failed, RunStatus.cancelled}
    ),
    RunStatus.succeeded: frozenset(),
    RunStatus.failed: frozenset(),
    RunStatus.cancelled: frozenset(),
}


def is_terminal_run(status: RunStatus) -> bool:
    """Return whether a run is immutable and cannot be resumed."""
    return status in TERMINAL_RUN_STATUSES


def can_transition_run(current: RunStatus, target: RunStatus) -> bool:
    """Return whether the persistent lifecycle permits ``current -> target``."""
    return target in _ALLOWED_RUN_TRANSITIONS[current]


def require_valid_run_transition(current: RunStatus, target: RunStatus) -> None:
    """Raise a stable error before a service attempts an invalid transition."""
    if is_terminal_run(current):
        raise ValueError(f"terminal Agent run cannot transition from {current.value}")
    if not can_transition_run(current, target):
        raise ValueError(
            f"invalid Agent run transition: {current.value} -> {target.value}"
        )
