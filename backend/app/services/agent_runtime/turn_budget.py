"""One hard, shared cap for model decisions in a PEV run."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AgentTurnBudget:
    """Mutable run-local allowance shared by Planner, Executor and Verifier."""

    maximum: int
    used: int = 0

    def __post_init__(self) -> None:
        if self.maximum < 1:
            raise ValueError("maximum agent turns must be positive")

    @property
    def remaining(self) -> int:
        """Return how many further model decisions the complete run may make."""
        return self.maximum - self.used

    def try_consume(self) -> bool:
        """Reserve one model decision before invoking the provider."""
        if self.used >= self.maximum:
            return False
        self.used += 1
        return True
