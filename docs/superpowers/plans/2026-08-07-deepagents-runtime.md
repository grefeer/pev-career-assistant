# DeepAgents Runtime (PEV on langchain deepagents) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `backend/app/services/deepagents_runtime/` — a PEV runtime whose three agents are langchain `deepagents` (0.6.12) agents driven by an external LangGraph harness — in parallel with the existing self-built `agent_runtime`, ending with a comparative eval.

**Architecture:** External LangGraph state graph (planner → executor → verifier → route) enforces all harness invariants (budgets, one-skill-per-step, stall-breaker, `waiting_user` degradation); each node invokes a deep agent built via `create_deep_agent` (no backend → file/execute tools disabled, `subagents=None`). Tools come from two sources: the career_skills registry tools wrapped generically as `@tool` (adapters), and the job-discovery SKILL.md workflow encoded as a LangGraph subgraph wrapped as `@tool`. Execution state checkpoints to Redis (`RedisSaver`, AOF on); on run completion `flush_run` sinks an authoritative snapshot to MySQL (`deepagents_runs` + `deepagents_artifacts`). The new package enters the 100% branch coverage gate; unit tests use `FakeListChatModel` + `InMemorySaver` + subprocess seams and never touch live infrastructure.

**Tech Stack:** Python 3.12, deepagents==0.6.12, langgraph 1.2.9 (StateGraph, InMemorySaver), langgraph-checkpoint-redis 0.5.0 (RedisSaver), langchain-openai 1.1.11 (ChatOpenAI), langchain-core 1.4.9 (FakeListChatModel from `langchain_core.language_models.fake_chat_models`), FastAPI/SQLAlchemy (sink), Pydantic v2.

**Design spec:** [docs/superpowers/specs/2026-08-07-deepagents-runtime-design.md](../specs/2026-08-07-deepagents-runtime-design.md)

## Global Constraints

