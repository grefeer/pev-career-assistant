from __future__ import annotations

from backend.app.services.deepagents_runtime.agents import (
    build_executor_agent,
    build_planner_agent,
    build_verifier_agent,
)
from tests.unit.deepagents_testkit import ScriptedModel


def _fake_model(*responses: str):
    return ScriptedModel(responses=list(responses))


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
