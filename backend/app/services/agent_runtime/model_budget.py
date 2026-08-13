"""Hard per-run budget for physical model requests and measured tokens."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any


def estimate_input_tokens(instruction: str, state: dict[str, object]) -> int:
    """Conservatively estimate prompt tokens before a provider request.

    Provider usage metadata is authoritative when available.  The estimate is
    only a preflight guard so a provider that omits usage cannot bypass the
    physical input ceiling.
    """
    encoded = json.dumps(
        {"instruction": instruction, "state": state},
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    # Four UTF-8 characters per token is intentionally conservative for the
    # mixed Chinese/English prompts used by this application.
    return max(1, (len(encoded) + 3) // 4)


@dataclass
class ModelCallBudget:
    """Mutable run-local physical model allowance shared by all PEV roles."""

    max_requests: int
    max_input_tokens: int
    max_output_tokens: int
    requests_used: int = 0
    input_tokens_used: int = 0
    output_tokens_used: int = 0
    _reserved_input_tokens: int = 0
    _active_reservation: int = 0

    def __post_init__(self) -> None:
        if min(self.max_requests, self.max_input_tokens, self.max_output_tokens) < 1:
            raise ValueError("model budget ceilings must be positive")

    @property
    def remaining_requests(self) -> int:
        return self.max_requests - self.requests_used

    @property
    def remaining_input_tokens(self) -> int:
        return self.max_input_tokens - self.input_tokens_used - self._reserved_input_tokens

    @property
    def remaining_output_tokens(self) -> int:
        return self.max_output_tokens - self.output_tokens_used

    def try_reserve(self, estimated_input_tokens: int) -> bool:
        """Reserve one provider request before crossing the model boundary."""
        estimate = max(1, estimated_input_tokens)
        if self.requests_used >= self.max_requests:
            return False
        if self.input_tokens_used + self._reserved_input_tokens + estimate > self.max_input_tokens:
            return False
        self.requests_used += 1
        self._reserved_input_tokens += estimate
        self._active_reservation = estimate
        return True

    def record(self, usage: dict[str, Any] | None) -> bool:
        """Commit measured usage, falling back to the preflight estimate."""
        estimate = self._active_reservation
        self._reserved_input_tokens = max(0, self._reserved_input_tokens - estimate)
        self._active_reservation = 0
        measured_input = usage.get("input_tokens") if isinstance(usage, dict) else None
        measured_output = usage.get("output_tokens") if isinstance(usage, dict) else None
        input_tokens = measured_input if isinstance(measured_input, int) and measured_input >= 0 else estimate
        output_tokens = measured_output if isinstance(measured_output, int) and measured_output >= 0 else 0
        # Keep the guard conservative if provider metadata under-reports the
        # request relative to the preflight serialization estimate.
        self.input_tokens_used += max(input_tokens, estimate)
        self.output_tokens_used += output_tokens
        return self.within_limits

    def cancel(self) -> None:
        """Release the active reservation after a provider call fails.

        ``try_reserve`` runs before the provider boundary. If the provider
        raises, ``record`` cannot safely infer usage and must not be called;
        leaving the reservation committed would make a recoverable retry look
        permanently over budget.
        """
        estimate = self._active_reservation
        self._reserved_input_tokens = max(0, self._reserved_input_tokens - estimate)
        self._active_reservation = 0
        if estimate:
            self.requests_used = max(0, self.requests_used - 1)

    @property
    def within_limits(self) -> bool:
        return (
            self.requests_used <= self.max_requests
            and self.input_tokens_used <= self.max_input_tokens
            and self.output_tokens_used <= self.max_output_tokens
        )
