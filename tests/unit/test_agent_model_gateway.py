"""OpenAI-compatible structured gateway behavior for live PEV Agents."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from backend.app.domain.agent_runtime import AgentRole
from backend.app.services.agent_runtime.model_gateway import (
    AgentModelGatewayConfigError,
    AgentModelGatewayError,
    LangChainModelGateway,
    build_agent_model_gateway,
)
from tests.conftest import settings_override
from backend.app.services.agent_runtime.schemas import PlannerDecision


class RecordingModel:
    """The smallest external-model double retaining structured-output behavior."""

    def __init__(self, response: object) -> None:
        self.response = response
        self.messages: list[object] = []
        self.schema: type[BaseModel] | None = None

    def with_structured_output(self, schema: type[BaseModel]) -> "RecordingModel":
        self.schema = schema
        return self

    def invoke(self, messages: list[object]) -> object:
        self.messages = messages
        return self.response


def test_gateway_converts_provider_structured_result_into_requested_agent_decision() -> None:
    """A provider response must be parsed before it can drive a real Agent loop."""
    model = RecordingModel(
        {
            "action": "need_user",
            "user_question": "请确认期望城市。",
        }
    )

    result = LangChainModelGateway(model).decide(
        role=AgentRole.planner,
        instruction="形成计划或提出澄清问题",
        state={"goal": "找 AI 应用开发岗位"},
        response_model=PlannerDecision,
    )

    assert result.action == "need_user"
    assert result.user_question == "请确认期望城市。"
    assert model.schema is PlannerDecision
    assert len(model.messages) == 2


def test_gateway_returns_stable_error_when_provider_output_breaks_agent_schema() -> None:
    """Malformed provider output must stop the run safely rather than become action."""
    model = RecordingModel({"action": "unknown"})

    with pytest.raises(AgentModelGatewayError, match="invalid_model_response"):
        LangChainModelGateway(model).decide(
            role=AgentRole.planner,
            instruction="形成计划",
            state={"goal": "找岗位"},
            response_model=PlannerDecision,
        )


def test_gateway_factory_fails_closed_without_a_model_key(monkeypatch) -> None:
    """An enabled harness must not fall back to a fabricated model decision."""
    monkeypatch.setattr("src.utils.get_api_key", lambda: None)

    with pytest.raises(AgentModelGatewayConfigError, match="missing_api_key"):
        build_agent_model_gateway(settings_override(agent_harness_enabled=True))