- **100% branch coverage** (`fail_under = 100` in `pyproject.toml`) on retained packages — the new package is auto-measured (nothing to add to `omit`). Every task must run the full suite before commit.
- **ruff clean**: `.\.venv\Scripts\python.exe -m ruff check backend tests scripts` must pass.
- **Never modify** these existing files (reused as-is): `backend/app/domain/agent_runtime.py`, `backend/app/services/agent_runtime/schemas.py`, `backend/app/services/agent_runtime/tool_registry.py`, `backend/app/services/agent_runtime/tool_context.py`, `backend/app/services/career_skills/*` (handlers), `skill/job-discovery/*` (scripts and docs).
- **Security gates (CLAUDE.md)**: never auto-click submit; never bypass login/captcha/anti-bot (blocked → degradation, never circumvent); no secrets in logs/argv (error messages truncated to ≤500 chars); Redis is never authoritative — MySQL is (gate #5 exception per spec §12 applies only to the `deepagents_runtime` execution checkpoint).
- **Budget semantics** (spec §5 / CLAUDE.md): turn/tool/replan counters live in graph state (JSON channel values) and are **never reset on resume**; only the wall-clock window refreshes on resume. `waiting_user` is always recoverable — never a crash or hard failure.
- **Thread contract** (spec §5): harness thread = `run_id`; agent threads = `f"{run_id}:{step_index}:{role}"`; workflow subgraph thread = `f"{run_id}:{step_index}:workflow"`.
- **One skill per step**: `PlanStep.allowed_skills` must contain exactly one skill; the Executor sees only that skill's tools.
- **Evidence binding**: only tool-produced evidence (dicts carrying both `source_url` and `content_hash`) enters `evidence_store`; model-proposed URIs never do.
- **Reused enums/schemas**: `RunStatus`, `VerificationDecision`, `AgentRole` from `backend.app.domain.agent_runtime`; `AgentTaskRequest`, `AgentBudget`, `ExecutionPlan`, `PlanStep`, `ToolObservation` from `backend.app.services.agent_runtime.schemas`; `ToolRegistry.invoke(role=, name=, context=, payload=, allowed_skills=)` + `tool_catalog(role=, allowed_skills=)`.
- **Commit convention**: `type: description` + `Co-Authored-By: Claude <noreply@anthropic.com>` footer.
- **Test runner**: `.\.venv\Scripts\python.exe -m pytest tests/unit/ -q` (full suite before every commit).
- **Settings in unit tests**: always construct via `settings_override(**values)` from `tests/conftest.py` (hermetic: `_env_file=None`, no production validation).

---

### Task 1: P0 skeleton — state, budgets, middleware, checkpointer factory, DB models + migration

**Files:**
- Create: `backend/app/services/deepagents_runtime/__init__.py`
- Create: `backend/app/services/deepagents_runtime/state.py`
- Create: `backend/app/services/deepagents_runtime/budgets.py`
- Create: `backend/app/services/deepagents_runtime/middleware.py`
- Create: `backend/app/services/deepagents_runtime/checkpoints/__init__.py`
- Create: `backend/app/services/deepagents_runtime/checkpoints/factory.py`
- Modify: `backend/app/db/models.py` (append two model classes after `AgentArtifact`)
- Create: `alembic/versions/20260807_0022_deepagents_runtime.py`
- Create: `tests/manual/redis_checkpoint_smoke.py` (live Redis API smoke — spec §6.3.2; `tests/manual` is excluded from pytest and from the coverage gate)
- Test: `tests/unit/test_deepagents_state.py`, `tests/unit/test_deepagents_budgets.py`, `tests/unit/test_deepagents_middleware.py`, `tests/unit/test_deepagents_checkpoint_factory.py`

**Interfaces:**
- Consumes: `Settings` fields (config.py, verified): `checkpoint_backend: Literal["sqlite","redis"]="sqlite"`, `redis_url`, `agent_harness_max_agent_turns=12`, `agent_harness_max_tool_calls=24`, `agent_harness_max_replans=2`, `agent_harness_max_wall_clock_seconds=300`; `AgentBudget` from agent_runtime.schemas; `settings_override` from `tests/conftest.py`.
- Produces (later tasks rely on these exact names):
  - `DeepAgentsState` (TypedDict, state.py) — channel schema for the harness graph.
  - `build_initial_state(*, run_id, user_id, goal, allowed_skills, context, budgets) -> DeepAgentsState` (state.py).
  - `DeepAgentsBudgets` (budgets.py) with `from_settings(settings)`, `from_agent_budget(budget)`, `try_consume_turn()`, `try_consume_tool()`, `try_consume_replan()`, `start_window()`, `refresh_window()`, `elapsed_seconds()`, `window_exhausted()`, `to_dict()`, `from_dict()`.
  - `TurnBudgetExhausted`, `ToolBudgetExhausted` (budgets.py).
  - `current_budgets(budgets)` context manager, `TurnBudgetMiddleware`, `ToolExclusionMiddleware` (middleware.py).
  - `create_checkpointer(settings) -> BaseCheckpointSaver` (checkpoints/factory.py).
  - ORM models `DeepAgentsRun`, `DeepAgentsArtifact` (db/models.py).

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_deepagents_budgets.py`:

```python
from __future__ import annotations

import pytest

from backend.app.services.agent_runtime.schemas import AgentBudget
from backend.app.services.deepagents_runtime.budgets import DeepAgentsBudgets
from tests.conftest import settings_override


def test_from_settings_maps_harness_limits() -> None:
    settings = settings_override(
        agent_harness_max_agent_turns=7,
        agent_harness_max_tool_calls=9,
        agent_harness_max_replans=3,
        agent_harness_max_wall_clock_seconds=120,
    )
    budgets = DeepAgentsBudgets.from_settings(settings)
    assert budgets.max_agent_turns == 7
    assert budgets.max_tool_calls == 9
    assert budgets.max_replans == 3
    assert budgets.max_wall_clock_seconds == 120
    assert budgets.turns_used == 0


def test_from_agent_budget_maps_request_budget() -> None:
    request_budget = AgentBudget(
        max_agent_turns=5, max_tool_calls=10, max_replans=1, max_wall_clock_seconds=60
    )
    budgets = DeepAgentsBudgets.from_agent_budget(request_budget)
    assert budgets.max_agent_turns == 5
    assert budgets.max_replans == 1


def test_turn_and_tool_budgets_are_hard_ceilings() -> None:
    budgets = DeepAgentsBudgets(
        max_agent_turns=2, max_tool_calls=1, max_replans=1, max_wall_clock_seconds=60
    )
    assert budgets.try_consume_turn()
    assert budgets.try_consume_turn()
    assert not budgets.try_consume_turn()
    assert budgets.try_consume_tool()
    assert not budgets.try_consume_tool()


def test_replan_budget_exhausts() -> None:
    budgets = DeepAgentsBudgets(
        max_agent_turns=10, max_tool_calls=10, max_replans=1, max_wall_clock_seconds=60
    )
    assert budgets.try_consume_replan()
    assert not budgets.try_consume_replan()


def test_wall_clock_window_refreshes_on_resume() -> None:
    budgets = DeepAgentsBudgets(
        max_agent_turns=10, max_tool_calls=10, max_replans=1, max_wall_clock_seconds=60
    )
    budgets.start_window()
    assert budgets.elapsed_seconds() < 60
    assert not budgets.window_exhausted()
    budgets._window_started_at = 0.0  # simulate an ancient start => elapsed ~ infinity
    assert budgets.window_exhausted()
    budgets.refresh_window()
    assert not budgets.window_exhausted()


def test_dict_roundtrip_preserves_counters_and_window() -> None:
    budgets = DeepAgentsBudgets(
        max_agent_turns=10, max_tool_calls=10, max_replans=1, max_wall_clock_seconds=60
    )
    budgets.try_consume_turn()
    budgets.try_consume_tool()
    payload = budgets.to_dict()
    restored = DeepAgentsBudgets.from_dict(payload)
    assert restored.turns_used == 1
    assert restored.tool_calls_used == 1
    assert restored.to_dict() == payload
    assert restored.elapsed_seconds() == 0.0  # window never started -> 0.0 branch


def test_non_positive_maximum_rejected() -> None:
    with pytest.raises(ValueError):
        DeepAgentsBudgets(
            max_agent_turns=0, max_tool_calls=1, max_replans=1, max_wall_clock_seconds=60
        )
```

`tests/unit/test_deepagents_state.py`:

```python
from __future__ import annotations

from backend.app.services.deepagents_runtime.budgets import DeepAgentsBudgets
from backend.app.services.deepagents_runtime.state import (
    DeepAgentsState,
    build_initial_state,
)


def test_initial_state_contains_every_channel() -> None:
    budgets = DeepAgentsBudgets(
        max_agent_turns=2, max_tool_calls=2, max_replans=1, max_wall_clock_seconds=60
    )
    state = build_initial_state(
        run_id="run-1",
        user_id="user-1",
        goal="帮我找后端岗位",
        allowed_skills=["job-discovery", "job-matching"],
        context={"candidate_urls": ["https://example.com/jobs"]},
        budgets=budgets,
    )
    assert set(state) == {
        "run_id", "user_id", "goal", "allowed_skills", "context", "budget",
        "plan_json", "step_index", "retry_count", "stalled_decisions",
        "evidence_store", "decisions", "run_status", "error_code",
        "final_summary", "started_at", "finished_at",
    }
    assert state["run_status"] is None
    assert state["plan_json"] is None
    assert state["evidence_store"] == []
    assert state["budget"]["turns_used"] == 0
```

`tests/unit/test_deepagents_middleware.py`:

```python
from __future__ import annotations

import pytest

from backend.app.services.deepagents_runtime.budgets import (
    DeepAgentsBudgets,
    TurnBudgetExhausted,
)
from backend.app.services.deepagents_runtime.middleware import (
    TurnBudgetMiddleware,
    current_budgets,
)


class _FakeModelRequest:
    def __init__(self) -> None:
        self.tools = [{"name": "keep"}]


class _FakeModelResponse:
    pass


def _handler(request):
    return _FakeModelResponse()


def test_turn_middleware_counts_and_exhausts() -> None:
    budgets = DeepAgentsBudgets(
        max_agent_turns=2, max_tool_calls=10, max_replans=1, max_wall_clock_seconds=60
    )
    middleware = TurnBudgetMiddleware()
    request = _FakeModelRequest()
    with current_budgets(budgets):
        middleware.wrap_model_call(request, _handler)
        middleware.wrap_model_call(request, _handler)
        assert budgets.turns_used == 2
        with pytest.raises(TurnBudgetExhausted):
            middleware.wrap_model_call(request, _handler)


def test_turn_middleware_inactive_without_context() -> None:
    middleware = TurnBudgetMiddleware()
    middleware.wrap_model_call(_FakeModelRequest(), _handler)  # no context -> no-op


def test_exclusion_middleware_filters_tools() -> None:
    from backend.app.services.deepagents_runtime.middleware import (
        ToolExclusionMiddleware,
    )

    middleware = ToolExclusionMiddleware(excluded=frozenset({"execute"}))
    seen: list[str] = []

    def recording_handler(request):
        seen.extend(t["name"] for t in request.tools)
        return _FakeModelResponse()

    request = _FakeModelRequest()
    middleware.wrap_model_call(request, recording_handler)
    assert seen == ["keep"]


async def _async_handler(request):
    return _FakeModelResponse()


def test_turn_middleware_async_counts_and_exhausts() -> None:
    import asyncio

    budgets = DeepAgentsBudgets(
        max_agent_turns=1, max_tool_calls=10, max_replans=1, max_wall_clock_seconds=60
    )
    middleware = TurnBudgetMiddleware()

    async def scenario() -> None:
        with current_budgets(budgets):
            assert await middleware.awrap_model_call(_FakeModelRequest(), _async_handler)
            with pytest.raises(TurnBudgetExhausted):
                await middleware.awrap_model_call(_FakeModelRequest(), _async_handler)

    asyncio.run(scenario())


def test_turn_middleware_async_inactive_without_context() -> None:
    import asyncio

    middleware = TurnBudgetMiddleware()
    asyncio.run(middleware.awrap_model_call(_FakeModelRequest(), _async_handler))


def test_exclusion_middleware_async_filters_tools() -> None:
    import asyncio

    from backend.app.services.deepagents_runtime.middleware import (
        ToolExclusionMiddleware,
    )

    middleware = ToolExclusionMiddleware(excluded=frozenset({"execute"}))
    seen: list[str] = []

    async def recording_handler(request):
        seen.extend(t["name"] for t in request.tools)
        return _FakeModelResponse()

    asyncio.run(middleware.awrap_model_call(_FakeModelRequest(), recording_handler))
    assert seen == ["keep"]
```

`tests/unit/test_deepagents_checkpoint_factory.py`:

```python
from __future__ import annotations

from langgraph.checkpoint.memory import InMemorySaver

from backend.app.services.deepagents_runtime.checkpoints.factory import (
    create_checkpointer,
)
from tests.conftest import settings_override


def test_sqlite_backend_maps_to_inmemory_saver() -> None:
    settings = settings_override(checkpoint_backend="sqlite")
    assert isinstance(create_checkpointer(settings), InMemorySaver)


def test_redis_backend_constructs_redis_saver(monkeypatch) -> None:
    created = {}

    class FakeRedisSaver:
        def __init__(self) -> None:
            self.setup_called = False

        def setup(self) -> None:
            self.setup_called = True

    def fake_from_url(url: str) -> FakeRedisSaver:
        created["url"] = url
        return FakeRedisSaver()

    monkeypatch.setattr(
        "backend.app.services.deepagents_runtime.checkpoints.factory.RedisSaver.from_url",
        fake_from_url,
    )
    settings = settings_override(
        checkpoint_backend="redis", redis_url="redis://localhost:6379/0"
    )
    saver = create_checkpointer(settings)
    assert isinstance(saver, FakeRedisSaver)
    assert saver.setup_called
    assert created["url"] == "redis://localhost:6379/0"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_deepagents_budgets.py tests/unit/test_deepagents_state.py tests/unit/test_deepagents_middleware.py tests/unit/test_deepagents_checkpoint_factory.py -q`
Expected: FAIL — `ModuleNotFoundError: deepagents_runtime`.

- [ ] **Step 3: Implement the skeleton**

`backend/app/services/deepagents_runtime/__init__.py`:

```python
"""PEV runtime built on langchain deepagents (parallel to agent_runtime).

The three deep agents (Planner / Executor / Verifier) are driven by an
external LangGraph harness graph that enforces all hard invariants
(budgets, one-skill-per-step, stall-breaker, recoverable waiting_user
degradation).  Execution checkpoints live in Redis (AOF); completed runs
sink to MySQL.
"""

from backend.app.services.deepagents_runtime.checkpoints.factory import (
    create_checkpointer,
)
from backend.app.services.deepagents_runtime.state import DeepAgentsState

__all__ = ["DeepAgentsState", "create_checkpointer"]
```

`backend/app/services/deepagents_runtime/state.py`:

```python
"""Channel state of the external DeepAgents PEV harness graph.

Every value must be JSON-serializable: LangGraph persists channel values
to the checkpointer, so budget counters survive resume and are never
reset (only the wall-clock window refreshes, per CLAUDE.md).
"""

from __future__ import annotations

import operator
import time
from typing import Annotated, Any, TypedDict

from backend.app.services.deepagents_runtime.budgets import DeepAgentsBudgets


class DeepAgentsState(TypedDict):
    run_id: str
    user_id: str
    goal: str
    allowed_skills: list[str]
    context: dict[str, Any]
    budget: dict[str, Any]  # DeepAgentsBudgets.to_dict() payload
    plan_json: dict[str, Any] | None  # ExecutionPlan.model_dump(mode="json")
    step_index: int
    retry_count: int  # consecutive RETRY_EXECUTOR decisions on the current step
    stalled_decisions: int  # consecutive no-progress executor decisions
    evidence_store: Annotated[list[dict[str, Any]], operator.add]
    decisions: Annotated[list[dict[str, Any]], operator.add]
    run_status: str | None
    error_code: str | None
    final_summary: str | None
    started_at: float  # epoch seconds, set by the orchestrator
    finished_at: float | None


def build_initial_state(
    *,
    run_id: str,
    user_id: str,
    goal: str,
    allowed_skills: list[str],
    context: dict[str, Any],
    budgets: DeepAgentsBudgets,
) -> DeepAgentsState:
    """Build the complete first-state for a fresh run (every channel present)."""
    return {
        "run_id": run_id,
        "user_id": user_id,
        "goal": goal,
        "allowed_skills": list(allowed_skills),
        "context": context,
        "budget": budgets.to_dict(),
        "plan_json": None,
        "step_index": 0,
        "retry_count": 0,
        "stalled_decisions": 0,
        "evidence_store": [],
        "decisions": [],
        "run_status": None,
        "error_code": None,
        "final_summary": None,
        "started_at": time.time(),
        "finished_at": None,
    }
```

`backend/app/services/deepagents_runtime/budgets.py`:

```python
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
```

`backend/app/services/deepagents_runtime/middleware.py`:

```python
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


@contextmanager
def current_budgets(budgets: DeepAgentsBudgets | None):
    """Bind the per-run budget for the duration of one agent invocation."""
    token = _current_budgets.set(budgets)
    try:
        yield
    finally:
        _current_budgets.reset(token)


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
```

`backend/app/services/deepagents_runtime/checkpoints/__init__.py`:

```python
"""Checkpointer factory + MySQL sink for the DeepAgents PEV runtime."""
```

`backend/app/services/deepagents_runtime/checkpoints/factory.py`:

```python
"""Checkpointer factory: Redis for execution, in-memory for unit tests.

``redis`` uses RedisSaver (AOF-persistent; the production backend, enforced
by Settings.validate_production_settings).  ``sqlite`` maps to
InMemorySaver so unit suites run without infrastructure (spec §6.1).
"""

from __future__ import annotations

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.redis import RedisSaver

from backend.app.config import Settings


def create_checkpointer(settings: Settings) -> BaseCheckpointSaver:
    """Return the run-state saver for the configured checkpoint backend."""
    if settings.checkpoint_backend == "redis":
        saver = RedisSaver.from_url(settings.redis_url)
        saver.setup()
        return saver
    return InMemorySaver()
```

`backend/app/db/models.py` — append after the `AgentArtifact` class. Reuse the module's existing imports (`UUIDPrimaryKeyMixin`, `TimestampMixin`, `Base`, `UniqueConstraint`, `RunStatus` already imported for `AgentRun`, `Enum`, `enum_kwargs`, `JSON`, `Text`, `String`, `DateTime`, `ForeignKey`, `Mapped`, `mapped_column`, `Any`, `datetime`):

```python
class DeepAgentsRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One completed deepagents PEV run (MySQL-authoritative snapshot).

    Written once at run completion by ``checkpoints/sink.py``; Redis holds
    only the in-flight execution checkpoint (security gate #5 exception,
    spec §12).  Payload JSON is constrained to safe summaries and artifact
    references; raw prompts, resume bytes and secrets must not be stored.
    """

    __tablename__ = "deepagents_runs"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    thread_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    allowed_skills_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    budget_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[RunStatus] = mapped_column(
        Enum(RunStatus, name="deepagents_run_status", **enum_kwargs),
        nullable=False,
        index=True,
    )
    plan_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    decisions_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, nullable=False
    )
    error_code: Mapped[str | None] = mapped_column(String(64))
    final_summary: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DeepAgentsArtifact(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One evidence/artifact produced by a deepagents run (authoritative copy)."""

    __tablename__ = "deepagents_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "run_id", "artifact_id", name="uq_deepagents_artifacts_run_artifact"
        ),
    )

    run_id: Mapped[str] = mapped_column(
        ForeignKey("deepagents_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    artifact_id: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    source_url: Mapped[str | None] = mapped_column(String(2048))
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
```

`alembic/versions/20260807_0022_deepagents_runtime.py` — first run `.\.venv\Scripts\alembic.exe current` and set `down_revision` to whatever it reports (expected `20260805_0020`; if the untracked `20260806_0021_profile_active_version` is already applied, use `20260806_0021`). Follow the exact style of `alembic/versions/20260801_0017_agent_runtime_runs.py`:

```python
"""deepagents PEV runtime records

Revision ID: 20260807_0022
Revises: <alembic current output>
Create Date: 2026-08-07 12:00:00.000000

MySQL-authoritative completion snapshots for the deepagents-based PEV
runtime.  Redis holds only the in-flight execution checkpoint; completed
run records and evidence artifacts are the authoritative copy here.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260807_0022"
down_revision: Union[str, Sequence[str], None] = "<alembic current output>"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _enum(*values: str, name: str) -> sa.Enum:
    return sa.Enum(
        *values,
        name=name,
        native_enum=False,
        create_constraint=True,
    )


def upgrade() -> None:
    """Create deepagents run snapshots and evidence artifacts."""
    op.create_table(
        "deepagents_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column(
            "user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("thread_id", sa.String(length=128), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("allowed_skills_json", sa.JSON(), nullable=False),
        sa.Column("budget_json", sa.JSON(), nullable=False),
        sa.Column(
            "status",
            _enum(
                "queued", "running", "waiting_user", "succeeded", "failed",
                "cancelled", name="deepagents_run_status",
            ),
            nullable=False,
        ),
        sa.Column("plan_json", sa.JSON(), nullable=True),
        sa.Column("decisions_json", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("final_summary", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("thread_id", name="uq_deepagents_runs_thread_id"),
    )
    op.create_index("ix_deepagents_runs_user_id", "deepagents_runs", ["user_id"], unique=False)
    op.create_index("ix_deepagents_runs_status", "deepagents_runs", ["status"], unique=False)

    op.create_table(
        "deepagents_artifacts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column(
            "run_id",
            sa.String(length=36),
            sa.ForeignKey("deepagents_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("artifact_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("source_url", sa.String(length=2048), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "run_id", "artifact_id", name="uq_deepagents_artifacts_run_artifact"
        ),
    )
    op.create_index("ix_deepagents_artifacts_run_id", "deepagents_artifacts", ["run_id"], unique=False)


def downgrade() -> None:
    """Drop deepagents snapshots in dependency order."""
    op.drop_index("ix_deepagents_artifacts_run_id", table_name="deepagents_artifacts")
    op.drop_table("deepagents_artifacts")
    op.drop_index("ix_deepagents_runs_status", table_name="deepagents_runs")
    op.drop_index("ix_deepagents_runs_user_id", table_name="deepagents_runs")
    op.drop_table("deepagents_runs")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_deepagents_budgets.py tests/unit/test_deepagents_state.py tests/unit/test_deepagents_middleware.py tests/unit/test_deepagents_checkpoint_factory.py -q`
Expected: PASS (all 14).

- [ ] **Step 5: Live Redis checkpointer smoke (spec §6.3.2 / §11)**

This is the P0 compatibility smoke: langgraph-checkpoint-redis 0.5.0's sync
API must work with langgraph-checkpoint 4.1.1 (a clean import does not mean
the full API behaves identically). Per spec §11: if it fails, stop and fall
back to the MySQL saver plan — flag the user before continuing.

`tests/manual/redis_checkpoint_smoke.py` (manual diagnostics — excluded from
pytest and from the coverage gate):

```python
"""Live smoke: langgraph-checkpoint-redis 0.5.0 round-trip (spec §6.3.2).

Confirms RedisSaver round-trips a checkpoint through the real Redis (AOF on).
Skips (exit 0) when Redis is unreachable; exits 1 when the round-trip fails.
Run with the docker-compose stack up:

    .\\.venv\\Scripts\\python.exe -m tests.manual.redis_checkpoint_smoke
"""

from __future__ import annotations

import asyncio
import sys

from langgraph.checkpoint.redis import RedisSaver

from backend.app.config import get_settings


def _checkpoint(thread_id: str) -> tuple[dict, dict]:
    checkpoint = {
        "v": 1,
        "ts": 1723000000.0,
        "id": f"smoke-{thread_id}",
        "channel_values": {"run_status": "running", "budget": {"max_agent_turns": 12}},
        "channel_versions": {},
        "versions_seen": {},
        "pending_sends": [],
    }
    return checkpoint, {"configurable": {"thread_id": thread_id}}


def _sync_round_trip(saver) -> bool:
    checkpoint, config = _checkpoint("smoke-thread")
    saver.put(config, checkpoint, {}, {})
    loaded = saver.get_tuple(config)
    saver.put(config, None, {}, {})  # delete the smoke thread
    return (
        loaded is not None
        and loaded.checkpoint["channel_values"]["run_status"] == "running"
    )


async def _async_round_trip(saver) -> bool:
    checkpoint, config = _checkpoint("smoke-async")
    await saver.aput(config, checkpoint, {}, {})
    loaded = await saver.aget_tuple(config)
    await saver.aput(config, None, {}, {})  # delete the smoke thread
    return (
        loaded is not None
        and loaded.checkpoint["channel_values"]["run_status"] == "running"
    )


def main() -> int:
    settings = get_settings()
    try:
        saver = RedisSaver.from_url(settings.redis_url)
    except Exception as exc:  # noqa: BLE001 - Redis may simply be down
        print(
            "SKIP: Redis unreachable (%s); start the docker-compose stack first"
            % type(exc).__name__
        )
        return 0
    try:
        ok = _sync_round_trip(saver)
        api = "sync put/get_tuple"
    except (AttributeError, TypeError, NotImplementedError):
        # 0.5.0 removed sync methods -> async aput/aget_tuple still works
        ok = asyncio.run(_async_round_trip(saver))
        api = "async aput/aget_tuple"
    print(
        "PASS: RedisSaver %s round-trip OK" % api
        if ok
        else "FAIL: RedisSaver %s round-trip mismatch" % api
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
```

Run: `.\.venv\Scripts\python.exe -m tests.manual.redis_checkpoint_smoke`
Expected (stack up): `PASS: RedisSaver <sync|async> round-trip OK` — sync preferred, async fallback is also PASS. Without Redis: `SKIP: Redis unreachable...` (exit 0).
Decision per spec §11: PASS → continue with RedisSaver; FAIL → STOP, fall back to the MySQL saver plan and flag the user.

- [ ] **Step 6: Verify DB models import cleanly and run the full suite**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/ -q`
Expected: all PASS (existing suite plus the new files).

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/deepagents_runtime backend/app/db/models.py alembic/versions/20260807_0022_deepagents_runtime.py tests/unit/test_deepagents_state.py tests/unit/test_deepagents_budgets.py tests/unit/test_deepagents_middleware.py tests/unit/test_deepagents_checkpoint_factory.py tests/manual/redis_checkpoint_smoke.py
git commit -m "feat(deepagents-runtime): P0 skeleton — state, budgets, middleware, checkpointer factory, DB tables

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: P1 — deep agents + external harness graph

**Files:**
- Create: `backend/app/services/deepagents_runtime/agents.py`
- Create: `backend/app/services/deepagents_runtime/harness.py`
- Create: `backend/app/services/deepagents_runtime/tools/__init__.py` (docstring only)
- Create: `backend/app/services/deepagents_runtime/tools/adapters.py` — minimal working stub of the three names the harness imports (`DuplicateCallTracker`, `bind_tool_context`/`tool_context` ContextVar pair, `build_skill_tools` returning `[]`); Task 3 replaces this stub with the real adapter
- Test: `tests/unit/test_deepagents_agents.py`, `tests/unit/test_deepagents_harness.py`

**Interfaces:**
- Consumes: Task 1 (`DeepAgentsState`, `build_initial_state`, `DeepAgentsBudgets` + `current_budgets`, `TurnBudgetMiddleware`, `ToolExclusionMiddleware`); `AgentTaskRequest`, `ExecutionPlan` (agent_runtime.schemas); `RunStatus`, `VerificationDecision` (domain.agent_runtime — verified members: queued/running/waiting_user/succeeded/failed/cancelled; PASS/RETRY_EXECUTOR/REPLAN/NEED_USER/FAIL); `create_deep_agent` (deepagents); `FakeListChatModel` from `langchain_core.language_models.fake_chat_models`.
- Produces:
  - `build_planner_agent(*, model, checkpointer=None)`, `build_executor_agent(*, model, tools, checkpointer=None)`, `build_verifier_agent(*, model, checkpointer=None)` (agents.py) — compiled deep agents, file/shell tools disabled, `subagents=None`, turn-budget middleware installed.
  - `VerifierDecision` (agents.py) — pydantic `{decision: VerificationDecision, rationale: str}`.
  - `DeepAgentsHarness(*, model_factory, tool_factory=None, checkpointer=None)` (harness.py) with `run(request: AgentTaskRequest, *, run_id: str, budgets: DeepAgentsBudgets | None = None) -> dict` and `resume(run_id: str) -> dict`.
  - `model_factory(role: str) -> BaseChatModel` — per-role model seam (tests inject per-role `FakeListChatModel` scripted sequences).
  - `tool_factory(skill_name: str) -> list[BaseTool] | None` — per-skill tool seam (Task 3 wires the real adapters).

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_deepagents_agents.py`:

```python
from __future__ import annotations

from langchain_core.language_models.fake_chat_models import FakeListChatModel

from backend.app.services.deepagents_runtime.agents import (
    build_executor_agent,
    build_planner_agent,
    build_verifier_agent,
)


def _fake_model(*responses: str):
    return FakeListChatModel(responses=list(responses))


def test_planner_agent_compiles() -> None:
    agent = build_planner_agent(model=_fake_model('{"task": null}'))
    assert agent is not None
    assert agent.name == "planner"


def test_executor_agent_compiles_with_tools() -> None:
    agent = build_executor_agent(model=_fake_model("done"), tools=[])
    assert agent.name == "executor"


def test_verifier_agent_compiles() -> None:
    agent = build_verifier_agent(model=_fake_model("{}"))
    assert agent.name == "verifier"
```

(If `agent.name` is not present on the compiled graph in the installed deepagents version, assert `agent.get_graph().name == "planner"` instead — verify at implementation time with one `print(agent)` in a scratch shell.)

`tests/unit/test_deepagents_harness.py`:

```python
from __future__ import annotations

import json

from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import tool

from backend.app.services.agent_runtime.schemas import AgentTaskRequest
from backend.app.services.deepagents_runtime.budgets import DeepAgentsBudgets
from backend.app.services.deepagents_runtime.harness import DeepAgentsHarness

PLAN_JSON = json.dumps(
    {
        "task": {
            "goal": "帮我找后端岗位",
            "allowed_skills": ["job-discovery", "job-matching"],
            "context": {"candidate_urls": ["https://example.com/jobs"]},
            "budget": {
                "max_agent_turns": 12,
                "max_tool_calls": 24,
                "max_replans": 2,
                "max_wall_clock_seconds": 300,
            },
        },
        "created_by": "planner",
        "complexity": "L1",
        "success_criteria": ["找到至少 1 个匹配岗位"],
        "steps": [
            {
                "step_id": "discover",
                "objective": "提取岗位列表",
                "allowed_skills": ["job-discovery"],
                "success_criteria": [],
                "requires_verification": True,
            },
            {
                "step_id": "match",
                "objective": "排序匹配",
                "allowed_skills": ["job-matching"],
                "success_criteria": [],
                "requires_verification": True,
            },
        ],
    },
    ensure_ascii=False,
)
VERIFIER_PASS_JSON = json.dumps(
    {"decision": "PASS", "rationale": "ok"}, ensure_ascii=False
)
VERIFIER_NEED_USER_JSON = json.dumps(
    {"decision": "NEED_USER", "rationale": "需要更多信息"}, ensure_ascii=False
)


@tool
def stub_discovery_tool(payload: str) -> str:
    """Test stub: return one piece of tool-produced evidence as observation JSON."""
    return json.dumps(
        {
            "tool_name": "stub",
            "status": "succeeded",
            "output": {
                "source_url": "https://example.com/jobs",
                "content_hash": "abc123",
                "candidates": [{"title": "后端工程师"}],
            },
        }
    )


def _scripted_factory(scripted: dict[str, list[str] | list[AIMessage]]):
    """Return a model_factory consuming one FakeListChatModel per role."""
    def factory(role: str) -> FakeListChatModel:
        return FakeListChatModel(responses=list(scripted[role]))

    return factory


def _request(**overrides) -> AgentTaskRequest:
    values = dict(
        goal="帮我找后端岗位",
        allowed_skills=["job-discovery", "job-matching"],
        context={"candidate_urls": ["https://example.com/jobs"]},
    )
    values.update(overrides)
    return AgentTaskRequest(**values)


def test_happy_path_plans_executes_verifies_and_succeeds() -> None:
    harness = DeepAgentsHarness(
        model_factory=_scripted_factory(
            {
                "planner": [PLAN_JSON],
                "executor": [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "stub_discovery_tool",
                                "args": {"payload": "{}"},
                                "id": "call_1",
                            }
                        ],
                    ),
                    "evidence collected",
                ],
                "verifier": [VERIFIER_PASS_JSON],
            }
        ),
        tool_factory=lambda skill: [stub_discovery_tool] if skill == "job-discovery" else [],
    )
    final = harness.run(_request(), run_id="run-1")
    assert final["run_status"] == "succeeded"
    assert final["step_index"] == 2
    assert final["error_code"] is None
    # evidence bound from the tool observation output (source_url + content_hash)
    assert any(item.get("content_hash") == "abc123" for item in final["evidence_store"])
    roles = {d.get("role") for d in final["decisions"]}
    assert {"planner", "executor", "verifier"} <= roles


def test_verifier_replan_consumes_replan_budget_and_degrades_when_exhausted() -> None:
    harness = DeepAgentsHarness(
        model_factory=_scripted_factory(
            {
                "planner": [PLAN_JSON, PLAN_JSON, PLAN_JSON],
                "executor": ["executed"],
                "verifier": [
                    json.dumps({"decision": "REPLAN", "rationale": "insufficient"}, ensure_ascii=False),
                    json.dumps({"decision": "REPLAN", "rationale": "still insufficient"}, ensure_ascii=False),
                    json.dumps({"decision": "REPLAN", "rationale": "again"}, ensure_ascii=False),
                ],
            }
        ),
        tool_factory=lambda skill: [],
    )
    request = _request()
    request.budget = request.budget.model_copy(update={"max_replans": 1})
    final = harness.run(request, run_id="run-replan")
    assert final["run_status"] == "waiting_user"
    assert final["error_code"] == "max_replans_exceeded"


def test_retry_exhaustion_degrades_to_waiting_user() -> None:
    harness = DeepAgentsHarness(
        model_factory=_scripted_factory(
            {
                "planner": [PLAN_JSON],
                "executor": ["executed", "executed again"],
                "verifier": [
                    json.dumps({"decision": "RETRY_EXECUTOR", "rationale": "补证据"}, ensure_ascii=False),
                    json.dumps({"decision": "RETRY_EXECUTOR", "rationale": "还要补"}, ensure_ascii=False),
                ],
            }
        ),
        tool_factory=lambda skill: [],
    )
    request = _request()
    request.budget = request.budget.model_copy(update={"max_replans": 1})
    final = harness.run(request, run_id="run-retry")
    assert final["run_status"] == "waiting_user"
    assert final["error_code"] == "retries_exceeded"
    assert final["step_index"] == 0  # same step, retried


def test_need_user_degrades_to_waiting_user() -> None:
    harness = DeepAgentsHarness(
        model_factory=_scripted_factory(
            {
                "planner": [PLAN_JSON],
                "executor": ["executed"],
                "verifier": [VERIFIER_NEED_USER_JSON],
            }
        ),
        tool_factory=lambda skill: [],
    )
    final = harness.run(_request(), run_id="run-need-user")
    assert final["run_status"] == "waiting_user"
    assert final["error_code"] == "needs_user"


def test_fail_marks_run_failed() -> None:
    harness = DeepAgentsHarness(
        model_factory=_scripted_factory(
            {
                "planner": [PLAN_JSON],
                "executor": ["executed"],
                "verifier": [
                    json.dumps({"decision": "FAIL", "rationale": "不可达"}, ensure_ascii=False)
                ],
            }
        ),
        tool_factory=lambda skill: [],
    )
    final = harness.run(_request(), run_id="run-fail")
    assert final["run_status"] == "failed"
    assert final["error_code"] == "verification_failed"


def test_wall_clock_exhaustion_degrades_before_planner() -> None:
    budgets = DeepAgentsBudgets(
        max_agent_turns=12, max_tool_calls=24, max_replans=2, max_wall_clock_seconds=300
    )
    budgets._window_started_at = 0.0  # ancient anchor => window exhausted

    def exploding_factory(role: str):
        raise AssertionError(f"model must not be called for role {role}")

    harness = DeepAgentsHarness(model_factory=exploding_factory, tool_factory=lambda skill: [])
    final = harness.run(_request(), run_id="run-wallclock", budgets=budgets)
    assert final["run_status"] == "waiting_user"
    assert final["error_code"] == "wall_clock_budget_exhausted"


def test_stall_breaker_after_three_non_progress_decisions() -> None:
    # Verifier REPLANs keep us on step 0; executor keeps producing no
    # ToolMessage => no progress => stalled counter hits 3 on the 3rd entry.
    harness = DeepAgentsHarness(
        model_factory=_scripted_factory(
            {
                "planner": [PLAN_JSON, PLAN_JSON, PLAN_JSON],
                "executor": ["no progress", "no progress", "no progress"],
                "verifier": [
                    json.dumps({"decision": "REPLAN", "rationale": "无进展"}, ensure_ascii=False),
                    json.dumps({"decision": "REPLAN", "rationale": "无进展"}, ensure_ascii=False),
                    json.dumps({"decision": "REPLAN", "rationale": "无进展"}, ensure_ascii=False),
                ],
            }
        ),
        tool_factory=lambda skill: [],
    )
    request = _request()
    request.budget = request.budget.model_copy(update={"max_replans": 3})
    final = harness.run(request, run_id="run-stall")
    assert final["run_status"] == "waiting_user"
    assert final["error_code"] == "stalled_no_progress"


def test_resume_continues_from_checkpoint() -> None:
    from langgraph.checkpoint.memory import InMemorySaver

    verifier_calls = {"n": 0}

    def factory(role: str) -> FakeListChatModel:
        if role == "verifier":
            verifier_calls["n"] += 1
            if verifier_calls["n"] == 1:
                return FakeListChatModel(responses=[VERIFIER_NEED_USER_JSON])
            return FakeListChatModel(responses=[VERIFIER_PASS_JSON])
        if role == "planner":
            return FakeListChatModel(responses=[PLAN_JSON, PLAN_JSON])
        return FakeListChatModel(responses=["executed", "executed again"])

    harness = DeepAgentsHarness(
        model_factory=factory,
        tool_factory=lambda skill: [],
        checkpointer=InMemorySaver(),
    )
    request = _request()
    request.budget = request.budget.model_copy(update={"max_replans": 2})
    first = harness.run(request, run_id="run-resume")
    assert first["run_status"] == "waiting_user"
    assert first["error_code"] == "needs_user"
    first_decision_count = len(first["decisions"])

    resumed = harness.resume("run-resume")
    assert resumed["run_status"] == "succeeded"
    assert len(resumed["decisions"]) > first_decision_count  # counters never reset
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_deepagents_agents.py tests/unit/test_deepagents_harness.py -q`
Expected: FAIL — `ModuleNotFoundError: deepagents_runtime.agents`.

- [ ] **Step 3: Implement the agents**

`backend/app/services/deepagents_runtime/agents.py`:

```python
"""Factory for the three deep agents (Planner / Executor / Verifier).

Each agent is a ``create_deep_agent`` graph with:
- no ``backend`` and an explicit tool-exclusion middleware, so the
  deepagents default file/shell tools (ls/read_file/write_file/edit_file/
  glob/grep/execute) are never offered — the only execution channel is the
  whitelisted skill wrappers (security: skill scripts only, spec §4.2);
- ``subagents=None`` so the ``task`` tool is never offered;
- the turn-budget middleware, counting every model call against the
  per-run budget injected via ``current_budgets``.
"""

from __future__ import annotations

from typing import Any, Sequence

from deepagents import create_deep_agent
from pydantic import BaseModel, Field

from backend.app.domain.agent_runtime import VerificationDecision
from backend.app.services.agent_runtime.schemas import ExecutionPlan
from backend.app.services.deepagents_runtime.middleware import (
    ToolExclusionMiddleware,
    TurnBudgetMiddleware,
)

# deepagents default tools that must never reach the model. ``write_todos``
# stays (todo discipline); ``task`` requires subagents which we never pass.
_DISABLED_TOOLS = frozenset(
    {"ls", "read_file", "write_file", "edit_file", "glob", "grep", "execute"}
)

PLANNER_PROMPT = """你是求职助手 PEV 运行时的规划者。读取用户目标、已确认画像事实与
已观察证据，输出严格满足 ExecutionPlan schema 的执行计划：
- task 字段必须原样回显输入（goal / allowed_skills / context / budget）；
- 每一步 allowed_skills 只能包含恰好一个 skill；
- 计划只能使用任务允许的 skill；
- complexity 与 success_criteria 必须填写。
只能输出合法 JSON。"""

EXECUTOR_PROMPT = """你是求职助手 PEV 运行时的执行者。你只负责当前这一步的目标：
- 只能调用当前 step 允许的 skill 工具；
- 优先使用工具产出证据（source_url + content_hash），不要凭记忆编造；
- 用工具完成证据收集后，用一句话总结你观察到的事实。"""

VERIFIER_PROMPT = """你是求职助手 PEV 运行时的验证者。你独立检查证据与产物，
绝不轻信执行者的总结：
- 证据不足 -> REPLAN；
- 证据可补 -> RETRY_EXECUTOR；
- 需要用户提供信息 -> NEED_USER；
- 目标不可达成 -> FAIL；
- 证据充分且满足成功标准 -> PASS。
输出 VerifierDecision JSON：decision 必须是 PASS/RETRY_EXECUTOR/REPLAN/
NEED_USER/FAIL 之一，rationale 简述依据。"""


class VerifierDecision(BaseModel):
    """Structured Verifier outcome consumed by the harness router."""

    model_config = {"extra": "forbid"}

    decision: VerificationDecision
    rationale: str = Field(min_length=1, max_length=4_000)


def build_agent(
    *,
    model: Any,
    name: str,
    tools: Sequence[Any] | None = None,
    system_prompt: str | None = None,
    response_format: Any = None,
    checkpointer: Any = None,
) -> Any:
    """Build one deep agent with file/shell tools disabled and turn budget on."""
    return create_deep_agent(
        model,
        tools=list(tools) if tools else None,
        system_prompt=system_prompt,
        middleware=(
            TurnBudgetMiddleware(),
            ToolExclusionMiddleware(excluded=_DISABLED_TOOLS),
        ),
        subagents=None,
        backend=None,
        permissions=[],
        response_format=response_format,
        checkpointer=checkpointer,
        name=name,
    )


def build_planner_agent(*, model: Any, checkpointer: Any = None) -> Any:
    """Planner: no tools, structured ExecutionPlan output."""
    return build_agent(
        model=model,
        name="planner",
        system_prompt=PLANNER_PROMPT,
        response_format=ExecutionPlan,
        checkpointer=checkpointer,
    )


def build_executor_agent(
    *, model: Any, tools: Sequence[Any], checkpointer: Any = None
) -> Any:
    """Executor: only the current step's skill-scoped tools."""
    return build_agent(
        model=model,
        name="executor",
        tools=list(tools),
        system_prompt=EXECUTOR_PROMPT,
        checkpointer=checkpointer,
    )


def build_verifier_agent(*, model: Any, checkpointer: Any = None) -> Any:
    """Verifier: no business tools, structured VerifierDecision output."""
    return build_agent(
        model=model,
        name="verifier",
        system_prompt=VERIFIER_PROMPT,
        response_format=VerifierDecision,
        checkpointer=checkpointer,
    )
```

- [ ] **Step 4: Implement the harness**

`backend/app/services/deepagents_runtime/harness.py`:

```python
"""External harness graph: planner -> executor -> verifier -> route.

The graph enforces every hard invariant (spec §5); the agents themselves
only ever produce decisions.  Budget counters and decisions live in channel
values so checkpoint/resume never resets them; only the wall-clock window
refreshes on resume.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Sequence

from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.graph import END, START, StateGraph

from backend.app.domain.agent_runtime import RunStatus, VerificationDecision
from backend.app.services.agent_runtime.schemas import (
    AgentTaskRequest,
    ExecutionPlan,
    ToolObservation,
)
from backend.app.services.deepagents_runtime.agents import (
    VerifierDecision,
    build_executor_agent,
    build_planner_agent,
    build_verifier_agent,
)
from backend.app.services.deepagents_runtime.budgets import (
    DeepAgentsBudgets,
    TurnBudgetExhausted,
    current_budgets,
)
from backend.app.services.deepagents_runtime.state import (
    DeepAgentsState,
    build_initial_state,
)

_OBSERVATION_EXCERPT_LIMIT = 1_200
_STALL_LIMIT = 3


class InvalidModelResponseError(RuntimeError):
    """Raised when an agent's output cannot be parsed into its schema."""


def _agent_thread(run_id: str, step_index: int, role: str) -> str:
    return f"{run_id}:{step_index}:{role}"


def _extract_structured(result: dict[str, Any], model: type[Any]) -> Any:
    """Read the structured output of a deep agent invocation.

    ``create_agent`` places the parsed structured output in the
    ``structured_response`` channel.  Raises InvalidModelResponseError when
    absent (the harness degrades to waiting_user instead of crashing).
    """
    structured = result.get("structured_response")
    if structured is None:
        raise InvalidModelResponseError("missing structured_response")
    if isinstance(structured, model):
        return structured
    return model.model_validate(structured)


def _project_tool_observations(
    messages: Sequence[Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Project tool results into decisions + evidence (incremental, bounded).

    Only tool-produced dicts carrying both ``source_url`` and ``content_hash``
    become evidence (evidence-bound tools invariant).
    """
    decisions: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, ToolMessage):
            continue
        try:
            obs = ToolObservation.model_validate(json.loads(message.content))
        except (ValueError, json.JSONDecodeError):
            continue
        decisions.append(
            {
                "tool": obs.tool_name,
                "status": obs.status,
                "error_code": obs.error_code,
            }
        )
        if obs.status != "succeeded" or obs.output is None:
            continue
        stack = [obs.output]
        seen: set[tuple[str, str]] = set()
        while stack:
            value = stack.pop()
            if isinstance(value, dict):
                if (
                    isinstance(value.get("source_url"), str)
                    and isinstance(value.get("content_hash"), str)
                ):
                    key = (value["source_url"], value["content_hash"])
                    if key not in seen:
                        seen.add(key)
                        evidence.append(value)
                stack.extend(value.values())
            elif isinstance(value, list):
                stack.extend(value)
    return decisions, evidence


def _is_non_progress(decision: dict[str, Any]) -> bool:
    if decision.get("status") != "succeeded":
        return True
    return decision.get("error_code") in {"duplicate_tool_call", "blocked"}


def _sole_skill(allowed_skills: list[str]) -> str:
    if len(allowed_skills) != 1:
        raise ValueError("each plan step must allow exactly one skill")
    return allowed_skills[0]


def _degrade(state: DeepAgentsState, error_code: str) -> dict[str, Any]:
    return {
        "run_status": RunStatus.waiting_user.value,
        "error_code": error_code,
        "final_summary": None,
    }


def _verifier_input(state: DeepAgentsState) -> str:
    plan = ExecutionPlan.model_validate(state["plan_json"])
    step = plan.steps[state["step_index"]]
    evidence_lines = []
    for item in state["evidence_store"][-10:]:
        text = json.dumps(item, ensure_ascii=False)[:_OBSERVATION_EXCERPT_LIMIT]
        evidence_lines.append(text)
    return json.dumps(
        {
            "step_objective": step.objective,
            "success_criteria": step.success_criteria,
            "evidence": evidence_lines,
        },
        ensure_ascii=False,
    )


class DeepAgentsHarness:
    """The deterministic lifecycle around the three deep agents."""

    def __init__(
        self,
        *,
        model_factory: Callable[[str], Any],
        tool_factory: Callable[[str], Sequence[Any]] | None = None,
        checkpointer: Any = None,
    ) -> None:
        self._model_factory = model_factory
        self._tool_factory = tool_factory
        self._checkpointer = checkpointer
        self._tracker = DuplicateCallTracker()
        self._graph = self._build_graph()

    def _build_graph(self) -> Any:
        graph = StateGraph(DeepAgentsState)
        graph.add_node("planner", self._planner_node)
        graph.add_node("executor", self._executor_node)
        graph.add_node("verifier", self._verifier_node)
        graph.add_node("finalize", self._finalize_node)
        graph.add_edge(START, "planner")
        graph.add_conditional_edges(
            "planner",
            lambda state: "finalize" if state["run_status"] else "executor",
            {"executor": "executor", "finalize": "finalize"},
        )
        graph.add_conditional_edges(
            "executor",
            lambda state: "finalize" if state["run_status"] else "verifier",
            {"verifier": "verifier", "finalize": "finalize"},
        )
        graph.add_conditional_edges(
            "verifier",
            self._route,
            {
                "next_step": "executor",
                "replan": "planner",
                "waiting_user": "finalize",
                "failed": "finalize",
                "succeeded": "finalize",
            },
        )
        graph.add_edge("finalize", END)
        return graph.compile(checkpointer=self._checkpointer)

    def run(
        self,
        request: AgentTaskRequest,
        *,
        run_id: str,
        budgets: DeepAgentsBudgets | None = None,
    ) -> dict[str, Any]:
        budgets = budgets or DeepAgentsBudgets.from_agent_budget(request.budget)
        budgets.start_window()
        initial = build_initial_state(
            run_id=run_id,
            user_id="",
            goal=request.goal,
            allowed_skills=request.allowed_skills,
            context=request.context,
            budgets=budgets,
        )
        final = self._graph.invoke(initial, {"configurable": {"thread_id": run_id}})
        return dict(final)

    def resume(self, run_id: str) -> dict[str, Any]:
        snapshot = self._graph.get_state({"configurable": {"thread_id": run_id}})
        budgets = DeepAgentsBudgets.from_dict(snapshot.values["budget"])
        budgets.refresh_window()
        final = self._graph.invoke(
            {"budget": budgets.to_dict()},
            {"configurable": {"thread_id": run_id}},
        )
        return dict(final)

    # -- nodes -------------------------------------------------------------

    def _planner_node(self, state: DeepAgentsState) -> dict[str, Any]:
        budgets = DeepAgentsBudgets.from_dict(state["budget"])
        if budgets.window_exhausted():
            return _degrade(state, "wall_clock_budget_exhausted")
        previous_status = state["run_status"]
        if (
            previous_status is not None
            and previous_status != RunStatus.waiting_user.value
        ):
            # terminal run resumed -> no-op (only waiting_user is recoverable)
            return {"finished_at": time.time()}
        if state["plan_json"] is not None and not budgets.try_consume_replan():
            return _degrade(state, "max_replans_exceeded")
        planner = build_planner_agent(
            model=self._model_factory("planner"), checkpointer=self._checkpointer
        )
        task_payload = {
            "goal": state["goal"],
            "allowed_skills": state["allowed_skills"],
            "context": state["context"],
        }
        try:
            with current_budgets(budgets):
                result = planner.invoke(
                    {"messages": [HumanMessage(json.dumps(task_payload, ensure_ascii=False))]},
                    {"configurable": {"thread_id": _agent_thread(state["run_id"], 0, "planner")}},
                )
            plan = _extract_structured(result, ExecutionPlan)
            plan.validate_plan_authority()
            for step in plan.steps:
                _sole_skill(step.allowed_skills)
        except (TurnBudgetExhausted, InvalidModelResponseError, ValueError) as exc:
            if isinstance(exc, TurnBudgetExhausted):
                return _degrade(state, str(exc))
            return _degrade(state, "invalid_model_response")
        return {
            "plan_json": plan.model_dump(mode="json"),
            "retry_count": 0,
            "run_status": None,  # clears waiting_user on resume (resume = re-entry)
            "error_code": None,
            "budget": budgets.to_dict(),
            "decisions": [
                {"role": "planner", "decision": "PLANNED", "steps": len(plan.steps)}
            ],
        }

    def _executor_node(self, state: DeepAgentsState) -> dict[str, Any]:
        budgets = DeepAgentsBudgets.from_dict(state["budget"])
        if budgets.window_exhausted():
            return _degrade(state, "wall_clock_budget_exhausted")
        last_decision = state["decisions"][-1] if state["decisions"] else None
        is_retry = bool(
            last_decision
            and last_decision.get("role") == "verifier"
            and last_decision.get("decision") == VerificationDecision.RETRY_EXECUTOR.value
        )
        if is_retry and not budgets.try_consume_replan():
            return _degrade(state, "retries_exceeded")
        plan = ExecutionPlan.model_validate(state["plan_json"])
        step = plan.steps[state["step_index"]]
        skill = _sole_skill(step.allowed_skills)
        if self._tool_factory is not None:
            tools = self._tool_factory(skill)
        else:
            tools = build_skill_tools(
                skill_name=skill,
                budgets=budgets,
                tracker=self._tracker,
            )
        agent = build_executor_agent(
            model=self._model_factory("executor"),
            tools=tools,
            checkpointer=self._checkpointer,
        )
        tool_ctx = ToolContext(
            user_id=state["user_id"],
            run_id=state["run_id"],
            metadata={
                "observed_public_evidence": state["evidence_store"],
                "context": state["context"],
            },
        )
        try:
            with bind_tool_context(tool_ctx):
                with current_budgets(budgets):
                    result = agent.invoke(
                        {"messages": [HumanMessage(step.objective)]},
                        {"configurable": {"thread_id": _agent_thread(state["run_id"], state["step_index"], "executor")}},
                    )
        except TurnBudgetExhausted as exc:
            return _degrade(state, str(exc))
        decisions, evidence = _project_tool_observations(result["messages"])
        progress_made = any(not _is_non_progress(d) for d in decisions)
        stalled = 0 if progress_made else state["stalled_decisions"] + 1
        if stalled >= _STALL_LIMIT:
            return _degrade(state, "stalled_no_progress")
        return {
            "decisions": decisions,
            "evidence_store": evidence,
            "stalled_decisions": stalled,
            "retry_count": state["retry_count"] + 1 if is_retry else 0,
            "budget": budgets.to_dict(),
        }

    def _verifier_node(self, state: DeepAgentsState) -> dict[str, Any]:
        budgets = DeepAgentsBudgets.from_dict(state["budget"])
        if budgets.window_exhausted():
            return _degrade(state, "wall_clock_budget_exhausted")
        verifier = build_verifier_agent(
            model=self._model_factory("verifier"), checkpointer=self._checkpointer
        )
        try:
            with current_budgets(budgets):
                result = verifier.invoke(
                    {"messages": [HumanMessage(_verifier_input(state))]},
                    {"configurable": {"thread_id": _agent_thread(state["run_id"], state["step_index"], "verifier")}},
                )
            decision = _extract_structured(result, VerifierDecision)
        except (TurnBudgetExhausted, InvalidModelResponseError) as exc:
            if isinstance(exc, TurnBudgetExhausted):
                return _degrade(state, str(exc))
            return _degrade(state, "invalid_model_response")
        update: dict[str, Any] = {
            "decisions": [
                {
                    "role": "verifier",
                    "decision": decision.decision.value,
                    "rationale": decision.rationale,
                }
            ],
            "budget": budgets.to_dict(),
        }
        if decision.decision == VerificationDecision.PASS:
            update["step_index"] = state["step_index"] + 1
        return update

    def _finalize_node(self, state: DeepAgentsState) -> dict[str, Any]:
        if state["run_status"] is not None:
            return {"finished_at": time.time()}
        last = state["decisions"][-1] if state["decisions"] else None
        decision = last.get("decision") if last else None
        if decision == VerificationDecision.PASS.value:
            return {
                "run_status": RunStatus.succeeded.value,
                "final_summary": "所有步骤已通过验证",
                "finished_at": time.time(),
            }
        if decision == VerificationDecision.NEED_USER.value:
            return {
                "run_status": RunStatus.waiting_user.value,
                "error_code": "needs_user",
                "finished_at": time.time(),
            }
        return {
            "run_status": RunStatus.failed.value,
            "error_code": "verification_failed",
            "finished_at": time.time(),
        }

    # -- routing -----------------------------------------------------------

    def _route(self, state: DeepAgentsState) -> str:
        if state["run_status"] is not None:
            return "finalize"
        last = state["decisions"][-1] if state["decisions"] else None
        decision = last.get("decision") if last else None
        if decision == VerificationDecision.PASS.value:
            plan = ExecutionPlan.model_validate(state["plan_json"])
            if state["step_index"] >= len(plan.steps):
                return "succeeded"
            return "next_step"
        if decision == VerificationDecision.RETRY_EXECUTOR.value:
            return "next_step"  # same step re-executes; replan budget checked at executor entry
        if decision == VerificationDecision.REPLAN.value:
            return "replan"
        if decision == VerificationDecision.NEED_USER.value:
            return "waiting_user"
        return "failed"
```

(`DuplicateCallTracker`, `bind_tool_context`/`tool_context`, `build_skill_tools` are imported at the top of harness.py, plus `from backend.app.services.agent_runtime.tool_context import ToolContext` for the executor's `tool_ctx`. They are implemented in Task 2 as minimal working versions in `tools/adapters.py` (see the Task 2 file list) so the harness imports cleanly: `DuplicateCallTracker` as shown below, `bind_tool_context`/`tool_context` as the ContextVar pair, and `build_skill_tools` as a no-op returning `[]`. Task 3 replaces `build_skill_tools` with the real registry loop. The default adapter path is only exercised when `tool_factory=None` — Task 3's `test_harness_executor_uses_registry_adapters_by_default` covers it.)

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_deepagents_agents.py tests/unit/test_deepagents_harness.py -q`
Expected: PASS.

**If a test fails**, the most likely causes and their fixes (in order):
1. `create_deep_agent` rejects `tools=None` → pass `tools=[]` for the planner instead.
2. The structured-output channel is not `structured_response` on the installed deepagents/langgraph version → inspect `result.keys()` in a scratch shell and update `_extract_structured` (the same key is used in `_planner_node` and `_verifier_node`).
3. `FakeListChatModel` raises `OutOfOrder`/exhaustion because a node ran more model calls than scripted → script one extra response per role (the executor loop always ends with a final plain AIMessage after the tool call).
4. `agent.name` attribute missing → use `agent.get_graph().name` (see the note in `test_deepagents_agents.py`).

- [ ] **Step 6: Run the full suite + ruff**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/ -q` then `.\.venv\Scripts\python.exe -m ruff check backend tests scripts`
Expected: all PASS; ruff clean.

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/deepagents_runtime/agents.py backend/app/services/deepagents_runtime/harness.py backend/app/services/deepagents_runtime/tools tests/unit/test_deepagents_agents.py tests/unit/test_deepagents_harness.py
git commit -m "feat(deepagents-runtime): P1 external harness graph + 3 deep agents

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: P2 — career_skills tool adapters + extraction gate

**Files:**
- Replace: `backend/app/services/deepagents_runtime/tools/adapters.py` (Task 2 stub -> the full adapter; the docstring in the file already announces the replacement)
- Create: `backend/app/services/deepagents_runtime/tools/extract_gate.py`
- Modify: `backend/app/services/deepagents_runtime/tools/__init__.py` (docstring, already exists from Task 2)
- Modify: `backend/app/services/deepagents_runtime/harness.py` (imports already wired in Task 2)
- Test: `tests/unit/test_deepagents_adapters.py`, `tests/unit/test_deepagents_extract_gate.py`

**Interfaces:**
- Consumes: Task 1/2 (`DeepAgentsBudgets`, `DuplicateCallTracker` minimal version); `ToolRegistry.invoke(role=, name=, context=, payload=, allowed_skills=)` + `tool_catalog(role=, allowed_skills=)`; `ToolObservation` (agent_runtime.schemas); `ToolContext(user_id, run_id, metadata)` (agent_runtime.tool_context); `build_career_tool_registry()` (career_skills.registry); `extract_observed_job_details` + `ExtractObservedJobDetailsInput/Output` (career_skills.job_discovery — evidence lookup is `context.metadata["observed_public_evidence"]`, a list of dicts matching `artifact_id` OR `observed:<content_hash>`; raises `PublicJobFetchError` when the artifact is missing).
- Produces:
  - `bind_tool_context(context: ToolContext)` context manager + `tool_context() -> ToolContext` getter (adapters.py).
  - `DuplicateCallTracker` (adapters.py) — `is_duplicate(tool_name, payload) -> bool` (consecutive-identical only).
  - `build_skill_tools(*, skill_name, budgets, tracker, context_factory=None, registry=None) -> Sequence[BaseTool]` (adapters.py) — one JSON-payload `StructuredTool` per registry tool of the skill (executor role scope).
  - `extract_with_gate(context, payload, *, enabled, llm_extractor=None) -> ExtractObservedJobDetailsOutput` (extract_gate.py) — regex-first, LLM only on empty/low-confidence when enabled, strict-Pareto union merge.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_deepagents_adapters.py`:

```python
from __future__ import annotations

import json

from langchain_core.language_models.fake_chat_models import FakeListChatModel

from backend.app.services.agent_runtime.schemas import AgentTaskRequest
from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.career_skills.registry import build_career_tool_registry
from backend.app.services.deepagents_runtime.budgets import DeepAgentsBudgets
from backend.app.services.deepagents_runtime.tools.adapters import (
    DuplicateCallTracker,
    build_skill_tools,
)


def _context_factory() -> ToolContext:
    return ToolContext(
        user_id="user-1",
        run_id="run-1",
        metadata={"observed_public_evidence": []},
    )


def _budgets() -> DeepAgentsBudgets:
    return DeepAgentsBudgets(
        max_agent_turns=12, max_tool_calls=24, max_replans=2, max_wall_clock_seconds=300
    )


def test_skill_tools_cover_registry_catalog() -> None:
    registry = build_career_tool_registry()
    catalog = registry.tool_catalog(
        role="executor", allowed_skills=frozenset({"job-discovery"})
    )
    tools = build_skill_tools(
        skill_name="job-discovery",
        context_factory=_context_factory,
        budgets=_budgets(),
        tracker=DuplicateCallTracker(),
    )
    assert {tool.name for tool in tools} == {entry["name"] for entry in catalog}


def test_skill_scoping_excludes_other_skills() -> None:
    tools = build_skill_tools(
        skill_name="job-matching",
        context_factory=_context_factory,
        budgets=_budgets(),
        tracker=DuplicateCallTracker(),
    )
    assert {tool.name for tool in tools} == {"match-observed-jobs"}


def test_adapter_folds_handler_failure_to_observation() -> None:
    tools = build_skill_tools(
        skill_name="job-discovery",
        context_factory=_context_factory,
        budgets=_budgets(),
        tracker=DuplicateCallTracker(),
    )
    by_name = {tool.name: tool for tool in tools}
    result = by_name["extract-observed-job-details"].invoke(
        {"payload": json.dumps({"artifact_id": "missing"})}
    )
    obs = json.loads(result)
    assert obs["status"] == "failed"  # unknown artifact -> failed observation, not crash
    assert obs["error_code"] is not None


def test_tool_budget_exhaustion_returns_observation_not_exception() -> None:
    budgets = _budgets()
    budgets.tool_calls_used = budgets.max_tool_calls
    tools = build_skill_tools(
        skill_name="job-discovery",
        context_factory=_context_factory,
        budgets=budgets,
        tracker=DuplicateCallTracker(),
    )
    result = tools[0].invoke({"payload": "{}"})
    obs = json.loads(result)
    assert obs["status"] == "failed"
    assert obs["error_code"] == "tool_budget_exhausted"


def test_duplicate_consecutive_call_rejected() -> None:
    tracker = DuplicateCallTracker()
    payload = {"artifact_id": "a"}
    assert not tracker.is_duplicate("extract", payload)
    assert tracker.is_duplicate("extract", payload)  # same call again
    assert not tracker.is_duplicate("extract", {"artifact_id": "b"})


def test_invalid_json_payload_folded_to_observation() -> None:
    tools = build_skill_tools(
        skill_name="job-discovery",
        context_factory=_context_factory,
        budgets=_budgets(),
        tracker=DuplicateCallTracker(),
    )
    result = tools[0].invoke({"payload": "{not json"})
    obs = json.loads(result)
    assert obs["status"] == "failed"


def test_harness_executor_uses_registry_adapters_by_default() -> None:
    """tool_factory=None -> harness wires build_skill_tools over the real
    registry (covers the default adapter path + bind_tool_context)."""
    from langchain_core.messages import AIMessage

    from backend.app.services.deepagents_runtime.harness import DeepAgentsHarness

    plan_json = json.dumps(
        {
            "task": {
                "goal": "帮我找后端岗位",
                "allowed_skills": ["job-discovery"],
                "context": {"candidate_urls": ["https://example.com/jobs"]},
                "budget": {
                    "max_agent_turns": 12,
                    "max_tool_calls": 24,
                    "max_replans": 2,
                    "max_wall_clock_seconds": 300,
                },
            },
            "created_by": "planner",
            "complexity": "L1",
            "success_criteria": ["找到至少 1 个匹配岗位"],
            "steps": [
                {
                    "step_id": "discover",
                    "objective": "提取岗位列表",
                    "allowed_skills": ["job-discovery"],
                    "success_criteria": [],
                    "requires_verification": True,
                }
            ],
        },
        ensure_ascii=False,
    )
    harness = DeepAgentsHarness(
        model_factory=lambda role: FakeListChatModel(
            responses={
                "planner": [plan_json],
                "executor": [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "fetch-public-job-pages",
                                "args": {"payload": json.dumps({"urls": []})},
                                "id": "call_1",
                            }
                        ],
                    ),
                    "evidence collected",
                ],
                "verifier": [
                    json.dumps({"decision": "PASS", "rationale": "ok"}, ensure_ascii=False)
                ],
            }[role]
        )
        # tool_factory omitted -> real adapters over the registry
    )
    final = harness.run(
        AgentTaskRequest(
            goal="帮我找后端岗位",
            allowed_skills=["job-discovery"],
            context={"candidate_urls": ["https://example.com/jobs"]},
        ),
        run_id="run-adapters",
    )
    assert final["run_status"] == "succeeded"
    tool_decisions = [d for d in final["decisions"] if d.get("role") == "executor"]
    assert tool_decisions and tool_decisions[0]["tool"] == "fetch-public-job-pages"
```

`tests/unit/test_deepagents_extract_gate.py`:

```python
from __future__ import annotations

from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.career_skills.job_discovery import (
    ExtractObservedJobDetailsInput,
    ExtractObservedJobDetailsOutput,
)
from backend.app.services.deepagents_runtime.tools import extract_gate
from backend.app.services.deepagents_runtime.tools.extract_gate import extract_with_gate

_CONTEXT = ToolContext(user_id="u1", run_id="r1")


def _candidate(**overrides) -> dict:
    candidate = {
        "title": "后端工程师",
        "company_name": "示例公司",
        "locations": ["上海"],
        "responsibilities": "职责",
        "requirements": "要求",
        "recruitment_types": ["校招"],
        "apply_url": None,
        "deadline_text": None,
        "confidence": 0.9,
        "evidence_refs": [{"artifact_id": "a1"}],
        "normalization_warnings": [],
    }
    candidate.update(overrides)
    return candidate


def _regex_output(*candidates: dict) -> ExtractObservedJobDetailsOutput:
    return ExtractObservedJobDetailsOutput(
        source_artifact_id="a1",
        source_url="https://example.com/jobs",
        content_hash="hash-regex",
        candidates=list(candidates),
    )


def _llm_extractor(context, payload) -> ExtractObservedJobDetailsOutput:
    return ExtractObservedJobDetailsOutput(
        source_artifact_id=payload.artifact_id,
        source_url="https://example.com/jobs",
        content_hash="hash-llm",
        candidates=[_candidate(title="后端工程师(LLM)")],
    )


def _patch_regex(monkeypatch, output: ExtractObservedJobDetailsOutput) -> None:
    monkeypatch.setattr(
        extract_gate,
        "extract_observed_job_details",
        lambda context, payload: output,
    )


def test_gate_disabled_never_calls_llm(monkeypatch) -> None:
    called = []

    def llm(context, payload):
        called.append(1)
        return _llm_extractor(context, payload)

    _patch_regex(monkeypatch, _regex_output())
    payload = ExtractObservedJobDetailsInput(artifact_id="a1")
    result = extract_with_gate(_CONTEXT, payload, enabled=False, llm_extractor=llm)
    assert called == []
    assert result.candidates == []


def test_gate_without_llm_extractor_returns_regex(monkeypatch) -> None:
    _patch_regex(monkeypatch, _regex_output(_candidate()))
    payload = ExtractObservedJobDetailsInput(artifact_id="a1")
    result = extract_with_gate(_CONTEXT, payload, enabled=True, llm_extractor=None)
    assert [c.title for c in result.candidates] == ["后端工程师"]


def test_gate_skips_llm_when_regex_confident(monkeypatch) -> None:
    called = []

    def llm(context, payload):
        called.append(1)
        return _llm_extractor(context, payload)

    _patch_regex(monkeypatch, _regex_output(_candidate(confidence=0.9)))
    payload = ExtractObservedJobDetailsInput(artifact_id="a1")
    result = extract_with_gate(_CONTEXT, payload, enabled=True, llm_extractor=llm)
    assert called == []
    assert [c.title for c in result.candidates] == ["后端工程师"]


def test_gate_calls_llm_on_empty_regex(monkeypatch) -> None:
    _patch_regex(monkeypatch, _regex_output())
    payload = ExtractObservedJobDetailsInput(artifact_id="a1")
    result = extract_with_gate(
        _CONTEXT, payload, enabled=True, llm_extractor=_llm_extractor
    )
    assert [c.title for c in result.candidates] == ["后端工程师(LLM)"]


def test_gate_calls_llm_on_low_or_missing_confidence(monkeypatch) -> None:
    _patch_regex(
        monkeypatch,
        _regex_output(_candidate(confidence=None), _candidate(title="低置信", confidence=0.4)),
    )
    payload = ExtractObservedJobDetailsInput(artifact_id="a1")
    result = extract_with_gate(
        _CONTEXT, payload, enabled=True, llm_extractor=_llm_extractor
    )
    titles = [c.title for c in result.candidates]
    assert "后端工程师" in titles  # regex candidate preserved verbatim
    assert "低置信" in titles
    assert "后端工程师(LLM)" in titles  # new identity appended (pareto union)


def test_merge_skips_duplicate_identity(monkeypatch) -> None:
    _patch_regex(monkeypatch, _regex_output(_candidate(confidence=0.4)))

    def same_identity_llm(context, payload):
        return ExtractObservedJobDetailsOutput(
            source_artifact_id=payload.artifact_id,
            source_url="https://example.com/jobs",
            content_hash="hash-llm",
            candidates=[
                _candidate(confidence=0.9, title="后端工程师", responsibilities="LLM 改写")
            ],
        )

    payload = ExtractObservedJobDetailsInput(artifact_id="a1")
    result = extract_with_gate(
        _CONTEXT, payload, enabled=True, llm_extractor=same_identity_llm
    )
    assert len(result.candidates) == 1
    assert result.candidates[0].responsibilities == "职责"  # regex version kept
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_deepagents_adapters.py tests/unit/test_deepagents_extract_gate.py -q`
Expected: FAIL — `ModuleNotFoundError: deepagents_runtime.tools` (or the Task 2 stub `build_skill_tools` returning `[]` makes the catalog assertions fail — either way RED).

- [ ] **Step 3: Implement the adapters**

`backend/app/services/deepagents_runtime/tools/__init__.py`:

```python
"""Tool layer: career_skills @tool adapters + skill workflow subgraphs."""
```

`backend/app/services/deepagents_runtime/tools/adapters.py` (this file REPLACES the Task 2 stub with the real implementation):

```python
"""Wrap career_skills registry tools as JSON-payload @tools for deep agents.

The generic adapter (one function over the registry catalog) keeps the
career_skills handlers byte-for-byte untouched while giving deep agents a
langchain tool surface.  Hard invariants enforced here:
- tool budget: ``try_consume_tool`` before each handler (hard ceiling);
- duplicate-call dedup: a consecutive identical successful call is folded
  into a ``duplicate_tool_call`` observation (executor-thrash breaker);
- failures never escape: handler exceptions become failed observations.
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
    budgets: DeepAgentsBudgets,
    tracker: DuplicateCallTracker,
    context_factory: Callable[[], ToolContext] | None = None,
    registry: ToolRegistry | None = None,
) -> Sequence[BaseTool]:
    """Wrap every registry tool of ``skill_name`` as a JSON-string @tool."""
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
            payload_json: str, *, _name: str = name, _desc: str = description
        ) -> str:
            if not budgets.try_consume_tool():
                return _failed_observation(_name, "tool_budget_exhausted")
            try:
                payload = json.loads(payload_json)
            except json.JSONDecodeError:
                return _failed_observation(_name, "invalid_tool_input")
            if tracker.is_duplicate(_name, payload):
                return _failed_observation(_name, "duplicate_tool_call")
            observation = registry.invoke(
                role=AgentRole.executor,
                name=_name,
                context=context_factory(),
                payload=payload,
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
```

`backend/app/services/deepagents_runtime/tools/extract_gate.py`:

```python
"""Regex-first JD extraction with an optional LLM gate (spec §4.3).

The deterministic regex extractor runs first and costs zero tokens; the LLM
extractor is consulted only when the gate is enabled AND the regex output is
empty or low-confidence.  The merge is a strict-Pareto union: regex
candidates are preserved verbatim; LLM candidates whose (title, company)
identity is not already present are appended.
"""

from __future__ import annotations

from typing import Callable

from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.career_skills.job_discovery import (
    ExtractObservedJobDetailsInput,
    ExtractObservedJobDetailsOutput,
    extract_observed_job_details,
)

_LOW_CONFIDENCE_BELOW = 0.6


def _needs_llm(result: ExtractObservedJobDetailsOutput) -> bool:
    if not result.candidates:
        return True
    return any(
        candidate.confidence is None or candidate.confidence < _LOW_CONFIDENCE_BELOW
        for candidate in result.candidates
    )


def _identity(candidate) -> tuple[str, str]:
    return ((candidate.title or "").strip(), (candidate.company_name or "").strip())


def _pareto_union(
    base: ExtractObservedJobDetailsOutput, extra: ExtractObservedJobDetailsOutput
) -> ExtractObservedJobDetailsOutput:
    base_ids = {_identity(candidate) for candidate in base.candidates}
    for candidate in extra.candidates:
        if _identity(candidate) not in base_ids:
            base.candidates.append(candidate)
            base_ids.add(_identity(candidate))
    return base


def extract_with_gate(
    context: ToolContext,
    payload: ExtractObservedJobDetailsInput,
    *,
    enabled: bool,
    llm_extractor: (
        Callable[
            [ToolContext, ExtractObservedJobDetailsInput],
            ExtractObservedJobDetailsOutput,
        ]
        | None
    ) = None,
) -> ExtractObservedJobDetailsOutput:
    """Run the deterministic extractor; gate the LLM on low-confidence gaps."""
    result = extract_observed_job_details(context, payload)
    if not enabled or llm_extractor is None or not _needs_llm(result):
        return result
    return _pareto_union(result, llm_extractor(context, payload))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_deepagents_adapters.py tests/unit/test_deepagents_extract_gate.py tests/unit/test_deepagents_harness.py -q`
Expected: PASS.

- [ ] **Step 5: Full suite + ruff + commit**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/ -q`; `.\.venv\Scripts\python.exe -m ruff check backend tests scripts`
Commit:

```bash
git add backend/app/services/deepagents_runtime/tools backend/app/services/deepagents_runtime/harness.py tests/unit/test_deepagents_adapters.py tests/unit/test_deepagents_extract_gate.py
git commit -m "feat(deepagents-runtime): P2 career_skills tool adapters + extraction gate

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: P3 — job-discovery skill workflow subgraph → @tool

**Files:**
- Create: `backend/app/services/deepagents_runtime/tools/skill_graphs/__init__.py`
- Create: `backend/app/services/deepagents_runtime/tools/skill_graphs/subprocess_runner.py`
- Create: `backend/app/services/deepagents_runtime/tools/skill_graphs/job_discovery_graph.py`
- Modify: `backend/app/services/deepagents_runtime/harness.py` (wrap the executor invocation with `workflow_thread_id`)
- Test: `tests/unit/test_deepagents_subprocess_runner.py`, `tests/unit/test_deepagents_job_discovery_graph.py`, `tests/unit/test_deepagents_skill_tool.py`

**Interfaces:**
- Consumes: Task 3 (`bind_tool_context` pattern, `_JsonPayload`); skill scripts (verified CLI contracts):
  - `browse.py <url> [--mode list|detail|interact|search|search-interact|click|parallel-fetch] [--out <dir>] [--max-pages N] [--max-cards N] ...`
  - `validate.py <candidates.json> [--strict] [--package] [--verify]`
  - `deduplicate.py <files...> [--out <merged.json>] [--no-verify] [--keep-garbage]`
  - `coverage_gate.py <candidates.json> [--pages <url>...] [--terminal-evidence <path>] [--expected-count N] [--manifest <path>]` → prints one JSON line
  - `normalize.py --title/--company/--text/--hash [--resp ...] [--req ...] [--json]`
- Produces:
  - `SKILL_DIR: Path` (subprocess_runner.py) — resolved `skill/job-discovery` directory.
  - `run_skill_script(script, cli_args="", stdin="", *, runner=None) -> str` (subprocess_runner.py) — whitelisted subprocess, never raises, injectable `runner` seam.
  - `build_job_discovery_graph(*, fetch_fn=None, script_runner=None, extract_fn=None) -> StateGraph` (job_discovery_graph.py) — nodes `fetch → extract → validate → dedup → coverage` over `JobDiscoveryWorkflowState` (`urls`, `pages`, `per_url_results`, `candidates`, `coverage`, `error`); `extract_fn: (pages) -> (candidates, error)` never raises — returns `error` text on failure.
  - `build_job_discovery_tool(*, fetch_fn=None, script_runner=None, extract_fn=None, checkpointer=None) -> StructuredTool` + `workflow_thread_id(thread: str)` context manager (skill_graphs/__init__.py) — thread = `f"{run_id}:{step_index}:workflow"`.
  - Harness change: executor node additionally wraps `agent.invoke` in `workflow_thread_id(...)`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_deepagents_subprocess_runner.py`:

```python
from __future__ import annotations

import subprocess
from pathlib import Path

from backend.app.services.deepagents_runtime.tools.skill_graphs.subprocess_runner import (
    SKILL_DIR,
    run_skill_script,
)


def test_skill_dir_resolves_to_skill_package() -> None:
    assert (SKILL_DIR / "SKILL.md").exists()
    assert (SKILL_DIR / "scripts" / "browse.py").exists()


def test_run_skill_script_rejects_unknown_scripts() -> None:
    def never_runs(*args, **kwargs):
        raise AssertionError("must not run")

    out = run_skill_script("rm", runner=never_runs)
    assert "ERROR" in out and "not allowed" in out


def test_run_skill_script_passes_through_stdout() -> None:
    captured = {}

    def fake_runner(
        script_path: Path,
        parts: list[str],
        *,
        cwd: Path,
        stdin: str | None,
        timeout: int,
    ) -> str:
        captured["path"] = script_path
        captured["cwd"] = cwd
        captured["parts"] = parts
        captured["stdin"] = stdin
        return "script stdout"

    out = run_skill_script("normalize", "--title 测试", stdin='{"x": 1}', runner=fake_runner)
    assert out == "script stdout"
    assert captured["parts"] == ["--title", "测试"]
    assert captured["cwd"] == SKILL_DIR
    assert captured["stdin"] == '{"x": 1}'


def test_run_skill_script_times_out_gracefully() -> None:
    def timing_out(*args, **kwargs):
        raise subprocess.TimeoutExpired("browse.py", timeout=900)

    out = run_skill_script("browse", runner=timing_out)
    assert "ERROR" in out and "timed out" in out
```

`tests/unit/test_deepagents_job_discovery_graph.py`:

```python
from __future__ import annotations

import json
from pathlib import Path

from backend.app.services.deepagents_runtime.tools.skill_graphs.job_discovery_graph import (
    build_job_discovery_graph,
)


def _fake_fetch(urls: list[str]) -> list[dict]:
    return [
        {
            "url": url,
            "source_url": url,
            "status": "succeeded",
            "content_hash": f"hash-{index}",
            "visible_text": "岗位：后端工程师\n任职要求：精通 Python",
        }
        for index, url in enumerate(urls)
    ]


def _fake_runner(script: str, cli_args: str = "", stdin: str = "") -> str:
    if script == "coverage_gate":
        # faithful to the real script: verified mirrors whether candidates exist
        parts = cli_args.split()
        candidates_path = Path(parts[0])
        candidates = json.loads(candidates_path.read_text(encoding="utf-8"))
        return json.dumps(
            {"verified": bool(candidates), "pages": 1, "candidates": len(candidates)}
        )
    if script == "validate":
        return json.dumps({"ok": True})
    if script == "deduplicate":
        parts = cli_args.split()
        out_path = Path(parts[parts.index("--out") + 1])
        out_path.write_text(json.dumps([{"title": "后端工程师"}]), encoding="utf-8")
        return "ok"
    return "{}"


def test_workflow_runs_end_to_end_with_seams() -> None:
    graph = build_job_discovery_graph(
        fetch_fn=_fake_fetch, script_runner=_fake_runner
    ).compile()
    final = graph.invoke({"urls": ["https://example.com/jobs"]})
    assert final["per_url_results"][0]["status"] == "succeeded"
    assert final["coverage"]["verified"] is True
    assert final["error"] is None


def test_fetch_failure_recorded_per_url_not_fatal() -> None:
    def fetch(urls: list[str]) -> list[dict]:
        return [{"url": url, "status": "failed", "error_code": "blocked"} for url in urls]

    graph = build_job_discovery_graph(fetch_fn=fetch, script_runner=_fake_runner).compile()
    final = graph.invoke({"urls": ["https://a.com", "https://b.com"]})
    assert final["per_url_results"][0]["status"] == "failed"
    assert final["candidates"] == []
    assert final["coverage"]["verified"] is False


def test_extract_failure_records_error() -> None:
    def failing_extract(pages: list[dict]) -> tuple[list[dict], str]:
        return [], "extract failed: evidence not found"

    graph = build_job_discovery_graph(
        fetch_fn=_fake_fetch,
        script_runner=_fake_runner,
        extract_fn=failing_extract,
    ).compile()
    final = graph.invoke({"urls": ["https://example.com/jobs"]})
    assert final["error"] == "extract failed: evidence not found"
    assert final["candidates"] == []


def test_default_extract_handles_missing_evidence(monkeypatch) -> None:
    import backend.app.services.deepagents_runtime.tools.skill_graphs.job_discovery_graph as jdg
    from backend.app.services.career_skills.job_discovery import PublicJobFetchError

    def raising_batch(context, payload):
        raise PublicJobFetchError("observed_evidence_not_found")

    monkeypatch.setattr(jdg, "extract_observed_job_details_batch", raising_batch)
    graph = build_job_discovery_graph(
        fetch_fn=_fake_fetch, script_runner=_fake_runner
    ).compile()
    final = graph.invoke({"urls": ["https://example.com/jobs"]})
    assert final["error"] is not None
    assert final["candidates"] == []


def test_script_error_recorded_not_fatal() -> None:
    def failing_runner(script: str, cli_args: str = "", stdin: str = "") -> str:
        if script == "validate":
            return "ERROR: validation failed"
        return _fake_runner(script, cli_args, stdin)

    graph = build_job_discovery_graph(
        fetch_fn=_fake_fetch, script_runner=failing_runner
    ).compile()
    final = graph.invoke({"urls": ["https://example.com/jobs"]})
    assert final["error"] is not None
    assert final["coverage"] is not None
```

`tests/unit/test_deepagents_skill_tool.py` (contract + thread-binding; uses its own fakes — copy them here, do not import from the graph test file):

```python
from __future__ import annotations

import json
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver

from backend.app.services.deepagents_runtime.tools.skill_graphs import (
    build_job_discovery_tool,
    workflow_thread_id,
)


def _fake_fetch(urls: list[str]) -> list[dict]:
    return [
        {
            "url": url,
            "source_url": url,
            "status": "succeeded",
            "content_hash": f"hash-{index}",
            "visible_text": "岗位：后端工程师\n任职要求：精通 Python",
        }
        for index, url in enumerate(urls)
    ]


def _fake_extract(pages: list[dict]) -> tuple[list[dict], None]:
    return (
        [
            {
                "title": "后端工程师",
                "company": "示例公司",
                "jd_url": pages[0]["url"],
                "content_hash": pages[0]["content_hash"],
            }
        ],
        None,
    )


def _fake_runner(script: str, cli_args: str = "", stdin: str = "") -> str:
    if script == "coverage_gate":
        parts = cli_args.split()
        candidates = json.loads(Path(parts[0]).read_text(encoding="utf-8"))
        return json.dumps(
            {"verified": bool(candidates), "pages": 1, "candidates": len(candidates)}
        )
    if script == "validate":
        return json.dumps({"ok": True})
    if script == "deduplicate":
        parts = cli_args.split()
        out_path = Path(parts[parts.index("--out") + 1])
        out_path.write_text(json.dumps([{"title": "后端工程师"}]), encoding="utf-8")
        return "ok"
    return "{}"


def test_job_discovery_tool_returns_partial_results_contract() -> None:
    tool = build_job_discovery_tool(
        fetch_fn=_fake_fetch, script_runner=_fake_runner, extract_fn=_fake_extract
    )
    # no thread bound -> config = {} branch (single-shot, no checkpointer)
    out = json.loads(
        tool.invoke(json.dumps({"payload": json.dumps(["https://example.com/jobs"])}))
    )
    assert set(out) == {"per_url_results", "candidates", "coverage"}
    assert out["per_url_results"][0]["status"] == "succeeded"
    assert out["candidates"][0]["title"] == "后端工程师"
    assert out["coverage"]["verified"] is True


def test_job_discovery_tool_threaded_invocation() -> None:
    tool = build_job_discovery_tool(
        fetch_fn=_fake_fetch,
        script_runner=_fake_runner,
        extract_fn=_fake_extract,
        checkpointer=InMemorySaver(),
    )
    with workflow_thread_id("run-1:0:workflow"):
        first = json.loads(
            tool.invoke(json.dumps({"payload": json.dumps(["https://a.com"])}))
        )
        second = json.loads(
            tool.invoke(json.dumps({"payload": json.dumps(["https://b.com"])}))
        )
    # thread config branch: second invoke re-runs from START with the new
    # input (last checkpoint is complete), so it fetches the new URL
    assert [r["url"] for r in first["per_url_results"]] == ["https://a.com"]
    assert [r["url"] for r in second["per_url_results"]] == ["https://b.com"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_deepagents_subprocess_runner.py tests/unit/test_deepagents_job_discovery_graph.py tests/unit/test_deepagents_skill_tool.py -q`
Expected: FAIL — `ModuleNotFoundError: skill_graphs`.

- [ ] **Step 3: Implement the subprocess runner**

`backend/app/services/deepagents_runtime/tools/skill_graphs/subprocess_runner.py`:

```python
"""Whitelisted execution channel for the job-discovery skill scripts.

Security: this is the ONLY way skill scripts may run (spec §4.2).  It is
deliberately NOT LocalShellBackend (which grants arbitrary shell): only the
nine allowlisted scripts run, cwd is pinned to the skill directory so
relative ``output/`` paths resolve, and an injectable ``runner`` seam keeps
unit tests deterministic.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[6]
SKILL_DIR = _PROJECT_ROOT / "skill" / "job-discovery"

_ALLOWED_SCRIPTS = frozenset(
    {
        "browse",
        "validate",
        "normalize",
        "deduplicate",
        "ocr_image",
        "state",
        "read_evidence",
        "write_candidates",
        "coverage_gate",
    }
)
_SCRIPT_TIMEOUT_SEC = 900

# runner: (script_path, parts, *, cwd, stdin, timeout) -> stdout text
_ScriptRunner = Callable[[Path, list[str], Path, str | None, int], str]


def _default_runner(
    script_path: Path,
    parts: list[str],
    *,
    cwd: Path,
    stdin: str | None,
    timeout: int,
) -> str:
    cmd = [sys.executable, str(script_path), *parts]
    child_env = {
        **os.environ,
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
    }
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
        input=stdin,
        env=child_env,
    )
    out = proc.stdout or ""
    if proc.stderr:
        out += "\n[stderr]\n" + proc.stderr[-2000:]
    return out


def run_skill_script(
    script: str,
    cli_args: str = "",
    stdin: str = "",
    *,
    runner: _ScriptRunner | None = None,
) -> str:
    """Run one allowlisted skill script; never raises, returns stdout/error."""
    if script not in _ALLOWED_SCRIPTS:
        return f"ERROR: script not allowed: {script}"
    script_path = SKILL_DIR / "scripts" / f"{script}.py"
    if not script_path.exists():
        return f"ERROR: script not found at {script_path}"
    try:
        parts = shlex.split(cli_args, posix=(os.name != "nt")) if cli_args else []
    except ValueError as exc:
        return f"ERROR: could not parse cli_args {cli_args!r}: {exc}"
    try:
        return (runner or _default_runner)(
            script_path,
            parts,
            cwd=SKILL_DIR,
            stdin=stdin if stdin else None,
            timeout=_SCRIPT_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        return f"ERROR: {script} timed out after {_SCRIPT_TIMEOUT_SEC}s"
    except OSError as exc:
        return f"ERROR: {script} could not start: {exc}"
```

- [ ] **Step 4: Implement the workflow subgraph**

`backend/app/services/deepagents_runtime/tools/skill_graphs/job_discovery_graph.py`:

```python
"""SKILL.md six-phase job-discovery workflow as a LangGraph subgraph.

Nodes mirror the validated skill behavior (browse → extract → validate →
deduplicate → coverage_gate).  Mechanical phases are fully deterministic;
the only LLM contact is the optional low-confidence extraction gate
(spec §4.3).  A per-URL fetch failure is recorded in ``per_url_results``
and never aborts the run (layered failure recovery, spec §4.2).  Compiling
with a checkpointer makes a mid-crawl crash resume from the last URL
instead of re-fetching.

Deterministic phases run the allowlisted skill scripts through
``run_skill_script`` with candidates exchanged via a temp JSON file under
the skill directory (scripts take file paths, matching their real CLI).
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.career_skills.job_discovery import (
    ExtractObservedJobDetailsBatchInput,
    FetchPublicJobPagesInput,
    PublicJobFetchError,
    extract_observed_job_details_batch,
    fetch_public_job_pages,
)
from backend.app.services.deepagents_runtime.tools.skill_graphs.subprocess_runner import (
    SKILL_DIR,
    run_skill_script,
)


class JobDiscoveryWorkflowState(TypedDict):
    urls: list[str]
    pages: list[dict[str, Any]]
    per_url_results: list[dict[str, Any]]
    candidates: list[dict[str, Any]]
    coverage: dict[str, Any]
    error: str | None


def _default_fetch(urls: list[str]) -> list[dict[str, Any]]:
    """requests fast-path via the reviewed registry handler (the Playwright
    fallback selected by SKILL.md lives behind the same handler)."""
    output = fetch_public_job_pages(
        ToolContext(user_id="", run_id="", metadata={}),
        FetchPublicJobPagesInput(urls=urls[:10]),
    )
    return [
        {
            "url": page.source_url,
            "status": "succeeded",
            "content_hash": page.content_hash,
            "title": page.title,
            "visible_text": page.visible_text,
        }
        for page in output.pages
    ] + [
        {
            "url": failure.source_url,
            "status": "failed",
            "error_code": failure.error_code,
        }
        for failure in output.failures
    ]


def _write_candidates(candidates: list[dict[str, Any]]) -> Path:
    workdir = Path(tempfile.mkdtemp(dir=SKILL_DIR))
    path = workdir / "candidates.json"
    path.write_text(json.dumps(candidates, ensure_ascii=False), encoding="utf-8")
    return path


def _default_extract(
    pages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str | None]:
    """Extract from succeeded pages via the reviewed career_skills engine.

    Evidence binding: each page is re-registered in the ToolContext metadata
    as ``observed:<content_hash>`` so the handler's
    ``_find_observed_evidence`` lookup resolves (spec §4.2).
    """
    metadata_pages = [
        {
            "artifact_id": f"observed:{page['content_hash']}",
            "source_url": page.get("source_url"),
            "content_hash": page.get("content_hash"),
            "visible_text": page.get("visible_text", ""),
        }
        for page in pages
    ]
    try:
        output = extract_observed_job_details_batch(
            ToolContext(
                user_id="",
                run_id="",
                metadata={"observed_public_evidence": metadata_pages},
            ),
            ExtractObservedJobDetailsBatchInput(
                artifact_ids=[page["content_hash"] for page in pages][:10]
            ),
        )
    except PublicJobFetchError as exc:
        return [], f"extract failed: {exc}"
    return [detail.model_dump(mode="json") for detail in output.details], None


def build_job_discovery_graph(
    *,
    fetch_fn=None,
    script_runner=None,
    extract_fn=None,
) -> StateGraph:
    """Assemble the workflow graph with injectable seams for tests."""
    fetch_fn = fetch_fn or _default_fetch
    script_runner = script_runner or run_skill_script
    extract_fn = extract_fn or _default_extract

    def fetch_node(state: JobDiscoveryWorkflowState) -> dict[str, Any]:
        pages = fetch_fn(state["urls"])
        return {
            "pages": pages,
            "per_url_results": [
                {
                    "url": page.get("url"),
                    "status": page.get("status", "failed"),
                    "error_code": page.get("error_code"),
                    "content_hash": page.get("content_hash"),
                }
                for page in pages
            ],
        }

    def extract_node(state: JobDiscoveryWorkflowState) -> dict[str, Any]:
        pages = [
            page
            for page in state.get("pages", [])
            if page.get("status") == "succeeded" and page.get("content_hash")
        ]
        if not pages:
            return {"candidates": []}
        candidates, error = extract_fn(pages)
        update: dict[str, Any] = {"candidates": candidates}
        if error is not None:
            update["error"] = error
        return update

    def validate_node(state: JobDiscoveryWorkflowState) -> dict[str, Any]:
        if not state["candidates"]:
            return {}
        with tempfile.TemporaryDirectory(dir=SKILL_DIR) as workdir:
            path = _write_candidates(state["candidates"])
            out = script_runner("validate", str(path))
        if "ERROR" in out:
            return {"error": f"validate failed: {out[:500]}"}
        return {}

    def dedup_node(state: JobDiscoveryWorkflowState) -> dict[str, Any]:
        if not state["candidates"]:
            return {}
        with tempfile.TemporaryDirectory(dir=SKILL_DIR) as workdir:
            work = Path(workdir)
            src = work / "candidates.json"
            src.write_text(
                json.dumps(state["candidates"], ensure_ascii=False), encoding="utf-8"
            )
            merged = work / "merged.json"
            out = script_runner("deduplicate", f"{src} --out {merged}")
            if "ERROR" in out:
                return {"error": f"deduplicate failed: {out[:500]}"}
            if merged.exists():
                try:
                    merged_candidates = json.loads(merged.read_text(encoding="utf-8"))
                except ValueError:
                    return {"error": "deduplicate output unparsable"}
                if isinstance(merged_candidates, list):
                    return {"candidates": merged_candidates}
                if (
                    isinstance(merged_candidates, dict)
                    and isinstance(merged_candidates.get("candidates"), list)
                ):
                    return {"candidates": merged_candidates["candidates"]}
                return {"error": "deduplicate output has no candidates list"}
        return {"error": "deduplicate produced no merged file"}

    def coverage_node(state: JobDiscoveryWorkflowState) -> dict[str, Any]:
        pages = [
            page for page in state.get("pages", []) if page.get("status") == "succeeded"
        ]
        page_urls = " ".join(page.get("url", "") for page in pages)
        with tempfile.TemporaryDirectory(dir=SKILL_DIR) as workdir:
            path = _write_candidates(state["candidates"])
            out = script_runner("coverage_gate", f"{path} --pages {page_urls}".strip())
        try:
            coverage = json.loads(out)
        except ValueError:
            coverage = {"verified": False, "error": "unparsable coverage_gate output"}
        return {"coverage": coverage}

    graph = StateGraph(JobDiscoveryWorkflowState)
    graph.add_node("fetch", fetch_node)
    graph.add_node("extract", extract_node)
    graph.add_node("validate", validate_node)
    graph.add_node("dedup", dedup_node)
    graph.add_node("coverage", coverage_node)
    graph.add_edge(START, "fetch")
    graph.add_edge("fetch", "extract")
    graph.add_edge("extract", "validate")
    graph.add_edge("validate", "dedup")
    graph.add_edge("dedup", "coverage")
    graph.add_edge("coverage", END)
    return graph
```

(Note: `_write_candidates` writes into `tempfile.mkdtemp(dir=SKILL_DIR)` while the `TemporaryDirectory` contexts are separate — a small directory leak per invocation is acceptable at eval scope; tighten to a single shared workdir if the live parity gate in Task 6 requires it.)

- [ ] **Step 5: Implement the @tool wrapper**

`backend/app/services/deepagents_runtime/tools/skill_graphs/__init__.py`:

```python
"""Skill workflow subgraphs wrapped as @tools for the Executor."""

from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel

from backend.app.services.deepagents_runtime.tools.skill_graphs.job_discovery_graph import (
    build_job_discovery_graph,
)

_workflow_thread: ContextVar[str | None] = ContextVar(
    "deepagents_workflow_thread", default=None
)


@contextmanager
def workflow_thread_id(thread: str):
    """Bind the workflow subgraph thread for one executor invocation."""
    token = _workflow_thread.set(thread)
    try:
        yield
    finally:
        _workflow_thread.reset(token)


class _JsonPayload(BaseModel):
    payload: str


def build_job_discovery_tool(
    *,
    fetch_fn=None,
    script_runner=None,
    extract_fn=None,
    checkpointer: Any = None,
) -> StructuredTool:
    """Wrap the compiled job-discovery workflow as a single @tool.

    Thread = ``f"{run_id}:{step_index}:workflow"`` (bound by the harness via
    ``workflow_thread_id``), so a mid-crawl crash resumes from the last URL
    instead of re-fetching.  Returns the structured partial-results
    contract: ``{per_url_results, candidates, coverage}``.
    """

    graph = build_job_discovery_graph(
        fetch_fn=fetch_fn, script_runner=script_runner, extract_fn=extract_fn
    ).compile(checkpointer=checkpointer)

    def run(urls_json: str) -> str:
        urls = json.loads(urls_json)
        thread = _workflow_thread.get()
        config = {"configurable": {"thread_id": thread}} if thread else {}
        final = graph.invoke({"urls": urls}, config)
        return json.dumps(
            {
                "per_url_results": final.get("per_url_results", []),
                "candidates": final.get("candidates", []),
                "coverage": final.get("coverage", {"verified": False}),
            },
            ensure_ascii=False,
        )

    return StructuredTool.from_function(
        func=run,
        name="run-job-discovery-workflow",
        description=(
            "按 SKILL.md 六阶段工作流批量处理招聘 URL：抓取页面、正则提取 JD"
            "（低置信才用 LLM）、校验、去重、覆盖门控。输入 JSON 数组"
            "（用户给出的官方招聘 URL），返回 per_url_results + candidates + coverage。"
        ),
        args_schema=_JsonPayload,
    )
```

In `harness.py`, wrap the executor invocation (nest inside the existing `bind_tool_context` block):

```python
        try:
            with bind_tool_context(tool_ctx):
                with workflow_thread_id(
                    _agent_thread(state["run_id"], state["step_index"], "workflow")
                ):
                    with current_budgets(budgets):
                        result = agent.invoke(
                            {"messages": [HumanMessage(step.objective)]},
                            {"configurable": {"thread_id": _agent_thread(state["run_id"], state["step_index"], "executor")}},
                        )
        except TurnBudgetExhausted as exc:
            return _degrade(state, str(exc))
```

with `from backend.app.services.deepagents_runtime.tools.skill_graphs import workflow_thread_id` added to the harness imports.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_deepagents_subprocess_runner.py tests/unit/test_deepagents_job_discovery_graph.py tests/unit/test_deepagents_skill_tool.py tests/unit/test_deepagents_harness.py -q`
Expected: PASS.

- [ ] **Step 7: Full suite + ruff + commit**

Commit:

```bash
git add backend/app/services/deepagents_runtime/tools/skill_graphs backend/app/services/deepagents_runtime/harness.py tests/unit/test_deepagents_subprocess_runner.py tests/unit/test_deepagents_job_discovery_graph.py tests/unit/test_deepagents_skill_tool.py
git commit -m "feat(deepagents-runtime): P3 job-discovery workflow subgraph as @tool

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 5: P4 — MySQL sink (flush_run)

**Files:**
- Create: `backend/app/services/deepagents_runtime/checkpoints/sink.py`
- Modify: `backend/app/services/deepagents_runtime/__init__.py` (export `flush_run_with_retry`)
- Modify: `backend/app/services/deepagents_runtime/harness.py` (flush hook — spec §6.2: `session_factory` constructor param, `_flush_if_configured` method + module-level `_evidence_artifacts`, call sites in `run()`/`resume()`)
- Test: `tests/unit/test_deepagents_sink.py`

**Interfaces:**
- Consumes: Task 1 models (`DeepAgentsRun`, `DeepAgentsArtifact`); `db_session` fixture from `tests/conftest.py` (in-memory SQLite, all ORM tables created); `RunStatus` from domain.agent_runtime; Task 2 `DeepAgentsHarness` + `DeepAgentsBudgets`.
- Produces:
  - `flush_run(session, *, run_id, user_id, thread_id, goal, allowed_skills, budget_dict, status, plan_json, decisions, error_code, final_summary, started_at, finished_at, artifacts: list[dict]) -> None` — idempotent UPSERT of the run + batch upsert of artifacts in ONE transaction (spec §6.2).
  - `flush_run_with_retry(session_factory, *, retries=3, backoff_seconds=0.5, **run_fields) -> None` — retries on any DB error, raises the last error after exhaustion.
  - `DeepAgentsHarness(..., session_factory: Callable[[], Any] | None = None)` (harness.py) — when wired, `run()`/`resume()` flush the completed snapshot to MySQL after every invoke (spec §6.2); `None` (default) skips flushing.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_deepagents_sink.py`:

```python
from __future__ import annotations

import json

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from backend.app.db.models import DeepAgentsArtifact, DeepAgentsRun
from backend.app.services.agent_runtime.schemas import AgentTaskRequest
from backend.app.services.deepagents_runtime.budgets import DeepAgentsBudgets
from backend.app.services.deepagents_runtime.checkpoints.sink import (
    flush_run,
    flush_run_with_retry,
)
from backend.app.services.deepagents_runtime.harness import DeepAgentsHarness

_RUN = dict(
    run_id="run-1",
    user_id="user-1",
    thread_id="run-1",
    goal="帮我找后端岗位",
    allowed_skills=["job-discovery"],
    budget_dict={"max_agent_turns": 12},
    status="succeeded",
    plan_json={"steps": []},
    decisions=[{"role": "planner", "decision": "PLANNED"}],
    error_code=None,
    final_summary="ok",
    started_at=1723000000.0,
    finished_at=1723000060.0,
)

_ARTIFACTS = [
    {
        "artifact_id": "abc123",
        "kind": "public_page_evidence",
        "source_url": "https://example.com/jobs",
        "content_hash": "abc123",
        "payload": {"text": "excerpt"},
    }
]


def test_flush_run_inserts_run_and_artifacts(db_session) -> None:
    flush_run(db_session, artifacts=_ARTIFACTS, **_RUN)
    run = db_session.query(DeepAgentsRun).filter_by(thread_id="run-1").one()
    assert run.status.value == "succeeded"
    # naive UTC: the in-memory SQLite fixture rejects tz-aware datetimes
    assert run.started_at is not None and run.started_at.tzinfo is None
    assert run.finished_at is not None and run.finished_at.tzinfo is None
    artifacts = db_session.query(DeepAgentsArtifact).filter_by(run_id="run-1").all()
    assert len(artifacts) == 1
    assert artifacts[0].artifact_id == "abc123"


def test_flush_run_is_idempotent_upsert(db_session) -> None:
    flush_run(db_session, artifacts=_ARTIFACTS, **_RUN)
    flush_run(db_session, artifacts=_ARTIFACTS, **_RUN)  # second flush must not duplicate
    run = db_session.query(DeepAgentsRun).filter_by(thread_id="run-1").one()
    artifacts = db_session.query(DeepAgentsArtifact).filter_by(run_id="run-1").all()
    assert len(artifacts) == 1


def test_flush_run_with_retry_retries_transient_failures(db_session) -> None:
    attempts = {"n": 0}

    def flaky_factory():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("transient")
        return db_session

    flush_run_with_retry(
        flaky_factory, retries=3, backoff_seconds=0, **_RUN, artifacts=_ARTIFACTS
    )
    assert attempts["n"] == 2
    assert db_session.query(DeepAgentsRun).filter_by(thread_id="run-1").count() == 1


def test_flush_run_with_retry_gives_up_and_raises(db_session) -> None:
    def always_failing():
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        flush_run_with_retry(
            always_failing, retries=2, backoff_seconds=0, **_RUN, artifacts=[]
        )


def test_flush_run_accepts_none_timestamps(db_session) -> None:
    # interrupted runs flush with no timestamps -> NULL row fields
    run_fields = {**_RUN, "started_at": None, "finished_at": None}
    flush_run(db_session, artifacts=[], **run_fields)
    run = db_session.query(DeepAgentsRun).filter_by(thread_id="run-1").one()
    assert run.started_at is None
    assert run.finished_at is None


# --- harness flush hook (spec §6.2): same scripted helpers as Task 2 ----

PLAN_FLUSH_JSON = json.dumps(
    {
        "plan_id": "plan-flush",
        "steps": [
            {
                "step_id": "discover",
                "objective": "抓取并提取 JD",
                "allowed_skills": ["job-discovery"],
                "success_criteria": [],
                "requires_verification": True,
            }
        ],
    },
    ensure_ascii=False,
)
VERIFIER_PASS_JSON = json.dumps(
    {"decision": "PASS", "rationale": "ok"}, ensure_ascii=False
)


@tool
def stub_discovery_tool(payload: str) -> str:
    """Test stub: return one piece of tool-produced evidence as observation JSON."""
    return json.dumps(
        {
            "tool_name": "stub",
            "status": "succeeded",
            "output": {
                "source_url": "https://example.com/jobs",
                "content_hash": "abc123",
                "candidates": [{"title": "后端工程师"}],
            },
        }
    )


def _scripted_factory(scripted: dict[str, list[str] | list[AIMessage]]):
    """Return a model_factory consuming one FakeListChatModel per role."""
    def factory(role: str) -> FakeListChatModel:
        return FakeListChatModel(responses=list(scripted[role]))

    return factory


def _request(**overrides) -> AgentTaskRequest:
    values = dict(
        goal="帮我找后端岗位",
        allowed_skills=["job-discovery"],
        context={"candidate_urls": ["https://example.com/jobs"]},
    )
    values.update(overrides)
    return AgentTaskRequest(**values)


def test_harness_flushes_completed_run(db_session) -> None:
    # full run() invoke with a wired session_factory -> row + artifact in MySQL
    harness = DeepAgentsHarness(
        model_factory=_scripted_factory(
            {
                "planner": [PLAN_FLUSH_JSON],
                "executor": [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "stub_discovery_tool",
                                "args": {"payload": "{}"},
                                "id": "call_1",
                            }
                        ],
                    ),
                    "evidence collected",
                ],
                "verifier": [VERIFIER_PASS_JSON],
            }
        ),
        tool_factory=lambda skill: (
            [stub_discovery_tool] if skill == "job-discovery" else []
        ),
        checkpointer=InMemorySaver(),
        session_factory=lambda: db_session,
    )
    final = harness.run(_request(), run_id="run-flush")
    assert final["run_status"] == "succeeded"
    run = db_session.query(DeepAgentsRun).filter_by(thread_id="run-flush").one()
    assert run.status.value == "succeeded"
    assert run.final_summary == "所有步骤已通过验证"
    artifacts = db_session.query(DeepAgentsArtifact).filter_by(run_id="run-flush").all()
    assert len(artifacts) == 1
    assert artifacts[0].content_hash == "abc123"
    assert artifacts[0].payload_json == {"text": ""}  # stub output has no visible_text


