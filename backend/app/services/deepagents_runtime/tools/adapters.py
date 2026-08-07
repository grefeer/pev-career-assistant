"""Per-skill tool adapters seam for the deep-agents harness (P1 stub).

The harness's ``tool_factory`` seam defaults to ``build_skill_tools``, which
returns an empty tool set for now: the deep agent therefore cannot call any
skill, the Executor cannot produce tool evidence, and the stall breaker hands
the run to the human — a safe degradation, never a crash.  Task 3 replaces
this stub with the real skill wrappers while keeping the exact seam below
(``DuplicateCallTracker``, ``tool_context``/``bind_tool_context``,
``build_skill_tools(skill_name=, budgets=, tracker=)``).
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator, Sequence

from backend.app.services.agent_runtime.tool_context import ToolContext

# The ToolContext of the run currently being executed (set by the harness
# around each Executor invocation so tool wrappers can scope data lookups).
tool_context: contextvars.ContextVar[ToolContext | None] = contextvars.ContextVar(
    "deepagents_tool_context", default=None
)


@contextmanager
def bind_tool_context(context: ToolContext) -> Iterator[None]:
    """Context manager binding ``tool_context`` for one Executor invocation."""
    token = tool_context.set(context)
    try:
        yield
    finally:
        tool_context.reset(token)


@dataclass
class DuplicateCallTracker:
    """Tracks identical tool calls so wrappers can dedup (stub).

    ``check(key)`` returns True when the key was already recorded, recording
    it on first sight.  The real wrappers (task 3) key on the PEV
    ``candidate_idempotency_key`` (SHA-256 of normalized company + title +
    location + apply_url + evidence_hash).
    """

    _seen: set[str] = field(default_factory=set)

    def check(self, key: str) -> bool:
        if key in self._seen:
            return True
        self._seen.add(key)
        return False


def build_skill_tools(
    *,
    skill_name: str,
    budgets: Any,
    tracker: DuplicateCallTracker,
) -> Sequence[Any]:
    """Return the langchain tool wrappers for one skill (P1: empty stub).

    Always returns no tools: until task 3 lands the real wrappers, the
    harness must not expose skill scripts to the deep agent.
    """
    del skill_name, budgets, tracker  # consumed by the real implementation
    return []
