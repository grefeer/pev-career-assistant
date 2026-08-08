"""AgentMiddleware pieces shared by all three deep agents.

``TurnBudgetMiddleware`` counts every model call (one instance is compiled
into each agent graph once; the per-run budget is injected per invocation
via the ``current_budgets`` context var set by the harness).
``ToolExclusionMiddleware`` strips the deepagents default file/shell tools
so the only execution channel is the whitelisted skill wrappers.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, TYPE_CHECKING

from langchain.agents.middleware.types import AgentMiddleware

from backend.app.services.deepagents_runtime.budgets import (
    DeepAgentsBudgets,
    TurnBudgetExhausted,
)

if TYPE_CHECKING:
    from langchain.agents.middleware.types import (
        ExtendedModelResponse,
        ModelRequest,
        ModelResponse,
        ResponseT,
    )
    from langchain_core.messages import AIMessage

_current_budgets: ContextVar[DeepAgentsBudgets | None] = ContextVar(
    "deepagents_current_budgets", default=None
)

#: The harness's per-run DuplicateCallTracker, bound around one executor
#: invocation so the workflow tool (reached via the tool_factory seam, which
#: cannot carry budgets/tracker without breaking the ``(skill)`` contract)
#: applies the same dedup invariant as the adapters.
_current_tracker: ContextVar[Any | None] = ContextVar(
    "deepagents_current_tracker", default=None
)


@contextmanager
def current_budgets(budgets: DeepAgentsBudgets | None):
    """Bind the per-run budget for the duration of one agent invocation."""
    token = _current_budgets.set(budgets)
    try:
        yield
    finally:
        _current_budgets.reset(token)


@contextmanager
def current_tracker(tracker: Any | None):
    """Bind the per-run DuplicateCallTracker for one agent invocation."""
    token = _current_tracker.set(tracker)
    try:
        yield
    finally:
        _current_tracker.reset(token)


def active_budgets() -> DeepAgentsBudgets | None:
    """Return the budget bound by the harness, or None outside an invocation."""
    return _current_budgets.get()


def active_tracker() -> Any:
    """Return the DuplicateCallTracker bound by the harness, or None."""
    return _current_tracker.get()


def _tool_name(tool: Any) -> str | None:
    if isinstance(tool, dict):
        name = tool.get("name")
        return name if isinstance(name, str) else None
    return getattr(tool, "name", None)


class TurnBudgetMiddleware(AgentMiddleware[Any, Any, Any]):
    """Count every model call; raise TurnBudgetExhausted past the ceiling."""

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any]:
        budgets = _current_budgets.get()
        if budgets is not None and not budgets.try_consume_turn():
            raise TurnBudgetExhausted("agent_turn_budget_exhausted")
        return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT] | AIMessage | ExtendedModelResponse[ResponseT]:
        budgets = _current_budgets.get()
        if budgets is not None and not budgets.try_consume_turn():
            raise TurnBudgetExhausted("agent_turn_budget_exhausted")
        return await handler(request)


class ToolExclusionMiddleware(AgentMiddleware[Any, Any, Any]):
    """Filter excluded tools before the model sees them (deepagents default tools)."""

    def __init__(self, *, excluded: frozenset[str]) -> None:
        self._excluded = excluded

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any]:
        if self._excluded:
            filtered = [t for t in request.tools if _tool_name(t) not in self._excluded]
            request = request.override(tools=filtered)
        return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT] | AIMessage | ExtendedModelResponse[ResponseT]:
        if self._excluded:
            filtered = [t for t in request.tools if _tool_name(t) not in self._excluded]
            request = request.override(tools=filtered)
        return await handler(request)
