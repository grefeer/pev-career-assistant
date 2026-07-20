"""TrajectoryBuffer -- shared real-time trace recorder for adapters and SnapshotExecutor.

Records each tool call as it happens so that on failure, partial progress is
available for Supervisor takeover (via to_snapshot_context) and final traces
are available for persistence (via to_dict).
"""
from __future__ import annotations

import time
from typing import Any


class TrajectoryBuffer:
    """In-memory buffer recording tool execution steps during a single task run."""

    def __init__(
        self,
        task_id: str,
        strategy_id: str | None,
        executor_type: str,
    ) -> None:
        self.task_id = task_id
        self.strategy_id = strategy_id
        self.executor_type = executor_type
        self._steps: list[dict[str, Any]] = []
        self._failed: bool = False
        self._started_at: float = time.monotonic()

    # -- public properties --------------------------------------------------

    @property
    def steps(self) -> list[dict[str, Any]]:
        return list(self._steps)

    @property
    def failed_step_index(self) -> int | None:
        for i, s in enumerate(self._steps):
            if s["status"] == "failed":
                return i
        return None

    @property
    def elapsed_ms(self) -> float:
        return (time.monotonic() - self._started_at) * 1000

    # -- recording ----------------------------------------------------------

    def record_step(
        self,
        tool: str,
        status: str,
        params: dict[str, Any] | None = None,
        result: Any = None,
        *,
        error: Exception | None = None,
        duration_ms: float = 0,
    ) -> None:
        """Record one tool execution step.

        After the first ``status='failed'`` step, subsequent calls are
        automatically marked ``is_fallback=True`` (Supervisor takeover).
        """
        is_fallback = self._failed
        self._steps.append({
            "tool": tool,
            "status": status,
            "params": params or {},
            "result": self._safe_serialize(result) if status == "ok" else None,
            "error": str(error) if error else None,
            "error_type": type(error).__name__ if error else None,
            "duration_ms": duration_ms,
            "is_fallback": is_fallback,
            "timestamp": time.monotonic(),
        })
        if status == "failed" and not self._failed:
            self._failed = True

    # -- serialization ------------------------------------------------------

    def to_snapshot_context(self) -> dict[str, Any]:
        """Build the snapshot_context dict for Supervisor takeover.

        Includes completed steps up to (but not including) the failed step,
        plus the failed step itself with error details.
        """
        fail_idx = self.failed_step_index
        if fail_idx is None:
            return {}
        completed = self._steps[:fail_idx]
        failed = self._steps[fail_idx]
        return {
            "source": self.executor_type,
            "strategy_id": self.strategy_id,
            "completed_steps": [
                {"tool": s["tool"], "params": s["params"], "result": s["result"]}
                for s in completed if s["status"] == "ok"
            ],
            "failed_step": {
                "tool": failed["tool"],
                "params": failed["params"],
                "error": failed["error"] or "",
                "error_type": failed["error_type"] or "",
            },
        }

    def to_dict(self) -> dict[str, Any]:
        """Return full buffer contents as a plain dict for persistence."""
        return {
            "task_id": self.task_id,
            "strategy_id": self.strategy_id,
            "executor_type": self.executor_type,
            "steps": list(self._steps),
            "failed_step_index": self.failed_step_index,
            "elapsed_ms": self.elapsed_ms,
        }

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _safe_serialize(value: Any) -> Any:
        """Convert result to a JSON-safe form. Truncates large strings."""
        if value is None:
            return None
        if isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, (list, tuple)):
            return [TrajectoryBuffer._safe_serialize(v) for v in value[:50]]
        if isinstance(value, dict):
            serialized = {}
            for k, v in value.items():
                sv = TrajectoryBuffer._safe_serialize(v)
                if isinstance(sv, str) and len(sv) > 500:
                    sv = sv[:500] + "...[truncated]"
                serialized[k] = sv
            return serialized
        s = str(value)
        return s[:500] if len(s) > 500 else s
