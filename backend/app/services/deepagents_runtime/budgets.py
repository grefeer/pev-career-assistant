"""Hard run-local ceilings for the DeepAgents PEV runtime.

Counters live in graph state (as a JSON dict) so checkpoint/resume never
resets them; only the wall-clock window anchor refreshes on resume
(transport pause, per CLAUDE.md).  Enforcement points: turn budget inside
agent loops (TurnBudgetMiddleware), tool budget inside tool adapters,
replans and wall-clock at harness node boundaries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from time import monotonic
from typing import Any

from backend.app.config import Settings
from backend.app.services.agent_runtime.schemas import AgentBudget


class TurnBudgetExhausted(RuntimeError):
    """Raised by TurnBudgetMiddleware when max_agent_turns is spent."""


class ToolBudgetExhausted(RuntimeError):
    """Raised by tool adapters when max_tool_calls is spent."""


@dataclass
class DeepAgentsBudgets:
    """Mutable per-run allowance shared by all three agents.

    ``to_dict``/``from_dict`` keep the counters checkpoint-safe (JSON channel).
    """

    max_agent_turns: int
    max_tool_calls: int
    max_replans: int
    max_wall_clock_seconds: int
    turns_used: int = 0
    tool_calls_used: int = 0
    replans_used: int = 0
    _window_started_at: float | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if (
            self.max_agent_turns < 1
            or self.max_tool_calls < 1
            or self.max_wall_clock_seconds < 1
        ):
            raise ValueError("budget maximums must be positive")

    @classmethod
    def from_settings(cls, settings: Settings) -> "DeepAgentsBudgets":
        return cls(
            max_agent_turns=settings.agent_harness_max_agent_turns,
            max_tool_calls=settings.agent_harness_max_tool_calls,
            max_replans=settings.agent_harness_max_replans,
            max_wall_clock_seconds=settings.agent_harness_max_wall_clock_seconds,
        )

    @classmethod
    def from_agent_budget(cls, budget: AgentBudget) -> "DeepAgentsBudgets":
        return cls(
            max_agent_turns=budget.max_agent_turns,
            max_tool_calls=budget.max_tool_calls,
            max_replans=budget.max_replans,
            max_wall_clock_seconds=budget.max_wall_clock_seconds,
        )

    def try_consume_turn(self) -> bool:
        if self.turns_used >= self.max_agent_turns:
            return False
        self.turns_used += 1
        return True

    def try_consume_tool(self) -> bool:
        if self.tool_calls_used >= self.max_tool_calls:
            return False
        self.tool_calls_used += 1
        return True

    def try_consume_replan(self) -> bool:
        if self.replans_used >= self.max_replans:
            return False
        self.replans_used += 1
        return True

    def start_window(self) -> None:
        if self._window_started_at is None:
            self._window_started_at = monotonic()

    def refresh_window(self) -> None:
        """Reset the wall-clock anchor on resume (transport pause, not spend)."""
        self._window_started_at = monotonic()

    def elapsed_seconds(self) -> float:
        if self._window_started_at is None:
            return 0.0
        return monotonic() - self._window_started_at

    def window_exhausted(self) -> bool:
        return self.elapsed_seconds() > self.max_wall_clock_seconds

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_agent_turns": self.max_agent_turns,
            "max_tool_calls": self.max_tool_calls,
            "max_replans": self.max_replans,
            "max_wall_clock_seconds": self.max_wall_clock_seconds,
            "turns_used": self.turns_used,
            "tool_calls_used": self.tool_calls_used,
            "replans_used": self.replans_used,
            "window_started_at": self._window_started_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "DeepAgentsBudgets":
        budgets = cls(
            max_agent_turns=payload["max_agent_turns"],
            max_tool_calls=payload["max_tool_calls"],
            max_replans=payload["max_replans"],
            max_wall_clock_seconds=payload["max_wall_clock_seconds"],
            turns_used=payload["turns_used"],
            tool_calls_used=payload["tool_calls_used"],
            replans_used=payload["replans_used"],
        )
        budgets._window_started_at = payload.get("window_started_at")
        return budgets