def test_harness_flush_hook_evidence_branches(db_session) -> None:
    # direct _flush_if_configured call: artifact_id/visible_text truthy + falsy
    harness = DeepAgentsHarness(
        model_factory=lambda role: None,
        checkpointer=InMemorySaver(),
        session_factory=lambda: db_session,
    )
    final = {
        "run_id": "run-direct",
        "user_id": "user-1",
        "goal": "帮我找后端岗位",
        "allowed_skills": ["job-discovery"],
        "budget": DeepAgentsBudgets(max_agent_turns=12).to_dict(),
        "run_status": "waiting_user",
        "plan_json": {"steps": []},
        "decisions": [{"role": "verifier", "decision": "NEED_USER"}],
        "error_code": "needs_user",
        "final_summary": None,
        "finished_at": 1723000060.0,
        "evidence_store": [
            {
                "artifact_id": "hash-1",
                "source_url": "https://a.com",
                "content_hash": "hash-1",
                "visible_text": "JD 文本",
            },
            {"source_url": "https://b.com", "content_hash": "hash-2"},
        ],
    }
    harness._flush_if_configured(final, started_at=1723000000.0)
    run = db_session.query(DeepAgentsRun).filter_by(thread_id="run-direct").one()
    assert run.status.value == "waiting_user"
    assert run.error_code == "needs_user"
    artifacts = db_session.query(DeepAgentsArtifact).filter_by(run_id="run-direct").all()
    assert len(artifacts) == 2
    by_hash = {a.content_hash: a for a in artifacts}
    assert by_hash["hash-1"].artifact_id == "hash-1"  # artifact_id truthy branch
    assert by_hash["hash-1"].payload_json == {"text": "JD 文本"}  # visible_text truthy
    assert by_hash["hash-2"].artifact_id == "hash-2"  # artifact_id or content_hash
    assert by_hash["hash-2"].payload_json == {"text": ""}  # visible_text falsy branch
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_deepagents_sink.py -q`
Expected: FAIL — `ModuleNotFoundError: sink`.

- [ ] **Step 3: Implement the sink**

`backend/app/services/deepagents_runtime/checkpoints/sink.py`:

```python
"""MySQL sink: authoritative completion snapshots (spec §6.2).

Executed once per run completion (succeeded / failed / waiting_user /
cancelled).  Idempotent: the run row and each artifact row are upserted by
primary key so a retried flush never duplicates records.  Single
transaction per flush.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.db.models import DeepAgentsArtifact, DeepAgentsRun


def _epoch_to_dt(value: float | None) -> datetime | None:
    if value is None:
        return None
    # naive UTC: the in-memory SQLite fixture rejects tz-aware datetimes
    return datetime.fromtimestamp(value, tz=timezone.utc).replace(tzinfo=None)


def flush_run(
    session: Session,
    *,
    run_id: str,
    user_id: str,
    thread_id: str,
    goal: str,
    allowed_skills: list[str],
    budget_dict: dict[str, Any],
    status: str,
    plan_json: dict[str, Any] | None,
    decisions: list[dict[str, Any]],
    error_code: str | None,
    final_summary: str | None,
    started_at: float | None,
    finished_at: float | None,
    artifacts: list[dict[str, Any]],
) -> None:
    """Upsert the run snapshot and its artifacts in one transaction.

    ``status`` accepts a plain string or a ``RunStatus``; the column type
    coerces both.  ``started_at``/``finished_at`` are epoch floats (channel
    values are JSON-safe floats) and are converted here.
    """
    run = session.get(DeepAgentsRun, run_id)
    if run is None:
        run = DeepAgentsRun(id=run_id, thread_id=thread_id)
        session.add(run)
    run.user_id = user_id
    run.goal = goal
    run.allowed_skills_json = allowed_skills
    run.budget_json = budget_dict
    run.status = status
    run.plan_json = plan_json
    run.decisions_json = decisions
    run.error_code = error_code
    run.final_summary = final_summary
    run.started_at = _epoch_to_dt(started_at)
    run.finished_at = _epoch_to_dt(finished_at)

    for artifact in artifacts:
        artifact_id = artifact["artifact_id"]
        existing = session.execute(
            select(DeepAgentsArtifact).where(
                DeepAgentsArtifact.run_id == run_id,
                DeepAgentsArtifact.artifact_id == artifact_id,
            )
        ).scalar_one_or_none()
        if existing is None:
            existing = DeepAgentsArtifact(
                id=str(uuid.uuid4()), run_id=run_id, artifact_id=artifact_id
            )
            session.add(existing)
        existing.kind = artifact["kind"]
        existing.source_url = artifact.get("source_url")
        existing.content_hash = artifact["content_hash"]
        existing.payload_json = artifact["payload"]
    session.commit()


def flush_run_with_retry(
    session_factory: Callable[[], Session],
    *,
    retries: int = 3,
    backoff_seconds: float = 0.5,
    **run_fields: Any,
) -> None:
    """Flush with retry+backoff; raises the last error after exhaustion."""
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            with session_factory() as session:
                flush_run(session, **run_fields)
            return
        except Exception as exc:  # noqa: BLE001 - external DB errors
            last_error = exc
            if attempt < retries - 1:
                time.sleep(backoff_seconds * (2**attempt))
    if last_error is not None:
        raise last_error
```

- [ ] **Step 4: Wire the flush hook into the harness (spec §6.2)**

The harness must flush a completed snapshot to MySQL after every `run()` /
`resume()`. Add the `session_factory` constructor param (default `None` =
flushing disabled — unit tests and the old runtime's construction sites
stay untouched), the `_flush_if_configured` method, the module-level
`_evidence_artifacts` mapper, and call sites in `run()`/`resume()`.

`backend/app/services/deepagents_runtime/harness.py` — top imports, next to
the existing harness imports (no circular import: `checkpoints.sink` imports
only `db.models` + sqlalchemy):

```python
from backend.app.services.deepagents_runtime.checkpoints.sink import flush_run_with_retry
```

Constructor — extend the signature and body (Task 2, line ~1517):

```python
    def __init__(
        self,
        *,
        model_factory: Callable[[str], Any],
        tool_factory: Callable[[str], Sequence[Any]] | None = None,
        checkpointer: Any = None,
        session_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._model_factory = model_factory
        self._tool_factory = tool_factory
        self._checkpointer = checkpointer
        self._session_factory = session_factory
        self._tracker = DuplicateCallTracker()
        self._graph = self._build_graph()
```

Module level, next to `_project_tool_observations`:

```python
def _evidence_artifacts(evidence_store: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map evidence-store entries to artifact rows (bounded, sanitized).

    Evidence entries always carry ``content_hash`` (only tool-produced
    evidence is stored); ``artifact_id`` defaults to the hash and
    ``visible_text`` is excerpted to 1,200 chars.
    """
    return [
        {
            "artifact_id": item.get("artifact_id") or item["content_hash"],
            "kind": "public_page_evidence",
            "source_url": item.get("source_url"),
            "content_hash": item["content_hash"],
            "payload": {"text": (item.get("visible_text") or "")[:1200]},
        }
        for item in evidence_store
    ]
