"""One hard, shared cap for tool invocations in a PEV run."""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock


@dataclass
class ToolCallBudget:
    """Mutable run-local allowance shared by Planner, Executor and Verifier."""

    maximum: int
    used: int = 0
    _lock: Lock = field(default_factory=Lock, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.maximum < 1:
            raise ValueError("maximum tool calls must be positive")

    @property
    def remaining(self) -> int:
        """Return the number of additional Agent-selected tools that may run."""
        with self._lock:
            return self.maximum - self.used

    def try_consume(self, *, reserve: int = 0) -> bool:
        """Atomically reserve one invocation while optionally retaining a tail."""
        if reserve < 0:
            raise ValueError("reserved tool calls must not be negative")
        with self._lock:
            if self.used >= self.maximum - reserve:
                return False
            self.used += 1
            return True
