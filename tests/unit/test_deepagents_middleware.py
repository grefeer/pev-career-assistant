from __future__ import annotations

import asyncio

import pytest

from backend.app.services.deepagents_runtime.budgets import (
    DeepAgentsBudgets,
    TurnBudgetExhausted,
)
from backend.app.services.deepagents_runtime.middleware import (
    ToolExclusionMiddleware,
    TurnBudgetMiddleware,
    current_budgets,
)


class _NamedTool:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeModelRequest:
    def __init__(self) -> None:
        self.tools = [{"name": "keep"}]

    def override(self, **overrides) -> "_FakeModelRequest":
        request = _FakeModelRequest()
        request.tools = overrides.get("tools", self.tools)
        return request


class _FakeModelResponse:
    pass


def _handler(request):
    return _FakeModelResponse()


async def _async_handler(request):
    return _FakeModelResponse()


def _make_request(tools: list) -> _FakeModelRequest:
    request = _FakeModelRequest()
    request.tools = tools
    return request


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


def test_turn_middleware_async_counts_and_exhausts() -> None:
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
    middleware = TurnBudgetMiddleware()
    asyncio.run(middleware.awrap_model_call(_FakeModelRequest(), _async_handler))


def test_exclusion_middleware_filters_tools() -> None:
    middleware = ToolExclusionMiddleware(excluded=frozenset({"execute"}))
    seen: list[str] = []

    def recording_handler(request):
        seen.extend(t["name"] for t in request.tools)
        return _FakeModelResponse()

    middleware.wrap_model_call(_FakeModelRequest(), recording_handler)
    assert seen == ["keep"]


def test_exclusion_middleware_async_filters_tools() -> None:
    middleware = ToolExclusionMiddleware(excluded=frozenset({"execute"}))
    seen: list[str] = []

    async def recording_handler(request):
        seen.extend(t["name"] for t in request.tools)
        return _FakeModelResponse()

    asyncio.run(middleware.awrap_model_call(_FakeModelRequest(), recording_handler))
    assert seen == ["keep"]


def test_exclusion_middleware_empty_excluded_passes_tools_through() -> None:
    middleware = ToolExclusionMiddleware(excluded=frozenset())
    seen: list[str] = []

    def recording_handler(request):
        seen.extend(t["name"] for t in request.tools)
        return _FakeModelResponse()

    async def async_recording_handler(request):
        seen.extend(t["name"] for t in request.tools)
        return _FakeModelResponse()

    middleware.wrap_model_call(_FakeModelRequest(), recording_handler)
    asyncio.run(middleware.awrap_model_call(_FakeModelRequest(), async_recording_handler))
    assert seen == ["keep", "keep"]


def test_exclusion_middleware_handles_object_tools_and_non_string_names() -> None:
    middleware = ToolExclusionMiddleware(excluded=frozenset({"execute"}))
    tools = [
        {"name": "keep"},
        {"name": 123},  # non-string dict name -> kept (None is never excluded)
        _NamedTool("execute"),  # object tool -> excluded via getattr name
        _NamedTool("search"),
    ]
    sync_seen: list = []
    async_seen: list = []

    def label(tool) -> object:  # noqa: ANN001
        return tool["name"] if isinstance(tool, dict) else tool.name

    def sync_handler(request):
        sync_seen.extend(label(t) for t in request.tools)
        return _FakeModelResponse()

    async def async_handler(request):
        async_seen.extend(label(t) for t in request.tools)
        return _FakeModelResponse()

    middleware.wrap_model_call(_make_request(tools), sync_handler)

    async def scenario() -> None:
        await middleware.awrap_model_call(_make_request(tools), async_handler)

    asyncio.run(scenario())

    assert sync_seen == ["keep", 123, "search"]
    assert async_seen == ["keep", 123, "search"]