```

Method, after `_finalize_node`:

```python
    def _flush_if_configured(self, state: dict[str, Any], started_at: float) -> None:
        """Flush the completed snapshot to MySQL when a factory is wired."""
        if self._session_factory is None:
            return
        flush_run_with_retry(
            self._session_factory,
            run_id=state["run_id"],
            user_id=state["user_id"],
            thread_id=state["run_id"],
            goal=state["goal"],
            allowed_skills=state["allowed_skills"],
            budget_dict=state["budget"],
            # graph terminal states always set run_status (never None here)
            status=state["run_status"],
            plan_json=state["plan_json"],
            decisions=state["decisions"],
            error_code=state["error_code"],
            final_summary=state["final_summary"],
            started_at=started_at,
            finished_at=state["finished_at"],
            artifacts=_evidence_artifacts(state["evidence_store"]),
        )
```

Call sites — `run()` (Task 2, line ~1578) — capture `started_at` and flush
before returning:

```python
        started_at = time.time()
        final = self._graph.invoke(initial, {"configurable": {"thread_id": run_id}})
        self._flush_if_configured(final, started_at)
        return dict(final)
```

`resume()` (Task 2, line ~1585) — same:

```python
        started_at = time.time()
        final = self._graph.invoke(
            {"budget": budgets.to_dict()},
            {"configurable": {"thread_id": run_id}},
        )
        self._flush_if_configured(final, started_at)
        return dict(final)
