"""One hard, shared cap for tool invocations in a PEV run."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ToolCallBudget:
    """Mutable run-local allowance shared by Planner, Executor and Verifier."""

    maximum: int
    used: int = 0

    def __post_init__(self) -> None:
        if self.maximum < 1:
            raise ValueError("maximum tool calls must be positive")

    @property
    def remaining(self) -> int:
        """Return the number of additional Agent-selected tools that may run."""
        return self.maximum - self.used

    def try_consume(self) -> bool:
        """Reserve one invocation before a ToolRegistry handler can execute."""
        if self.used >= self.maximum:
            return False
        self.used += 1
        return True
