"""Wrap career_skills registry tools as JSON-payload @tools for deep agents.

The generic adapter (one function over the registry catalog) keeps the
career_skills handlers byte-for-byte untouched while giving deep agents a
langchain tool surface.  Hard invariants enforced here:
- tool budget: ``try_consume_tool`` before each handler (hard ceiling);
- duplicate-call dedup: a consecutive identical successful call is folded
  into a ``duplicate_tool_call`` observation (executor-thrash breaker);
- failures never escape: handler exceptions become failed observations.

``budgets``/``tracker`` resolve at call time: explicit args win (the
harness default path), otherwise the harness-bound context vars
(``active_budgets``/``active_tracker``) — the ``tool_factory`` seam passes
neither, so without the fallback every seam-built tool crashes on its first
call (both stay ``None`` outside a harness invocation and the guards below
degrade to unguarded, matching ``skill_graphs.build_job_discovery_tool``).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from langchain_core.tools import StructuredTool, BaseTool
from pydantic import BaseModel

from backend.app.domain.agent_runtime import AgentRole
from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.agent_runtime.tool_registry import ToolRegistry
from backend.app.services.career_skills.registry import build_career_tool_registry
from backend.app.services.deepagents_runtime.budgets import DeepAgentsBudgets
from backend.app.services.deepagents_runtime.middleware import (
    active_budgets,
    active_tracker,
)


class _JsonPayload(BaseModel):
    """Single-string argument contract shared by every adapter tool."""

    payload: str


_current_tool_context: ContextVar[ToolContext | None] = ContextVar(
    "deepagents_tool_context", default=None
)


def tool_context() -> ToolContext:
    """Return the per-invocation ToolContext bound by the harness."""
    context = _current_tool_context.get()
    if context is None:
        raise RuntimeError("tool invoked outside a harness executor invocation")
    return context


@contextmanager
def bind_tool_context(context: ToolContext):
    """Bind the run's ToolContext for the duration of one executor invocation."""
    token = _current_tool_context.set(context)
    try:
        yield
    finally:
        _current_tool_context.reset(token)


class DuplicateCallTracker:
    """Reject a consecutive identical tool call (executor thrash breaker)."""

    def __init__(self) -> None:
        self._last: tuple[str, str] | None = None  # (tool_name, payload_digest)

    def is_duplicate(self, tool_name: str, payload: dict[str, Any]) -> bool:
        digest = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        if self._last == (tool_name, digest):
            return True
        self._last = (tool_name, digest)
        return False


def _failed_observation(tool_name: str, error_code: str) -> str:
    from backend.app.services.agent_runtime.schemas import ToolObservation

    return ToolObservation(
        tool_name=tool_name, status="failed", error_code=error_code
    ).model_dump_json()


def build_skill_tools(
    *,
    skill_name: str,
    budgets: DeepAgentsBudgets | None = None,
    tracker: DuplicateCallTracker | None = None,
    context_factory: Callable[[], ToolContext] | None = None,
    registry: ToolRegistry | None = None,
) -> Sequence[BaseTool]:
    """Wrap every registry tool of ``skill_name`` as a JSON-string @tool.

    ``budgets``/``tracker`` fall back to the harness-bound context vars when
    None (the tool_factory seam passes neither); outside a harness
    invocation both resolve to None and the guards below treat the tool as
    unguarded (matching ``skill_graphs.build_job_discovery_tool``).
    """
    registry = registry or build_career_tool_registry()
    catalog = registry.tool_catalog(
        role=AgentRole.executor, allowed_skills=frozenset({skill_name})
    )
    context_factory = context_factory or tool_context
    tools: list[StructuredTool] = []
    for entry in catalog:
        name: str = entry["name"]
        description: str = entry["description"]

        def _handler(
            payload: str, *, _name: str = name, _desc: str = description
        ) -> str:
            run_budgets = budgets or active_budgets()
            run_tracker = tracker or active_tracker()
            if run_budgets is not None and not run_budgets.try_consume_tool():
                return _failed_observation(_name, "tool_budget_exhausted")
            try:
                payload_dict = json.loads(payload)
            except json.JSONDecodeError:
                return _failed_observation(_name, "invalid_tool_input")
            if run_tracker is not None and run_tracker.is_duplicate(
                _name, payload_dict
            ):
                return _failed_observation(_name, "duplicate_tool_call")
            observation = registry.invoke(
                role=AgentRole.executor,
                name=_name,
                context=context_factory(),
                payload=payload_dict,
                allowed_skills=frozenset({skill_name}),
            )
            return observation.model_dump_json()

        tools.append(
            StructuredTool.from_function(
                func=_handler,
                name=name,
                description=description,
                args_schema=_JsonPayload,
            )
        )
    return tools