```

Note: `time` and `Callable`/`Any`/`Sequence` are already imported by Task 2's
harness.py. Task 2's existing tests now also execute the new `resume()`
call-site statement and the `self._session_factory is None` early-return
branch (they construct the harness without a factory); the two flush tests
above cover the wired flush branch and both `_evidence_artifacts` `or`
branches. All branches are covered by the combined suite.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_deepagents_sink.py -q`
Expected: PASS (all 7 sink tests + 2 harness flush tests).

- [ ] **Step 6: Full suite + ruff + commit**

Commit:

```bash
git add backend/app/services/deepagents_runtime/checkpoints/sink.py backend/app/services/deepagents_runtime/__init__.py backend/app/services/deepagents_runtime/harness.py tests/unit/test_deepagents_sink.py
git commit -m "feat(deepagents-runtime): P4 MySQL sink with idempotent flush_run

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 6: P5 — comparative eval, parity gate, docs, coverage close-out

**Files:**
- Create: `backend/app/services/deepagents_runtime/eval/__init__.py`
- Create: `backend/app/services/deepagents_runtime/eval/compare_runner.py`
- Create: `tests/manual/run_deepagents_parity.py`
- Modify: `CLAUDE.md` (security gate #5 exception clause from spec §12; new "DeepAgents Runtime" section; doc-table row)
- Test: `tests/unit/test_deepagents_compare_runner.py`

**Interfaces:**
- Consumes: Task 2/3/5 (`DeepAgentsHarness`, `DeepAgentsBudgets`, `build_skill_tools`, `flush_run_with_retry`, `create_checkpointer`); legacy runtime entry (verified): `build_agent_model_gateway(settings)` + `PlannerAgent(gateway, tools)` / `ExecutorAgent(gateway, tools)` / `VerifierAgent(gateway, tools)` + `AgentRuntime(planner=, executor=, verifier=, agent_version="pev-1")` + `AgentRunService(settings, runtime=).create_run(db, user_id=, task=) -> AgentRunResult(run_id, status: RunStatus, summary, error_code)`; DB models `AgentRun/AgentStep/AgentTurn/AgentPlan/AgentEvent`; question docs `tests/question/Q###.json` schema: `{id, question, meta: {skills: [...], complexity, ...}, profile, reference_answer, optional chain}`; baseline records `tests/manual/_skill_ten_url_<slug>.json` schema: `{slug, status, candidate_count, unique_listing_count, coverage_verified, ...}`; `SessionLocal` from `backend.app.db.session`; `load_project_env` from `backend.app.services.agent_runtime.provider_config`.
- Produces:
  - `Question` dataclass `(id, goal, allowed_skills, context)`; `RunMetrics` dataclass `(status, steps, turns, tool_calls, replans, wall_clock_s, error_code)` (compare_runner.py).
  - `run_legacy_question(question, *, settings, session_factory, runner=None) -> RunMetrics` — `runner: (question, settings, session_factory) -> result-like` (attributes `run_id`, `status: RunStatus`, `error_code`); defaults to the real AgentRunService assembly (compare_runner.py).
  - `run_deepagents_question(question, *, settings, run_id, harness=None) -> RunMetrics` — `harness: .run(request, *, run_id, budgets=None) -> final dict` (keys `run_status`, `budget`, `plan_json`, `error_code`); defaults to real ChatOpenAI + DeepAgentsHarness (compare_runner.py).
  - `summarize_comparison(*, legacy, deepagents) -> dict` (compare_runner.py).
  - `run_comparison(questions, *, out_dir, settings, session_factory) -> dict` — writes `report.json` + `report.md`.
  - `main(argv) -> int` CLI with `--ids Q001,Q002` and `--out-dir` flags.

- [ ] **Step 1: Write the failing unit tests**

`tests/unit/test_deepagents_compare_runner.py`:

```python
from __future__ import annotations

import json

from backend.app.db.models import AgentEvent, AgentPlan, AgentStep, AgentTurn
from backend.app.domain.agent_runtime import RunStatus
from backend.app.services.deepagents_runtime.budgets import DeepAgentsBudgets
from backend.app.services.deepagents_runtime.eval.compare_runner import (
    Question,
    RunMetrics,
    _load_questions,
    main,
    run_comparison,
    run_deepagents_question,
    run_legacy_question,
    summarize_comparison,
)


def _question() -> Question:
    return Question(
        id="Q001",
        goal="帮我找后端岗位",
        allowed_skills=["job-discovery"],
        context={"profile": {}},
    )


class _FakeResult:
    def __init__(self, run_id, status, error_code=None):
        self.run_id = run_id
        self.status = status
        self.error_code = error_code


class _FakeHarness:
    def __init__(self, finals):
        self._finals = list(finals)

    def run(self, request, *, run_id, budgets=None):
        self.request = request
        self.run_id = run_id
        return self._finals.pop(0)


def test_summarize_comparison_computes_distribution_and_counts() -> None:
    legacy = [
        RunMetrics("succeeded", steps=2, turns=4, tool_calls=3, replans=0, wall_clock_s=10.0, error_code=None),
        RunMetrics("waiting_user", steps=1, turns=2, tool_calls=1, replans=0, wall_clock_s=5.0, error_code="blocked"),
    ]
    deepagents = [
        RunMetrics("succeeded", steps=2, turns=3, tool_calls=2, replans=0, wall_clock_s=8.0, error_code=None),
        RunMetrics("succeeded", steps=2, turns=5, tool_calls=4, replans=1, wall_clock_s=12.0, error_code=None),
    ]
    summary = summarize_comparison(legacy=legacy, deepagents=deepagents)
    assert summary["legacy"]["succeeded"] == 1
    assert summary["deepagents"]["succeeded"] == 2
    assert summary["deepagents"]["avg_turns"] == 4.0
    assert summary["deepagents"]["replan_total"] == 1
    assert summary["deepagents"]["avg_wall_clock_s"] == 10.0
    assert summary["legacy"]["error_codes"] == ["blocked"]


def test_summarize_comparison_empty_inputs() -> None:
    summary = summarize_comparison(legacy=[], deepagents=[])
    assert summary["legacy"]["avg_turns"] == 0.0
    assert summary["deepagents"]["total"] == 0


def test_run_legacy_question_counts_db_rows(db_session) -> None:
    # fake runner skips the real AgentRunService; rows are seeded directly
    # (mirror existing test_agent_runtime tests if required fields differ)
    db_session.add_all(
        [
            AgentStep(run_id="legacy-1", step_id="s-1", agent_role="planner", status="succeeded", objective="目标"),
            AgentTurn(run_id="legacy-1", agent_role="executor", turn_index=0),
            AgentTurn(run_id="legacy-1", agent_role="executor", turn_index=1),
            AgentPlan(run_id="legacy-1", revision=2),
            AgentEvent(run_id="legacy-1", event_type="tool_call", payload_json={"tool": "fetch-public-job-pages"}),
            # payload_json=None -> `payload_json or {}` falsy branch
            AgentEvent(run_id="legacy-1", event_type="other", payload_json=None),
        ]
    )
    db_session.commit()

    def fake_runner(question, settings, session_factory):
        return _FakeResult(run_id="legacy-1", status=RunStatus.succeeded)

    metrics = run_legacy_question(
        _question(), settings=None, session_factory=lambda: db_session, runner=fake_runner
    )
    assert metrics.status == "succeeded"
    assert metrics.steps == 1
    assert metrics.turns == 2
    assert metrics.tool_calls == 1
    assert metrics.replans == 1  # plan_count 2 - 1


def test_run_legacy_question_empty_tables(db_session) -> None:
    def fake_runner(question, settings, session_factory):
        return _FakeResult(run_id="legacy-2", status=RunStatus.succeeded)

    metrics = run_legacy_question(
        _question(), settings=None, session_factory=lambda: db_session, runner=fake_runner
    )
    assert metrics.steps == 0  # `or 0` fallback
    assert metrics.replans == 0  # `or 1` fallback: max(0, 1 - 1)


def test_run_deepagents_question_parses_harness_output() -> None:
    budgets = DeepAgentsBudgets(max_agent_turns=12)
    harness = _FakeHarness(
        [
            {
                "run_status": "waiting_user",
                "budget": budgets.to_dict(),
                "plan_json": {"steps": [{"step_index": 0}, {"step_index": 1}]},
                "error_code": "stalled_no_progress",
            },
            # truthy non-dict plan_json -> isinstance(..., dict) false branch
            {
                "run_status": "succeeded",
                "budget": budgets.to_dict(),
                "plan_json": ["not", "a", "dict"],
                "error_code": None,
            },
        ]
    )
    first = run_deepagents_question(
        _question(), settings=None, run_id="eval-Q001", harness=harness
    )
    assert first.status == "waiting_user"
    assert first.steps == 2
    assert first.turns == budgets.turns_used
    assert first.error_code == "stalled_no_progress"
    assert harness.run_id == "eval-Q001"
    assert harness.request.allowed_skills == ["job-discovery"]
    second = run_deepagents_question(
        _question(), settings=None, run_id="eval-Q001", harness=harness
    )
    assert second.steps == 0  # non-dict plan_json -> else branch


def test_run_comparison_writes_report_files(tmp_path, monkeypatch) -> None:
    import backend.app.services.deepagents_runtime.eval.compare_runner as cr

    def fake_legacy(q, *, settings, session_factory):
        return RunMetrics("succeeded", steps=1, turns=2, tool_calls=1, replans=0, wall_clock_s=1.0, error_code=None)

    def fake_deepagents(q, *, settings, run_id):
        return RunMetrics("succeeded", steps=1, turns=1, tool_calls=1, replans=0, wall_clock_s=0.5, error_code=None)

    monkeypatch.setattr(cr, "run_legacy_question", fake_legacy)
    monkeypatch.setattr(cr, "run_deepagents_question", fake_deepagents)
    report = run_comparison(
        [_question()], out_dir=tmp_path, settings=None, session_factory=None
    )
    assert (tmp_path / "report.json").exists()
    assert (tmp_path / "report.md").exists()
    assert report["summary"]["legacy"]["succeeded"] == 1
    assert "DeepAgents Runtime 对比评测" in (tmp_path / "report.md").read_text(encoding="utf-8")


def test_load_questions_skips_missing_docs() -> None:
    assert _load_questions(["NO_SUCH_QUESTION"]) == []


def test_load_questions_skips_chain_docs(monkeypatch) -> None:
    import backend.app.services.deepagents_runtime.eval.compare_runner as cr

    chain_doc = json.dumps(
        {"id": "Q001", "question": "g", "chain": [{"id": "Q001a"}], "meta": {"skills": ["job-discovery"]}},
        ensure_ascii=False,
    )
    monkeypatch.setattr(cr.Path, "read_text", lambda self, **kwargs: chain_doc)
    assert _load_questions(["Q001"]) == []


def test_main_no_questions_returns_1(monkeypatch) -> None:
    import backend.app.services.deepagents_runtime.eval.compare_runner as cr

    monkeypatch.setattr(
        "backend.app.services.agent_runtime.provider_config.load_project_env", lambda: None
    )
    monkeypatch.setattr(cr, "_load_questions", lambda ids: [])
    assert main(["--ids", "Q001", "--out-dir", "unused"]) == 1


def test_main_runs_comparison(tmp_path, monkeypatch) -> None:
    import backend.app.services.deepagents_runtime.eval.compare_runner as cr
    from backend.app.config import Settings
    from backend.app.db import session as db_session_module

    calls = {}
    monkeypatch.setattr(
        "backend.app.services.agent_runtime.provider_config.load_project_env", lambda: None
    )
    monkeypatch.setattr("backend.app.config.get_settings", lambda: Settings())
    monkeypatch.setattr(db_session_module, "SessionLocal", object())
    monkeypatch.setattr(cr, "_load_questions", lambda ids: [_question()])

    def fake_comparison(questions, *, out_dir, settings, session_factory):
        calls["questions"] = questions
        calls["out_dir"] = out_dir
        return {"summary": {}, "per_question": []}

    monkeypatch.setattr(cr, "run_comparison", fake_comparison)
    # trailing comma covers the ids-comprehension filter falsy branch
    assert main(["--ids", "Q001,", "--out-dir", str(tmp_path)]) == 0
    assert calls["questions"][0].id == "Q001"
    assert calls["out_dir"] == tmp_path


def test_run_legacy_question_default_runner_path(monkeypatch, db_session) -> None:
    import contextlib

    import backend.app.services.deepagents_runtime.eval.compare_runner as cr

    class _FakeAgentRunService:
        def __init__(self, settings, *, runtime):
            self.settings = settings
            self.runtime = runtime

        def create_run(self, db, *, user_id, task):
            assert user_id == "eval-user"
            assert task.goal == "帮我找后端岗位"
            return _FakeResult(run_id="legacy-default", status=RunStatus.succeeded)

    class _FakeSessionFactory:
        def __init__(self, session):
            self._session = session

        def __call__(self):
            return self._session

        def begin(self):
            return contextlib.nullcontext(self._session)

    # the default runner closure resolves these names in the cr module
    # namespace at call time, so patching them covers the real assembly
    monkeypatch.setattr(cr, "build_agent_model_gateway", lambda settings: object())
    monkeypatch.setattr(cr, "build_career_tool_registry", lambda: object())
    monkeypatch.setattr(cr, "PlannerAgent", lambda gateway, tools: object())
    monkeypatch.setattr(cr, "ExecutorAgent", lambda gateway, tools: object())
    monkeypatch.setattr(cr, "VerifierAgent", lambda gateway, tools: object())
    monkeypatch.setattr(cr, "AgentRuntime", lambda **kwargs: object())
    monkeypatch.setattr(cr, "AgentRunService", _FakeAgentRunService)
    metrics = run_legacy_question(
        _question(),
        settings=None,
        session_factory=_FakeSessionFactory(db_session),
    )
    assert metrics.status == "succeeded"
    assert metrics.steps == 0  # no rows for legacy-default


def test_run_deepagents_question_default_harness_path(monkeypatch) -> None:
    import backend.app.services.deepagents_runtime.eval.compare_runner as cr

    class _Settings:
        agent_harness_model = "deepseek-chat"

    budgets = DeepAgentsBudgets(max_agent_turns=12)
    final = {
        "run_status": "succeeded",
        "budget": budgets.to_dict(),
        # plan_json None -> `final.get(...) or {}` falsy branch
        "plan_json": None,
        "error_code": None,
    }

    class _FakeChatOpenAI:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class _FakeHarnessClass:
        def __init__(self, *, model_factory, checkpointer):
            # exercise both role branches of the default model_factory
            model_factory("planner")
            model_factory("executor")

        def run(self, request, *, run_id, budgets=None):
            return final

    monkeypatch.setattr("langchain_openai.ChatOpenAI", _FakeChatOpenAI)
    monkeypatch.setattr(cr, "DeepAgentsHarness", _FakeHarnessClass)
    monkeypatch.setattr(cr, "create_checkpointer", lambda settings: object())
    metrics = run_deepagents_question(
        _question(), settings=_Settings(), run_id="eval-default"
    )
    assert metrics.status == "succeeded"
    assert metrics.steps == 0
    assert metrics.turns == 0
    assert metrics.tool_calls == 0


def test_load_questions_empty_ids_uses_all_docs(monkeypatch) -> None:
    import backend.app.services.deepagents_runtime.eval.compare_runner as cr

    good_doc = json.dumps(
        {"id": "Q001", "question": "帮我找后端岗位", "meta": {"skills": ["job-discovery"]}},
        ensure_ascii=False,
    )
    monkeypatch.setattr(cr.Path, "read_text", lambda self, **kwargs: good_doc)
    questions = _load_questions([])  # ids falsy -> glob all docs branch
    assert questions  # tests/question has at least one .json doc
    assert questions[0].allowed_skills == ["job-discovery"]


def test_load_questions_skips_docs_without_skills(monkeypatch) -> None:
    import backend.app.services.deepagents_runtime.eval.compare_runner as cr

    no_skills = json.dumps(
        {"id": "Q001", "question": "g", "meta": {}}, ensure_ascii=False
    )
    monkeypatch.setattr(cr.Path, "read_text", lambda self, **kwargs: no_skills)
    assert _load_questions(["Q001"]) == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_deepagents_compare_runner.py -q`
Expected: FAIL — `ModuleNotFoundError: eval.compare_runner`.

- [ ] **Step 3: Implement the compare runner**

`backend/app/services/deepagents_runtime/eval/__init__.py`:

```python
"""Dual-runtime comparison eval (agent_runtime vs deepagents_runtime)."""
```

`backend/app/services/deepagents_runtime/eval/compare_runner.py`:

```python
"""Compare agent_runtime vs deepagents_runtime on the same question set.

Runs against the real stack (docker compose: Redis + MySQL + LLM key).
Writes ``report.json`` and ``report.md`` under ``out_dir``: success
distribution (succeeded / waiting_user / failed), avg turns, tool calls,
replans, wall-clock and error codes, per question.

The legacy leg runs through the real AgentRunService so both sides execute
their full production code paths; the deepagents leg runs the harness with
a real ChatOpenAI (DeepSeek via the same environment provider as the
legacy gateway).  Live only — never unit-tested end to end.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from sqlalchemy import func, select

from backend.app.config import Settings
from backend.app.db.models import AgentEvent, AgentPlan, AgentStep, AgentTurn
from backend.app.services.agent_runtime.executor_agent import ExecutorAgent
from backend.app.services.agent_runtime.model_gateway import build_agent_model_gateway
from backend.app.services.agent_runtime.planner_agent import PlannerAgent
from backend.app.services.agent_runtime.runtime import AgentRuntime
from backend.app.services.agent_runtime.schemas import AgentTaskRequest
from backend.app.services.agent_runtime.service import AgentRunService
from backend.app.services.agent_runtime.verifier_agent import VerifierAgent
from backend.app.services.career_skills.registry import build_career_tool_registry
from backend.app.services.deepagents_runtime.budgets import DeepAgentsBudgets
from backend.app.services.deepagents_runtime.checkpoints.factory import create_checkpointer
from backend.app.services.deepagents_runtime.harness import DeepAgentsHarness


@dataclass
class Question:
    id: str
    goal: str
    allowed_skills: list[str]
    context: dict


@dataclass
class RunMetrics:
    status: str
    steps: int
    turns: int
    tool_calls: int
    replans: int
    wall_clock_s: float
    error_code: str | None


def run_legacy_question(
    question: Question,
    *,
    settings: Settings,
    session_factory,
    runner=None,
) -> RunMetrics:
    """Run one question through the existing agent_runtime service.

    ``runner`` is an injectable seam for unit tests: ``(question, settings,
    session_factory) -> result-like`` with ``run_id`` / ``status`` /
    ``error_code`` attributes.  Defaults to the real service assembly (live
    eval only — the default path is unit-covered by monkeypatching the
    ``cr.*`` assembly names, see ``test_run_legacy_question_default_runner_path``).
    """
    if runner is None:

        def default_runner(question, settings, session_factory):
            gateway = build_agent_model_gateway(settings)
            tools = build_career_tool_registry()
            runtime = AgentRuntime(
                planner=PlannerAgent(gateway=gateway, tools=tools),
                executor=ExecutorAgent(gateway=gateway, tools=tools),
                verifier=VerifierAgent(gateway=gateway, tools=tools),
                agent_version="pev-1",
            )
            service = AgentRunService(settings, runtime=runtime)
            task = AgentTaskRequest(
                goal=question.goal,
                allowed_skills=question.allowed_skills,
                context=question.context,
            )
            with session_factory.begin() as db:  # commit so the counters can read it
                return service.create_run(db, user_id="eval-user", task=task)

        runner = default_runner
    started = time.monotonic()
    result = runner(question, settings, session_factory)
    elapsed = time.monotonic() - started
    with session_factory() as db:
        steps = db.scalar(
            select(func.count())
            .select_from(AgentStep)
            .where(AgentStep.run_id == result.run_id)
        ) or 0
        turns = db.scalar(
            select(func.count())
            .select_from(AgentTurn)
            .where(AgentTurn.run_id == result.run_id)
        ) or 0
        replans = max(
            0,
            (
                db.scalar(
                    select(func.count())
                    .select_from(AgentPlan)
                    .where(AgentPlan.run_id == result.run_id)
                )
                or 1
            )
            - 1,
        )
        events = db.scalars(
            select(AgentEvent).where(AgentEvent.run_id == result.run_id)
        )
        tool_calls = sum(
            1 for event in events if (event.payload_json or {}).get("tool")
        )
    return RunMetrics(
        status=result.status.value,
        steps=steps,
        turns=turns,
        tool_calls=tool_calls,
        replans=replans,
        wall_clock_s=round(elapsed, 2),
        error_code=result.error_code,
    )


def run_deepagents_question(
    question: Question, *, settings: Settings, run_id: str, harness=None
) -> RunMetrics:
    """Run one question through the deepagents harness (real model).

    ``harness`` is an injectable seam for unit tests: an object with
    ``run(request, *, run_id, budgets=None) -> final dict`` (keys
    ``run_status`` / ``budget`` / ``plan_json`` / ``error_code``).
    Defaults to real ChatOpenAI + DeepAgentsHarness (live eval only — the
    default path is unit-covered by monkeypatching ``cr.DeepAgentsHarness``
    and ``langchain_openai.ChatOpenAI``, see
    ``test_run_deepagents_question_default_harness_path``).
    """
    if harness is None:
        from langchain_openai import ChatOpenAI

        def model_factory(role: str) -> ChatOpenAI:
            return ChatOpenAI(
                model=settings.agent_harness_model,
                temperature=0,
                max_tokens=4096 if role == "planner" else 2048,
            )

        harness = DeepAgentsHarness(
            model_factory=model_factory,
            checkpointer=create_checkpointer(settings),
        )
    request = AgentTaskRequest(
        goal=question.goal,
        allowed_skills=question.allowed_skills,
        context=question.context,
    )
    started = time.monotonic()
    final = harness.run(request, run_id=run_id)
    elapsed = time.monotonic() - started
    budgets = DeepAgentsBudgets.from_dict(final["budget"])
    plan_json = final.get("plan_json") or {}
    steps = len(plan_json.get("steps", [])) if isinstance(plan_json, dict) else 0
    return RunMetrics(
        status=final["run_status"] or "unknown",
        steps=steps,
        turns=budgets.turns_used,
        tool_calls=budgets.tool_calls_used,
        replans=budgets.replans_used,
        wall_clock_s=round(elapsed, 2),
        error_code=final.get("error_code"),
    )


def _avg(values: list[float]) -> float:
    return round(sum(values) / len(values), 2) if values else 0.0


def summarize_comparison(
    *, legacy: list[RunMetrics], deepagents: list[RunMetrics]
) -> dict:
    """Aggregate per-runtime distributions and averages for the report."""

    def bucket(metrics: list[RunMetrics]) -> dict:
        statuses = [m.status for m in metrics]
        return {
            "succeeded": statuses.count("succeeded"),
            "waiting_user": statuses.count("waiting_user"),
            "failed": statuses.count("failed"),
            "total": len(metrics),
            "avg_steps": _avg([m.steps for m in metrics]),
            "avg_turns": _avg([m.turns for m in metrics]),
            "avg_tool_calls": _avg([m.tool_calls for m in metrics]),
            "avg_wall_clock_s": _avg([m.wall_clock_s for m in metrics]),
            "replan_total": sum(m.replans for m in metrics),
            "error_codes": sorted({m.error_code for m in metrics if m.error_code}),
        }

    return {"legacy": bucket(legacy), "deepagents": bucket(deepagents)}


def run_comparison(
    questions: list[Question], *, out_dir: Path, settings: Settings, session_factory
) -> dict:
    """Run both runtimes over the questions and write report.json + report.md."""
    out_dir.mkdir(parents=True, exist_ok=True)
    legacy_metrics: list[RunMetrics] = []
    deepagents_metrics: list[RunMetrics] = []
    per_question: list[dict] = []
    for question in questions:
        deepagents_metrics.append(
            run_deepagents_question(
                question, settings=settings, run_id=f"eval-{question.id}"
            )
        )
        legacy_metrics.append(
            run_legacy_question(
                question, settings=settings, session_factory=session_factory
            )
        )
        per_question.append(
            {
                "id": question.id,
                "goal": question.goal,
                "legacy": asdict(legacy_metrics[-1]),
                "deepagents": asdict(deepagents_metrics[-1]),
            }
        )
    summary = summarize_comparison(
        legacy=legacy_metrics, deepagents=deepagents_metrics
    )
    report = {"summary": summary, "per_question": per_question}
    (out_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "report.md").write_text(_render_markdown(report), encoding="utf-8")
    return report


def _render_markdown(report: dict) -> str:
    summary = report["summary"]
    lines = ["# DeepAgents Runtime 对比评测", ""]
    for runtime in ("legacy", "deepagents"):
        bucket = summary[runtime]
        lines.append(f"## {runtime}")
        lines.append(
            f"- succeeded={bucket['succeeded']} waiting_user={bucket['waiting_user']} "
            f"failed={bucket['failed']} total={bucket['total']}"
        )
        lines.append(
            f"- avg_steps={bucket['avg_steps']} avg_turns={bucket['avg_turns']} "
            f"avg_tool_calls={bucket['avg_tool_calls']} "
            f"avg_wall_clock_s={bucket['avg_wall_clock_s']} replan_total={bucket['replan_total']}"
        )
        lines.append(f"- error_codes={bucket['error_codes']}")
        lines.append("")
    return "\n".join(lines)


def _load_questions(ids: list[str]) -> list[Question]:
    """Load Q###.json / C###.json docs from tests/question (schema verified)."""
    question_dir = Path(__file__).resolve().parents[5] / "tests" / "question"
    all_ids = sorted(path.stem for path in question_dir.glob("*.json"))
    questions: list[Question] = []
    for qid in ids or all_ids:
        doc_path = question_dir / f"{qid}.json"
        if not doc_path.exists():
            print(f"SKIP {qid}: {doc_path.name} missing")
            continue
        doc = json.loads(doc_path.read_text(encoding="utf-8"))
        meta = doc.get("meta") or {}
        skills = meta.get("skills") or []
        if not skills or "chain" in doc:
            print(f"SKIP {qid}: needs meta.skills (chain docs run via eval_runner)")
            continue
        questions.append(
            Question(
                id=doc["id"],
                goal=doc["question"],
                allowed_skills=skills,
                context={"profile": doc.get("profile"), "meta": meta},
            )
        )
    return questions


def main(argv: list[str] | None = None) -> int:
    """CLI: ``--ids Q001,Q002 --out-dir <dir>`` (defaults to all questions)."""
    import argparse

    from backend.app.config import get_settings
    from backend.app.db.session import SessionLocal
    from backend.app.services.agent_runtime.provider_config import load_project_env

    parser = argparse.ArgumentParser()
    parser.add_argument("--ids", default="")
    parser.add_argument(
        "--out-dir", default="tests/question/eval_results/deepagents_round_1"
    )
    args = parser.parse_args(argv)
    ids = [value.strip() for value in args.ids.split(",") if value.strip()]

    load_project_env()  # ensure .env vars for settings + LLM key (mirrors eval_runner)
    questions = _load_questions(ids)
    if not questions:
        print("no questions loaded")
        return 1
    run_comparison(
        questions,
        out_dir=Path(args.out_dir),
        settings=get_settings(),
        session_factory=SessionLocal,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the unit tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_deepagents_compare_runner.py -q`
Expected: PASS.

- [ ] **Step 5: Write the live parity script and run it**

`tests/manual/run_deepagents_parity.py`:

```python
"""10-URL parity gate: job-discovery workflow subgraph vs B-mode baseline.

Gate (spec §7): the workflow tool's per-URL success count and extracted
candidate count must not be WORSE than the recorded B-mode baseline
(``tests/manual/_skill_ten_url_*.json``, produced by
``run_skill_ten_url_eval.py``).  Exits 0 on parity, 1 on regression.

Requires: real stack (docker compose up, LLM key) + RUN_DEEPAGENTS_PARITY=1.
"""

from __future__ import annotations

import json
from pathlib import Path

from backend.app.services.deepagents_runtime.tools.skill_graphs import (
    build_job_discovery_tool,
)

_BASELINE_DIR = Path(__file__).resolve().parent
_URLS = [
    "https://app.mokahr.com/campus-recruitment/deeproute/145894#/home",
    "https://careers.pddglobalhr.com/campus/grad?t=AOT9z6aa0x",
    "https://xiaopeng.jobs.feishu.cn/campus/position/list",
    "https://recruit.inovance.com/#/jobs",
    "https://job.xiaohongshu.com/campus/position",
    "https://talent.didiglobal.com/campus/",
    "https://hr.163.com/campus.html",
    "https://talent.baidu.com/jobs/campus/list",
    "https://jobs.bytedance.com/campus/position",
    "https://xiaomi.jobs.f.mioffice.cn/s/kJVnd58xtWY",
]

SKIP_MSG = "RUN_DEEPAGENTS_PARITY=1 required (live LLM + Playwright)"


def main() -> int:
    import os

    if os.environ.get("RUN_DEEPAGENTS_PARITY") != "1":
        print(SKIP_MSG)
        return 0
    tool = build_job_discovery_tool()
    result = json.loads(tool.invoke({"payload": json.dumps(_URLS)}))
    per_url = result["per_url_results"]
    candidates = result["candidates"]
    coverage = result["coverage"]

    baseline_records = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(_BASELINE_DIR.glob("_skill_ten_url_*.json"))
    ]
    baseline_success = sum(
        1 for record in baseline_records if record.get("status") == "succeeded"
    )
    baseline_candidates = sum(
        record.get("unique_listing_count", 0) for record in baseline_records
    )
    our_success = sum(
        1 for entry in per_url if entry.get("status") == "succeeded"
    )
    print(
        f"baseline success={baseline_success} candidates={baseline_candidates} | "
        f"ours success={our_success} candidates={len(candidates)} coverage={coverage}"
    )
    if our_success < baseline_success or len(candidates) < baseline_candidates:
        print("PARITY FAILED: regression vs B-mode baseline")
        return 1
    if not coverage.get("verified", False):
        print("PARITY FAILED: coverage gate not verified")
        return 1
    print("PARITY PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Run: `$env:RUN_DEEPAGENTS_PARITY='1'; .\.venv\Scripts\python.exe -X utf8 tests/manual/run_deepagents_parity.py`
This is a live run; record its outcome in the task notes. If it fails, the expected iteration points are: (a) the workflow's fast-path fetch returning empty SPA shells (wire the Playwright fallback into `_default_fetch` the same way the eval harness enables it — `eval_runner.py:489` `jd_skill.enable_playwright_fallback(True)`); (b) `coverage_gate` needing the page evidence files the scripts write under the skill dir — pass `--terminal-evidence`/`--manifest` pointing at the browse output. Fix and re-run until the gate passes or the regression is documented honestly in the task notes.

- [ ] **Step 6: Update CLAUDE.md**

1. In the "Security Hard Gates" list, append after item 5's text:

```markdown
   - **Exception (deepagents_runtime only, spec 2026-08-07):** agent
     execution checkpoints (LangGraph threads) may live in Redis (AOF
     persistence); completed run records and evidence artifacts are always
     flushed to MySQL at run completion (`flush_run`, idempotent + retry).
     This exception covers only short-lived execution state — MySQL stays
     authoritative for business state.
```

2. Add a "DeepAgents Runtime" section (after the "Personal Career Assistant (PEV Runtime)" section):

```markdown
## DeepAgents Runtime (parallel build, eval pending)

> Design: [docs/superpowers/specs/2026-08-07-deepagents-runtime-design.md](docs/superpowers/specs/2026-08-07-deepagents-runtime-design.md)

A second PEV runtime built on langchain deepagents (`backend/app/services/deepagents_runtime/`),
built in parallel with the self-built `agent_runtime` and not yet replacing
it.  Three deep agents (Planner / Executor / Verifier) are driven by an
external LangGraph harness graph that enforces the same invariants
(budgets, one-skill-per-step, evidence binding, stall-breaker, recoverable
`waiting_user`).  Tools: career_skills registry tools wrapped generically as
`@tool` (adapters.py), and the job-discovery SKILL.md workflow encoded as a
LangGraph subgraph wrapped as `run-job-discovery-workflow`.  Execution state
checkpoints to Redis (AOF); completed runs flush to MySQL
(`deepagents_runs` / `deepagents_artifacts`).  Comparative eval:
`python -m backend.app.services.deepagents_runtime.eval.compare_runner --ids Q001 Q002 --out-dir tests/question/eval_results/deepagents_round_1`
```

3. Add a doc-table row:

```markdown
| [DeepAgents Runtime Design](docs/superpowers/specs/2026-08-07-deepagents-runtime-design.md) | deepagents-based parallel PEV runtime: harness graph, tool layer, Redis checkpoint + MySQL sink |
```

- [ ] **Step 7: Full suite + ruff + coverage close-out**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/ -q` — all PASS with the new package at 100% branch coverage. If coverage reports a gap in the new package, add the missing unit test (in the matching `tests/unit/test_deepagents_*.py`) before committing. Run `.\.venv\Scripts\python.exe -m ruff check backend tests scripts`.

- [ ] **Step 8: Commit**

```bash
git add backend/app/services/deepagents_runtime/eval tests/manual/run_deepagents_parity.py tests/unit/test_deepagents_compare_runner.py CLAUDE.md
git commit -m "feat(deepagents-runtime): P5 comparative eval, parity gate, docs, coverage close-out

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Self-Review Notes (plan vs spec)

- §1 decision summary → Tasks 1-6 map 1:1 (parallel build evaluated in Task 6; 3 deep agents in Task 2; workflow subgraph in Task 4; Redis+MySQL in Task 1/5; coverage gate in every task).
- §4.1 adapters → Task 3; §4.2 workflow subgraph → Task 4 (whitelist runner + per-node script contracts verified against the real scripts); §4.3 extraction gate → Task 3 (`extract_with_gate`); §4.4 skill selection → Task 3 (job-matching/career-planning/resume-tailoring adapters are generic over the registry; company-research/interview-prep/application-tracking deferred — documented as eval-time optional in the spec).
- §5 invariants → Task 2 harness (budget gates at every node boundary, one-skill-per-step `_sole_skill` + planner-side validation, stall-breaker, evidence projection, thread mapping, resume with counter preservation + window refresh).
- §6 persistence → Task 1 (models/migration/factory) + Task 5 (sink). Residual risks from §6.3: (1) crash-before-flush documented — the sink retries; startup salvage sweep explicitly deferred (noted in spec); (2) langgraph-checkpoint-redis compat — imports verified in Task 1 unit tests + **Task 1 Step 5 live Redis smoke** (sync with async fallback; failure → MySQL-saver fallback per §11), plus real Redis exercised by the Task 6 live runs.
- §6.2 gap found in review (first draft had no flush caller): Task 5 Step 4 wires the sink into the harness — `session_factory` constructor param (default `None` skips flushing), `_flush_if_configured` after every `run()`/`resume()` invoke, module-level `_evidence_artifacts` (`artifact_id or content_hash`, `visible_text or ""` excerpted to 1,200 chars, matching `_project_tool_observations` entry shape). Coverage: `test_harness_flushes_completed_run` covers the run() call site + wired flush branch; `test_harness_flush_hook_evidence_branches` covers both `or` branches via a direct `_flush_if_configured` call; Task 2's existing tests cover the resume() call site and the `session_factory is None` early return.
- §7 eval/parity → Task 6.
- §8 coverage → every task runs the full suite; new package auto-measured.
- §12 CLAUDE.md exception → Task 6 Step 6.
- All types cross-checked between tasks: `DeepAgentsBudgets` methods (`try_consume_turn/tool/replan`, `start_window/refresh_window/window_exhausted`, `to_dict/from_dict`), `build_skill_tools(*, skill_name, budgets, tracker, context_factory=None, registry=None)`, `flush_run` kwargs, `RunMetrics` fields, `Question` fields — identical names in every task that references them.
- Known iteration seams (honest bounds): Task 2 Step 5 lists the four most likely `create_deep_agent`/`structured_response`/`FakeListChatModel`/`agent.name` deviations with fixes; Task 6 Step 5 lists the two live-parity iteration points (Playwright fallback wiring, coverage_gate evidence paths).

---

## Plan Extension (2026-08-07, USER DECISION): full skill-parity port of job-discovery

**Why this extension exists:** the parity gate (baseline: 6 URLs, success=3, candidates=797) failed against the Task 4 subgraph (run3: success=6, candidates=1, coverage verified=False). Root cause established by gap analysis (`.superpowers/sdd/2026-08-07-deepagents-runtime/skill-gap-inventory.md`, 94 items: PORTED 7 / PARTIAL 19 / MISSING 61 / OUT-OF-SUBGRAPH-SCOPE 7): the Task 4 subgraph rendered the skill as a single batch pass and did not port the skill's interactive browsing layer, per-page extraction fan-out, incremental persistence, or the WeChat image-article (OCR) branch. The user's decision: **port every idea/branch present in the 9 `skill/job-discovery` markdown docs into the subgraph (including the WeChat OCR branch); nothing may exist in the skill that does not exist in the subgraph; the parity gate remains the acceptance criterion and must be re-run when the port is complete.**

**Scope rules (same as before, all tasks):** `skill/job-discovery/*` (scripts and docs) and `backend/app/services/career_skills/*` remain **read-only** — the subgraph drives the existing scripts' modes/parameters through `run_skill_script` and reuses the existing handlers. All 100% branch coverage / ruff / never-modify / commit-footer / security-gate constraints from Global Constraints above apply. Script CLI flag spellings must be verified against the real scripts by the implementer (the same seam pattern Task 4 used); the contract shapes below are the verified output keys.

**Verified script contracts the extension builds on (from gap analysis, all verified against the real scripts):**

- `browse.py <url> --mode <mode> [--out <dir>] [--max-pages N] [--cache-mode use|revalidate|off] [--wait MS]` prints one JSON line to stdout: `status` (`ok`|`blocked`|`error`), `url`, `mode`, `title`, `content_hash` (format `sha256_<16>`, truncated — **never** the manifest hash), `text_path`, `screenshot_path`. `status=blocked` (0-char shell / unsafe URL / nav error) — never retried; `status=error` (timeout / Playwright missing) exits 1.
- `parallel-fetch` output additionally carries `used_path` (one of `parallel` / `spa_shell_no_pagination` / `spa_shell_empty_no_evidence` / `click_fallback_no_detect` / `click_fallback_fetch_error` / `interact_fallback_*`), `page_count`, `page_files` (list of `pages/page_NN.txt` relative to `--out`), `text_length`. `<500` chars is the SPA fast-fail → search-interact retry target.
- `list` mode additionally carries `terminal_evidence` (one of `next_control_absent` / `page_content_repeated`) and, with `--cache-mode use`, writes `cache.json` in `--out` and marks hits `"cached": True`. Cache applies to `list` mode only.
- `coverage_gate.py --manifest <manifest.json>` (or `--pages <paths>` + `--terminal-evidence <marker>`): requires candidates whose `evidence_refs[].content_hash` intersects the **sha256 of the page file bytes** (full hex, computed by the script from disk); manifest keys: `page_files` (real non-zero paths under the evidence dir), `terminal_evidence` (observed markers only — an empty list is honest), `declared_total_pages`, `pages_collected`, `truncated_by_max_pages`, `listing_count`. Output: `coverage_verified` + reasons + metrics; `missing_terminal_evidence` when no marker passed.
- `write_candidates.py` reads candidate JSON from **stdin**, rejects candidates without `title`+`company`+`body` (title-only evidence is dropped), writes `output/candidates/page_NN.json` (suffix `page_<NN>_…` redirects to `page_NN.json`), `--out` must stay under `output/`, exit code 0 always, `--append` is identity-deduped.
- `deduplicate.py` globs `output/candidates/page_*.json` (never `_merged.*`), writes `merged_final.json`, prints counts to stdout.
- `state.py check <url> <update_time>` → exit 0 = skip / exit 1 = extract; `mark <url> <entry_id> --file-id <file-id> --sheet-id <sheet-id>` (both flags required); state lives at `output/state.json`; `entry_id = content_hash[:16]_url_hash8`.
- `normalize.py` computes comparison keys only (`--title/--company/--text/--hash --json`) — it never alters stored titles.
- `ocr_image.py` is in `_ALLOWED_SCRIPTS` but currently never invoked — Task 9 wires it.
- `run_skill_script(script, cli_args="", stdin="", *, runner=None)` already supports `stdin` and `cwd=SKILL_DIR`; no runner change is needed for any task below.
- `ExtractObservedJobDetailsInput` has **one** `artifact_id: str` field; `ExtractObservedJobDetailsOutput` = `source_artifact_id` / `source_url` / `content_hash` / `candidates: list[ExtractedJobDetails]`; `ExtractedJobDetails.confidence` is a non-nullable float. Per-page fan-out therefore = one `extract_with_gate` call per page file with that page's `artifact_id`.
- Rulings on doc contradictions (matches the skill's own design): X1 → per-page fan-out is the canonical extraction path, batch extract retained only for static fast-path evidence; X2 → only observed terminal evidence is ever reported; M3 → validate step keeps the schema.md standard values (career_skills' English values are its read-only behavior); U10 → dedup stdout counts enter the tool output.

---

### Task 7: browse-backed fetch — site classification, mode selection, fallback chain, cache, terminal evidence

**Files:**
- Create: `backend/app/services/deepagents_runtime/tools/skill_graphs/browse_fetch.py`
- Modify: `backend/app/services/deepagents_runtime/tools/skill_graphs/job_discovery_graph.py` (fetch node + `_default_fetch` replacement; keep node/edge structure and all seams)
- Test: `tests/unit/test_deepagents_browse_fetch.py`, extend `tests/unit/test_deepagents_skill_tool.py`

**Interfaces:**
- Consumes: `run_skill_script` (subprocess_runner.py), `_ALLOWED_SCRIPTS` (unchanged), current fetch-node seam `fetch_fn(urls, *, runner)` shape (Task 4).
- Produces (later tasks rely on these exact names):
  - `class SiteClass(str, Enum)` with `WECHAT` / `PARALLEL_FETCH` / `LIST` / `SEARCH_INTERACT` / `PROBE`; `classify_url(url: str) -> SiteClass` (module-level, pure).
  - `@dataclass PageFile: path: str; content_hash: str; text_length: int` and `@dataclass UrlFetchResult: url; site_class: str; mode: str; status: str; used_path: str | None; page_files: list[PageFile]; terminal_evidence: list[str]; cached: bool; title: str | None; blocked_reason: str | None; error_code: str | None`.
  - `page_file_hash(path: str, *, out_dir: str) -> tuple[str, int]` — full `sha256(file bytes).hexdigest()` + byte length (the manifest/evidence hash; browse's own `sha256_<16>` is never used as evidence hash).
  - `browse_fetch_urls(urls: list[str], *, runner=None, out_dir: str | None = None, cache_mode: str = "use") -> list[UrlFetchResult]` — default `runner=None` calls `run_skill_script`; `out_dir=None` defaults to `output/evidence/run-<run_id>`-style stable dir (see Step 4).
  - `mode_for_class(site_class: SiteClass, *, probe: dict | None = None) -> str` — the SKILL.md Phase 2 table + probe decision (Step 2).

- [ ] **Step 1: Write the failing tests** — `tests/unit/test_deepagents_browse_fetch.py`:

```python
from __future__ import annotations

import json
import hashlib

import pytest

from backend.app.services.deepagents_runtime.tools.skill_graphs.browse_fetch import (
    SiteClass,
    classify_url,
    page_file_hash,
    browse_fetch_urls,
    mode_for_class,
)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://mp.weixin.qq.com/s/AbC123", SiteClass.WECHAT),
        ("https://weixin.qq.com/s/xyz", SiteClass.WECHAT),
        ("https://job.mokahr.com/abc", SiteClass.PARALLEL_FETCH),
        ("https://jobs.bytedance.com/...", SiteClass.PARALLEL_FETCH),
        ("https://jobs.feishu.cn/abc", SiteClass.LIST),
        ("https://www.zhipin.com/job/1", SiteClass.SEARCH_INTERACT),
        ("https://unknown.example.com/list", SiteClass.PROBE),
    ],
)
def test_classify_url_table(url: str, expected: SiteClass) -> None:
    assert classify_url(url) == expected


def test_page_file_hash_is_sha256_of_bytes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "page_01.txt")
        Path(path).write_text("职位描述", encoding="utf-8")
        digest, size = page_file_hash(path, out_dir=tmp)
        assert digest == hashlib.sha256(Path(path).read_bytes()).hexdigest()
        assert size == len("职位描述".encode("utf-8"))


def test_parallel_fetch_success_contract(tmp_path) -> None:
    # runner returns the verified parallel-fetch JSON contract
    def fake_runner(script, *, cli_args="", stdin=""):
        assert script == "browse"
        assert "--mode parallel-fetch" in cli_args
        return json.dumps({
            "status": "ok", "url": "https://job.mokahr.com/abc", "mode": "parallel-fetch",
            "title": "公司职位", "content_hash": "sha256_abcd1234",
            "text_path": "output/evidence/run-x/页_01.txt",
            "page_files": ["pages/page_01.txt", "pages/page_02.txt"],
            "page_count": 2, "used_path": "parallel", "text_length": 5000,
        })

    results = browse_fetch_urls(
        ["https://job.mokahr.com/abc"], runner=fake_runner, out_dir=str(tmp_path)
    )
    assert len(results) == 1
    result = results[0]
    assert result.status == "succeeded"
    assert result.mode == "parallel-fetch"
    assert result.used_path == "parallel"
    assert result.terminal_evidence == []
    assert result.cached is False
    assert len(result.page_files) == 2
    assert all(pf.content_hash.startswith("sha256_") is False for pf in result.page_files)  # full hex
```

(Continue with: `test_spa_shell_empty_falls_back_to_search_interact_once`, `test_blocked_never_retried`, `test_error_retried_once_with_wait_5000`, `test_list_mode_terminal_evidence_and_cache_passthrough`, `test_wechat_urls_not_browsed`, `test_parallel_and_search_interact_hard_limits` — one each; the last asserts the fake runner records that at most one `--mode parallel-fetch` and one `--mode search-interact` invocation happened for a URL that exhausted both.)

- [ ] **Step 2: Run tests to verify they fail** — `browse_fetch.py` does not exist yet.

- [ ] **Step 3: Implement `classify_url` + `mode_for_class`** — encode the SKILL.md Phase 2 classification table (implementer reads `skill/job-discovery/SKILL.md` Phase 2 and `site-catalog.md` for the exact host lists; the verified families: `mp.weixin.qq.com`/`weixin.qq.com` articles → `WECHAT`; mokahr / bytedance / Mioffice hosts → `PARALLEL_FETCH`; `jobs.feishu.cn` → `LIST` (max-pages 3); zhipin / zhiye hosts → `SEARCH_INTERACT`; everything else → `PROBE`). `mode_for_class`: `PARALLEL_FETCH → "parallel-fetch"`, `LIST → "list"`, `SEARCH_INTERACT → "search-interact"`, `PROBE → "list"` (probe), `WECHAT → None` (never browsed here).

- [ ] **Step 4: Implement `page_file_hash` + `browse_fetch_urls` with the fallback chain** — per URL, with **hard per-URL caps (one `parallel-fetch` + one `search-interact` maximum across the whole chain, enforced by a counter map)**: primary mode per classification; `PROBE` runs `list` and treats `text_length < 4096` as thin → `search-interact`; `PARALLEL_FETCH` with `page_count == 0` or `used_path` in `{spa_shell_empty_no_evidence, click_fallback_fetch_error}` → `search-interact`; `LIST` with `page_count == 0` → `search-interact`; `status=error` → one retry of the same mode with `--wait 5000`; `status=blocked` → never retried, mapped to `blocked_reason` per the browse output detail (implementer verifies the exact reason key in `browse.py`; the families are login/captcha/anti-bot/unsafe-url/empty-shell). `--cache-mode <cache_mode>` passed only for `list` mode. Each browse call resolves page files against `out_dir` and computes full-hash + length per page via `page_file_hash`; a page file missing on disk is dropped from `page_files` (never crashes). WeChat URLs return `UrlFetchResult(status="wechat_pending", mode=None, page_files=[])` without any browse call.

- [ ] **Step 5: Rewire the fetch node** — `job_discovery_graph.py` fetch node calls `fetch_fn(urls)` (seam unchanged) whose new default `_default_fetch` orchestrates `browse_fetch_urls` and shapes `per_url_results` entries carrying `source_url` + `content_hash` (per-URL evidence = first page hash) + a bounded `visible_text` (first page text, ≤1200 chars) so the harness promotes evidence exactly as before; `status=blocked` URLs map to per-url `error_code="blocked"` (the stall-breaker treats them as no-progress); `wechat_pending` URLs are carried through untouched for Task 9. The old requests fast path (`fetch_public_job_pages`) is removed from the default fetch; the `fetch_fn` seam remains for tests.

- [ ] **Step 6: Full suite + ruff** — `.\.venv\Scripts\python.exe -m pytest tests/unit/ -q` (all PASS, new package still 100% branch) and `.\.venv\Scripts\python.exe -m ruff check backend tests scripts`. Existing `test_deepagents_skill_tool.py` fakes that pinned the old fetch behavior are updated to the new `per_url_results` shape (Task 4 fix round 1 already established that shape; add `mode`/`page_files` keys).

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/deepagents_runtime/tools/skill_graphs/browse_fetch.py backend/app/services/deepagents_runtime/tools/skill_graphs/job_discovery_graph.py tests/unit/test_deepagents_browse_fetch.py tests/unit/test_deepagents_skill_tool.py
git commit -m "feat(deepagents-runtime): browse-backed fetch with site classification + fallback chain

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 8: per-page extraction fan-out + LLM extraction gate (extraction-guide contract)

**Files:**
- Create: `backend/app/services/deepagents_runtime/tools/llm_extractor.py`
- Modify: `backend/app/services/deepagents_runtime/tools/skill_graphs/job_discovery_graph.py` (extract node + dedup node; read candidates from `page_NN.json`)
- Modify: `backend/app/config.py` (add `deepagents_llm_extraction_enabled: bool = False` to `Settings`)
- Test: `tests/unit/test_deepagents_llm_extractor.py`, extend `tests/unit/test_deepagents_skill_tool.py`

**Interfaces:**
- Consumes: `extract_with_gate(context, payload, *, enabled, llm_extractor=None)` (extract_gate.py), `ExtractObservedJobDetailsInput` / `ExtractObservedJobDetailsOutput` / `ExtractedJobDetails` (career_skills, read-only), Task 7 `PageFile`/`UrlFetchResult`, `run_skill_script` stdin, `settings_override` from `tests/conftest.py`.
- Produces:
  - `class LLMJobExtractor` with `__init__(self, settings)` and `__call__(self, context: ToolContext, payload: ExtractObservedJobDetailsInput) -> ExtractObservedJobDetailsOutput`; `build_llm_extractor(settings) -> LLMJobExtractor | None` (None when `deepagents_llm_extraction_enabled` is False).
  - `extract_page(page: PageFile, *, url: str, out_dir: str, context: ToolContext, extract_fn, llm_extractor: Callable | None = None) -> list[ExtractedJobDetails]` — per-page gated extraction.
  - `write_page_candidates(page_id: str, candidates: list[ExtractedJobDetails], *, runner=None, candidates_dir: str) -> int` — stdin contract to `write_candidates`; returns number of accepted candidates (title+company+body survivors).

- [ ] **Step 1: Write the failing tests** — `tests/unit/test_deepagents_llm_extractor.py`:

```python
from __future__ import annotations

import json

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel  # NOT available with bind_tools — use ScriptedModel from tests/unit/deepagents_testkit.py

from backend.app.services.agent_runtime.schemas import ToolObservation
from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.career_skills.job_discovery import (
    ExtractObservedJobDetailsInput,
    ExtractObservedJobDetailsOutput,
)
from backend.app.services.deepagents_runtime.tools.llm_extractor import (
    LLMJobExtractor,
    build_llm_extractor,
)
from tests.conftest import settings_override
from tests.unit.deepagents_testkit import ScriptedModel


def test_build_llm_extractor_respects_flag() -> None:
    off = settings_override(deepagents_llm_extraction_enabled=False)
    assert build_llm_extractor(off) is None
    on = settings_override(deepagents_llm_extraction_enabled=True)
    extractor = build_llm_extractor(on)
    assert extractor is not None
    assert extractor._model is not None


def test_extractor_folds_parse_failure_to_empty_candidates() -> None:
    # ScriptedModel returns prose without any JSON -> extractor must NOT raise;
    # it folds to an empty candidate list (verifier sees honest "no candidates").
    model = ScriptedModel(responses=["这个页面没有可解析的职位内容。"])
    extractor = LLMJobExtractor(settings_override(deepagents_llm_extraction_enabled=True))
    extractor._model = model
    ctx = ToolContext(user_id="u", run_id="r", metadata={})
    output = extractor(ctx, ExtractObservedJobDetailsInput(artifact_id="page_01"))
    assert output.candidates == []
    assert output.content_hash == "page_01"
```

(Continue: `test_extractor_lenient_json_fence_strip` — model returns ```` ```json {…} ``` ```` and the extractor parses it; `test_extractor_uses_extraction_guide_prompt` — asserts the prompt mentions job-title/company/body fields per `extraction-guide.md`; `test_extractor_never_raises_on_model_error` — model raises, extractor folds.)

- [ ] **Step 2: Run tests to verify they fail** — `llm_extractor.py` does not exist.

- [ ] **Step 3: Implement `LLMJobExtractor`** — `ChatOpenAI(model=settings.agent_harness_model, temperature=0, max_tokens=4096)` (same model/params as the eval path). Prompt = the `skill/job-discovery/extraction-guide.md` contract (implementer transcribes its field list and output discipline verbatim into the system prompt). Parse: lenient — strip code fences, find the first `{…}` block, `json.loads`, tolerate trailing prose; validate the parsed list into `ExtractedJobDetails` items (drop invalid ones, never raise). Any model error / validation failure → return `ExtractObservedJobDetailsOutput(source_url="", content_hash=payload.artifact_id, candidates=[])` (fold, never raise). The `extraction-guide.md` field names map onto `ExtractedJobDetails` 12 fields (implementer maps from the schema file, read-only).

- [ ] **Step 4: Rewrite the extract node as per-page fan-out** — for each `UrlFetchResult` with pages: for each `PageFile`: (1) read the page text from its resolved path; (2) register observed evidence `observed:<page.content_hash>` with that text + `source_url` (the same registration pattern the current `_default_extract` uses — verified against `career_skills/job_discovery.py:789-801`); (3) one `extract_with_gate(context, ExtractObservedJobDetailsInput(artifact_id=page.content_hash), enabled=<settings deepagents_llm_extraction_enabled>, llm_extractor=build_llm_extractor(settings))` call per page — the strict-Pareto union of regex + LLM candidates is already in `extract_with_gate`; (4) collect the output's candidates. Per-page evidence dicts (`source_url` + `content_hash` + bounded `visible_text`) enter `evidence_store` exactly as the harness expects. Batch extraction is retained **only** for evidence entries without page files (static fast-path compat; X1 ruling).

- [ ] **Step 5: Persist candidates per page + dedup from page files** — after extraction per page, `write_page_candidates("page_01", candidates, runner=..., candidates_dir=...)` pipes the candidates JSON via stdin to `write_candidates` (the script enforces `output/` + valid-candidate rules and writes `page_NN.json`). The dedup node changes to: glob `page_*.json` (excluding `_merged.*`), run `deduplicate`, parse its stdout counts, read `merged_final.json`, and include `merged_count` + dedup counts in the tool output (U10 ruling). The old glob-merged path is removed.

- [ ] **Step 6: Full suite + ruff** — both new tests and the extended tool tests pass; 100% branch coverage retained (the per-page loop branches — page-file-missing, zero-candidates, LLM-gate-triggered — must each be exercised by a test).

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/deepagents_runtime/tools/llm_extractor.py backend/app/services/deepagents_runtime/tools/skill_graphs/job_discovery_graph.py backend/app/config.py tests/unit/test_deepagents_llm_extractor.py tests/unit/test_deepagents_skill_tool.py
git commit -m "feat(deepagents-runtime): per-page extraction fan-out with LLM gate + page-file candidates

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 9: WeChat image-article (OCR) slice — Levels 1-6 + channel triage + REPLACE-OCR

**Files:**
- Create: `backend/app/services/deepagents_runtime/tools/skill_graphs/wechat_slice.py`
- Modify: `backend/app/services/deepagents_runtime/tools/skill_graphs/job_discovery_graph.py` (route `wechat_pending` URLs into the slice; wire `errors.jsonl` append)
- Test: `tests/unit/test_deepagents_wechat_slice.py`

**Interfaces:**
- Consumes: `run_skill_script` (now invoked with `ocr_image` for the first time), `_ALLOWED_SCRIPTS` (unchanged — `ocr_image` already allowlisted), Task 7 `classify_url`/`UrlFetchResult`, Task 8 `write_page_candidates`/`LLMJobExtractor` (OCR text is extraction input, not a replacement for the gate).
- Produces:
  - `@dataclass WechatResult: url; status: str; channel: str | None; candidates: list[ExtractedJobDetails]; application_channel_json: dict | None; needs_deep_crawl: bool; reason: str | None`.
  - `run_wechat_slice(url: str, *, runner=None, out_dir: str, context: ToolContext, extract_fn, llm_extractor: Callable | None = None) -> WechatResult` — the Level 1-6 pipeline.
  - `classify_wechat_channel(*, article_text: str, ocr_texts: list[str]) -> tuple[str, str | None]` — channel A/B/C/D + reason (pure function).
  - `append_errors_jsonl(entry: dict, *, runner=None, out_dir: str) -> None` — append-one-line to `output/errors.jsonl` (idempotent entries carry `url` + `timestamp`-free cause key).

- [ ] **Step 1: Write the failing tests** — `tests/unit/test_deepagents_wechat_slice.py`:

```python
from __future__ import annotations

import json

import pytest

from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.deepagents_runtime.tools.skill_graphs.wechat_slice import (
    classify_wechat_channel,
    run_wechat_slice,
    append_errors_jsonl,
)


def test_channel_a_job_content_in_text() -> None:
    channel, reason = classify_wechat_channel(
        article_text="岗位：前端工程师。公司：某某科技。负责……",
        ocr_texts=[],
    )
    assert channel == "A"
    assert reason is None


def test_channel_b_job_content_only_in_ocr() -> None:
    channel, reason = classify_wechat_channel(
        article_text="欢迎转发",
        ocr_texts=["招聘：后端工程师，薪资面议，简历投递……"],
    )
    assert channel == "B"
    assert reason is None


def test_channel_c_contact_only() -> None:
    channel, reason = classify_wechat_channel(article_text="加微信: abc", ocr_texts=[])
    assert channel == "C"
    assert reason is not None


def test_channel_d_non_job_promotional() -> None:
    channel, reason = classify_wechat_channel(article_text="双十一大促，全场五折", ocr_texts=[])
    assert channel == "D"
    assert reason is not None


def test_needs_deep_crawl_appends_errors_jsonl(tmp_path) -> None:
    def fake_runner(script, *, cli_args="", stdin=""):
        return json.dumps({"ok": True})  # state/ocr scripts are faked in unit tests

    append_errors_jsonl(
        {"url": "https://mp.weixin.qq.com/s/abc", "cause": "needs_deep_crawl"},
        runner=fake_runner, out_dir=str(tmp_path),
    )
    lines = (tmp_path / "errors.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["url"] == "https://mp.weixin.qq.com/s/abc"
```

(Continue: `test_run_wechat_slice_full_pipeline` — fake runner responds to `browse`-less flow (article fetch is done by the slice's own requests guard — seam `fetch_html_fn`), `ocr_image` returns a fake OCR text, extraction returns one candidate, channel B, `application_channel_json` populated, `needs_deep_crawl=False`; `test_run_wechat_slice_channel_d_skips_extraction`; `test_run_wechat_slice_article_fetch_blocked` — non-public URL / nav failure → `status="blocked"`, `reason` set, no candidates; `test_ocr_image_failure_folds` — ocr script failure does not crash the slice, the image is skipped.)

- [ ] **Step 2: Run tests to verify they fail** — `wechat_slice.py` does not exist.

- [ ] **Step 3: Implement the Level 1-6 pipeline in `run_wechat_slice`** — implementer reads `skill/job-discovery/wechat-image-handling.md` (read-only) and encodes every Level verbatim; the verified skeleton: **L1** URL guard (scheme http(s), no userinfo, global IP — reuse the `_fetch_validated`/`_assert_public_url` semantics from the skill; redirects followed manually, max 5 hops, private/cloud-metadata target → `unsafe_public_url` blocked); **L2** parse the article HTML for `<img>` srcs (drop `data:` URIs); **L3** download each image with the doc's size filters (skip undersized/oversized — never raise); **L4** per surviving image, `run_skill_script("ocr_image", cli_args=...)` (exact flags from `scripts/ocr_image.py`); **L5** combine article text + OCR texts per the doc's combine format; **L6** `classify_wechat_channel`:
  - A = job content sufficient in article text → extract candidates from combined text;
  - B = job content only from OCR (REPLACE-OCR rule: when an image is a job posting, the OCR text replaces the article text as extraction input) → extract candidates from OCR text;
  - C = contact-only (微信/邮箱, no job content) → `needs_manual_review` semantics: `status="needs_manual_review"`, reason, no candidates;
  - D = non-job/promotional → `status="skipped"`, reason, no candidates.
  - `needs_deep_crawl` (doc's condition — e.g. paginated long-form / iframe-embedded content) → `append_errors_jsonl({"url":…, "cause": "needs_deep_crawl"})` and the result carries the flag. Every candidate from channels A/B is enriched with `application_channel_json` per the doc (channel key + source image/OCR metadata), and candidates flow through the same `write_page_candidates` path so dedup/coverage see them.

- [ ] **Step 4: Wire the slice into the graph** — the fetch node's `wechat_pending` results route to `run_wechat_slice` before the extract node; the slice's candidates merge into the per-run candidate set (same `page_NN.json` flow via `write_page_candidates` with a `wechat` page id). `needs_manual_review` URLs surface as per-url `error_code="needs_manual_review"` (a recoverable classification — the human reviews, never auto-retried).

- [ ] **Step 5: Full suite + ruff** — 100% branch coverage retained (each channel branch, both OCR-failure and article-fetch-blocked branches exercised).

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/deepagents_runtime/tools/skill_graphs/wechat_slice.py backend/app/services/deepagents_runtime/tools/skill_graphs/job_discovery_graph.py tests/unit/test_deepagents_wechat_slice.py
git commit -m "feat(deepagents-runtime): WeChat image-article OCR slice (levels 1-6 + channel triage)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 10: incremental persistence — state check/mark, merged accumulation, errors.jsonl, normalize, stable state dir

**Files:**
- Create: `backend/app/services/deepagents_runtime/tools/skill_graphs/persistence.py`
- Modify: `backend/app/services/deepagents_runtime/tools/skill_graphs/job_discovery_graph.py` (state check/mark hooks + normalize node + stable out-dir selection; removes the `tempfile.mkdtemp` leak — Task 4 minor)
- Test: `tests/unit/test_deepagents_persistence.py`, extend `tests/unit/test_deepagents_skill_tool.py`

**Interfaces:**
- Consumes: `run_skill_script` (`state`, `normalize`, `deduplicate`, `write_candidates`), Task 7 `browse_fetch_urls` (now takes `state_dir`), Task 8/9 candidate flows.
- Produces:
  - `state_check(url: str, update_time: str, *, runner=None, state_dir: str) -> bool` (True = skip — exit 0 per `state.py check`).
  - `state_mark(url: str, entry_ids: list[str], *, runner=None, state_dir: str, file_id: str, sheet_id: str) -> None` — one `mark` call per entry id (`entry_id = content_hash[:16]_url_hash8`, verified format).
  - `load_prior_candidates(*, state_dir: str) -> list[dict]` — reads `output/candidates/merged_final.json` if present, else `[]`.
  - `append_errors_jsonl(entry: dict, *, runner=None, state_dir: str) -> None` — moved here from Task 9 (Task 9 imports it from this module).
  - `normalize_candidates(candidates: list[dict], *, runner=None) -> dict[str, str]` — comparison-key map via `normalize.py --json` (never alters stored titles).

- [ ] **Step 1: Write the failing tests** — `tests/unit/test_deepagents_persistence.py`:

```python
from __future__ import annotations

import json
import os

import pytest

from backend.app.services.deepagents_runtime.tools.skill_graphs.persistence import (
    state_check,
    state_mark,
    load_prior_candidates,
    append_errors_jsonl,
    normalize_candidates,
)


class FakeStateRunner:
    def __init__(self, exit_codes: dict[str, int]) -> None:
        self.exit_codes = exit_codes
        self.calls: list[tuple[str, str, str]] = []

    def __call__(self, script, *, cli_args="", stdin=""):
        self.calls.append((script, cli_args, stdin))
        if script == "state" and cli_args.startswith("check "):
            return json.dumps({"exit_code": self.exit_codes.get(cli_args, 0)})
        if script == "state":
            return json.dumps({"marked": True})
        return json.dumps({"ok": True})


def test_state_check_exit_zero_means_skip() -> None:
    runner = FakeStateRunner({"check https://a/1 2026-01-01": 0})
    assert state_check("https://a/1", "2026-01-01", runner=runner, state_dir="x") is True
    runner2 = FakeStateRunner({"check https://a/1 2026-01-01": 1})
    assert state_check("https://a/1", "2026-01-01", runner=runner2, state_dir="x") is False


def test_state_mark_requires_file_and_sheet_id() -> None:
    runner = FakeStateRunner({})
    with pytest.raises(ValueError):
        state_mark("https://a/1", ["h1_u1"], runner=runner, state_dir="x", file_id="", sheet_id="f")
    with pytest.raises(ValueError):
        state_mark("https://a/1", ["h1_u1"], runner=runner, state_dir="x", file_id="f", sheet_id="")
    state_mark("https://a/1", ["h1_u1"], runner=runner, state_dir="x", file_id="f", sheet_id="s")
    assert runner.calls[-1][0] == "state"
    assert "--file-id f" in runner.calls[-1][1] and "--sheet-id s" in runner.calls[-1][1]


def test_load_prior_candidates_missing_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        assert load_prior_candidates(state_dir=tmp) == []


def test_load_prior_candidates_reads_merged_final(tmp_path) -> None:
    out = tmp_path / "output" / "candidates"
    out.mkdir(parents=True)
    (out / "merged_final.json").write_text(json.dumps({"candidates": [{"title": "A"}]}), encoding="utf-8")
    assert load_prior_candidates(state_dir=str(tmp_path)) == [{"title": "A"}]


def test_append_errors_jsonl_appends_lines(tmp_path) -> None:
    append_errors_jsonl({"url": "u1", "cause": "c1"}, runner=FakeStateRunner({}), state_dir=str(tmp_path))
    append_errors_jsonl({"url": "u2", "cause": "c2"}, runner=FakeStateRunner({}), state_dir=str(tmp_path))
    lines = (tmp_path / "output" / "errors.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2


def test_normalize_candidates_returns_comparison_keys() -> None:
    def runner(script, *, cli_args="", stdin=""):
        assert script == "normalize"
        return json.dumps({"normalized_title": "java-工程师", "key": "java工程师"})
    keys = normalize_candidates([{"title": " Java工程师 "}], runner=runner)
    assert keys["key"] == "java工程师"
```

- [ ] **Step 2: Run tests to verify they fail** — `persistence.py` does not exist.

- [ ] **Step 3: Implement `persistence.py`** — `state_check`/`state_mark` map 1:1 to `state.py check/mark` (exit code 0 → skip; mark requires both `--file-id` and `--sheet-id` — a missing flag raises `ValueError`, matching the real script's CLI requirement). `load_prior_candidates` reads `output/candidates/merged_final.json` under `state_dir` (missing → `[]`). `append_errors_jsonl` appends one JSON line to `output/errors.jsonl`, creating dirs. `normalize_candidates` runs `normalize.py --json` over each candidate title/company and returns the comparison-key map (the script's contract: keys only, storage titles untouched).

- [ ] **Step 4: Wire persistence into the graph** — (1) stable state dir replaces `tempfile.mkdtemp(dir=SKILL_DIR)` (Task 4 minor): per-run evidence under `SKILL_DIR/output/evidence/run-<run_id>`; `state.json`, `candidates/merged_final.json`, `errors.jsonl` are the **stable** incremental store (P1-P9 contract: check before each URL's fetch — skip when the state says the URL was already extracted with a matching update time; mark after extraction with the run's file/sheet ids; merged_final accumulates across runs because `write_candidates --append` + `deduplicate` merge prior + new); (2) a **normalize node** runs after dedup — comparison keys enter the tool output (not the stored titles); (3) `errors.jsonl` accumulates `needs_deep_crawl` / `needs_manual_review` entries across runs (Task 9's `append_errors_jsonl` moves to this module). The tool payload gains an optional `prior_metadata` input (file_id/sheet_id/update_time) — absent → no state check/mark (single-shot mode, unchanged behavior for the harness eval).

- [ ] **Step 5: Full suite + ruff** — 100% branch coverage retained (skip-path, mark-path, missing-flag, missing-file, accumulate branches all exercised).

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/deepagents_runtime/tools/skill_graphs/persistence.py backend/app/services/deepagents_runtime/tools/skill_graphs/job_discovery_graph.py tests/unit/test_deepagents_persistence.py tests/unit/test_deepagents_skill_tool.py
git commit -m "feat(deepagents-runtime): incremental persistence (state check/mark, merged accumulation, errors.jsonl)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 11: honest coverage gate (manifest contract) + tool output contract + production wiring

**Files:**
- Modify: `backend/app/services/deepagents_runtime/tools/skill_graphs/job_discovery_graph.py` (coverage node rewrite)
- Modify: `backend/app/services/deepagents_runtime/tools/skill_graphs/__init__.py` (tool output contract: `status` / `pages` / `merged_count` / `coverage` keys; remove the synthesized terminal-evidence and URLs-as-pages paths — Task 4 review gap #2)
- Modify: `backend/app/services/deepagents_runtime/eval/compare_runner.py` (wire `tool_factory` for the deepagents leg — the production-path wiring point)
- Test: extend `tests/unit/test_deepagents_skill_tool.py`, `tests/unit/test_deepagents_compare_runner.py`

**Interfaces:**
- Consumes: Task 7 `UrlFetchResult` (terminal evidence now observed, never synthesized), Task 8 merged candidates + `merged_count`, Task 10 persistence, `run_skill_script` (`coverage_gate` with `--manifest`).
- Produces:
  - `build_manifest(*, out_dir: str, results: list[UrlFetchResult], merged_count: int, listing_count: int | None) -> dict` — honest manifest: `page_files` = real non-zero paths under the evidence dir, `terminal_evidence` = observed markers only (empty list when none), `declared_total_pages`, `pages_collected`, `truncated_by_max_pages`, `listing_count`.
  - Tool output dict now: `{"status": "succeeded" | "blocked" | "failed", "pages": [...], "candidates_file": "...", "merged_count": int, "terminal_evidence": [...], "coverage": {"verified": bool, "page_count": int, "reasons": [...]}, "per_url_results": [...], "candidates": [...]}` — `per_url_results`/`candidates` still carry `source_url` + `content_hash` for evidence promotion; `status="blocked"` (with per-url `error_code="blocked"`) when every URL was blocked, `"failed"` only on runner-level error, `"succeeded"` otherwise (per-url failures recorded inside `pages`).
  - `build_job_discovery_tools(skill_name: str, *, budgets, tracker) -> list[Any]`-shaped factory used by `compare_runner.run_deepagents_question`: for `skill_name == "job-discovery"` returns `[build_job_discovery_tool(fetch_fn=None, script_runner=run_skill_script, extract_fn=None, checkpointer=None)]` (defaults → real browse orchestration + real per-page extraction + in-memory subgraph checkpointer); other skills → `build_skill_tools(skill_name=…, budgets=…, tracker=…)` (existing path).

- [ ] **Step 1: Write the failing tests** — extend `tests/unit/test_deepagents_skill_tool.py`:

```python
def test_coverage_node_passes_real_manifest(tmp_path):
    # runner intercepts coverage_gate and asserts --manifest path exists and
    # its page_files point at real non-empty files; returns the real script's
    # output shape {coverage_verified: True, reasons: [], ...}
    ...

def test_coverage_node_no_synthesized_terminal_evidence():
    # results with terminal_evidence == [] -> manifest.terminal_evidence == []
    # (never derived from the last page hash)

def test_tool_output_contract_blocked_all_urls():
    # all URLs blocked -> status "blocked", error_code "blocked" per url

def test_tool_output_contract_merged_count_present():
    # succeeded path -> merged_count == len(merged candidates)
```

Extend `tests/unit/test_deepagents_compare_runner.py`:

```python
def test_deepagents_leg_wires_job_discovery_tool_factory():
    # monkeypatch cr.DeepAgentsHarness; assert the harness was constructed with
    # a tool_factory and that calling it for skill "job-discovery" yields a
    # single callable tool whose build params route to build_job_discovery_tool
    # (patched at module level); other skills fall back to build_skill_tools.
```

- [ ] **Step 2: Run tests to verify they fail** — the manifest/coverage contract is not implemented (the current node passes URLs as `--pages` and synthesizes terminal evidence — both are the defects this task fixes).

- [ ] **Step 3: Rewrite the coverage node honestly** — build the manifest from the real run outputs (`page_files` = only files that exist and are non-empty under the evidence dir; `terminal_evidence` = only markers actually observed by browse, empty list otherwise; counts from the run); run `coverage_gate --manifest <manifest_path>`; parse `coverage_verified` + reasons + metrics into the output; never fabricate `next_control_absent`/`page_content_repeated`, never pass URLs as page paths. When the coverage script exits non-zero / unparsable (including `missing_terminal_evidence`), the output reports `verified: False` with the reasons string — same safe degradation as Task 4 fix round 1.

- [ ] **Step 4: Tool output contract** — `build_job_discovery_tool.run` returns `ToolObservation(tool_name=…, status=…, error_code=…, output={…})` with the Step 1 contract keys; `status` semantics per Interfaces above. The `_merged.*` glob exclusion from Task 8 prevents double-counting.

- [ ] **Step 5: Wire `compare_runner`** — `run_deepagents_question`'s default harness construction gains `tool_factory=build_job_discovery_tools` (imports from the skill_graphs package); the `harness=None` default path stays the only wiring point (production API wiring remains P2+, documented in the spec — unchanged). Existing compare_runner tests keep passing (the monkeypatched harness seam absorbs the new kwarg).

- [ ] **Step 6: Full suite + ruff** — 100% branch coverage retained; `test_deepagents_skill_tool.py`'s coverage fakes updated to mirror the real `coverage_gate` output shape (the Task 4 residual minor about the no-pages fake is resolved here).

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/deepagents_runtime/tools/skill_graphs/job_discovery_graph.py backend/app/services/deepagents_runtime/tools/skill_graphs/__init__.py backend/app/services/deepagents_runtime/eval/compare_runner.py tests/unit/test_deepagents_skill_tool.py tests/unit/test_deepagents_compare_runner.py
git commit -m "feat(deepagents-runtime): honest manifest coverage gate + tool output contract + eval wiring

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 12: parity gate re-run, Task 6 review minors, honest conclusion

**Files:**
- Modify: `backend/app/services/deepagents_runtime/eval/compare_runner.py` (per-question isolation — minor c)
- Modify: `backend/app/services/deepagents_runtime/eval/summarize*.py` (bucket closure — minor d; exact file per repo layout)
- Modify: `tests/manual/run_deepagents_parity.py` (parity-table run1 mislabel — minor a; SKIP-path claim — minor b)
- Test: `tests/unit/test_deepagents_compare_runner.py`, `tests/unit/test_deepagents_parity.py` (if present)

**Steps:**

- [ ] **Step 1: Fix the 4 Task 6 review minors (recorded in the ledger 2026-08-07):**
  - (a) parity report table rows for `gate`/`SKIP` contradict `parity_run1.log` → fix the report generation to read the log rows (never hardcode).
  - (b) SKIP-path claim overstated: module-level imports execute before the env check → move the imports inside the env-guarded branch (or correct the claim in the log message; pick the code fix if the imports are heavy).
  - (c) `run_comparison` has no per-question isolation: wrap each question's deepagents + legacy legs in `try/except`, record `{"error": …}` in `per_question`, and continue the round (a single failing question must not kill the round).
  - (d) `summarize_comparison` buckets don't close: `unknown` statuses (e.g. `"unknown"` from the harness seam) never enter any tally → add an `"unknown"` bucket so `succeeded + waiting_user + failed + unknown == total` always.

- [ ] **Step 2: Re-run the parity gate** — same 6-URL baseline, same command as Task 6. **Pass** = per the gate: `success ≥ 3` AND `candidates ≥ 797` (no regression vs baseline) AND coverage `verified=True` for the job-discovery step. If the live LLM/browse run needs Playwright + network (the task's known iteration point), follow the Task 6 Step 5 iteration procedure (re-run on transient infra failure; investigate on real regression).

- [ ] **Step 3: Full suite + ruff + commit the minors** — run the full unit suite (100% branch) and ruff; commit the four fixes as one `fix(deepagents-runtime): parity tooling minors` commit (footer convention).

- [ ] **Step 4: Honest conclusion** — write the outcome into the ledger (`.superpowers/sdd/2026-08-07-deepagents-runtime/progress.md`):
  - If the gate passes: record the run numbers, name the parity branch (which modes/URLs reached which paths), and list any residual differences from the skill's behavior that are *documented decisions* (e.g. per-URL tool semantics vs the retired Smartsheet L3 flow — the 7 OUT-OF-SUBGRAPH-SCOPE items from the gap inventory) — never claim "100% identical" if the inventory says otherwise.
  - If the gate fails: new honest conclusion per the parity-gate rule (spec §7: "不劣化才通过") — quantify the delta vs baseline, name the root cause with file:line evidence, and state whether the port decision (full port) or the parity baseline needs revisiting. Do NOT paper over the result.
  - Also record: `user_id=""` production flush limitation remains (eval seam only; API wiring is P2+ per spec) — carried, not silently fixed.

- [ ] **Step 5: Commit the conclusion** (docs/ledger-only commit, footer convention) — after this task the branch is ready for the final whole-branch review.

---

## Extension Self-Review Notes (vs the skill docs)

- The gap inventory (`.superpowers/sdd/2026-08-07-deepagents-runtime/skill-gap-inventory.md`) is the traceability matrix: Tasks 7-12 map onto its MISSING/PARTIAL items by section (browse modes B1-B13 → Task 7; single-url extraction U1-U13 → Task 8; wechat W1-W6 → Task 9; incremental P1-P12 → Task 10; scripts R4/R6/R7 coverage+state+normalize → Tasks 10-11; schema M1-M7 → Task 8 validate/dedup standards). The 7 OUT-OF-SUBGRAPH-SCOPE items are the retired Smartsheet L3 ingestion flow (per-URL tool semantics) — explicitly concluded in Task 12 Step 4, not silently dropped.
- X1/X2 headline contradictions from the inventory are resolved in the extension with the same rulings the skill's own docs make (per-page fan-out canonical; only observed terminal evidence).
- Task 6's parity baseline (success=3, candidates=797) is the gate in Task 12; the extension changes the subgraph behavior, not the baseline.
- All interfaces cross-checked across tasks: `UrlFetchResult` (7→8→11), `PageFile` (7→8), `LLMJobExtractor`/`extract_page` (8→9), `append_errors_jsonl` (9→10 move), `build_manifest` (11), tool output keys (8→11). No task references a name another task does not produce.
