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
    _role_action_contract,
    _strip_json_fence,
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


class JsonOnlyModel:
    """OpenAI-compatible provider double that rejects response_format schemas."""

    def with_structured_output(self, _schema: type[BaseModel]) -> "JsonOnlyModel":
        return self

    def invoke(self, _messages: list[object]) -> object:
        if not hasattr(self, "rejected"):
            self.rejected = True
            raise RuntimeError("response_format type is unavailable now")
        return type("RawResponse", (), {"content": '{"action":"need_user","user_question":"请确认城市。"}'})()


class RaisingModel:
    def with_structured_output(self, _schema: type[BaseModel]) -> "RaisingModel":
        raise RuntimeError("provider is down")


class FallbackResponseModel:
    def with_structured_output(self, _schema: type[BaseModel]) -> "FallbackResponseModel":
        raise RuntimeError("response_format not supported")

    def __init__(self, content: object | Exception) -> None:
        self.content = content

    def invoke(self, _messages: list[object]) -> object:
        if isinstance(self.content, Exception):
            raise self.content
        return type("RawResponse", (), {"content": self.content})()


class LocalJsonPreferredModel:
    """A provider that must never receive LangChain response_format wiring."""

    def __init__(self) -> None:
        self.structured_requested = False

    def with_structured_output(self, _schema: type[BaseModel]) -> "LocalJsonPreferredModel":
        self.structured_requested = True
        raise AssertionError("structured protocol must not be requested")

    def invoke(self, _messages: list[object]) -> object:
        return type(
            "RawResponse",
            (),
            {"content": '{"action":"need_user","user_question":"请确认城市。"}'},
        )()


class InvalidStructuredThenJsonModel:
    """Provider returns an invalid structured object but supports ordinary JSON retry."""

    def with_structured_output(self, _schema: type[BaseModel]) -> "InvalidStructuredThenJsonModel":
        self.structured = True
        return self

    def invoke(self, _messages: list[object]) -> object:
        if getattr(self, "structured", False):
            self.structured = False
            return {"action": "not-valid"}
        return type("RawResponse", (), {"content": '{"action":"need_user","user_question":"请确认城市。"}'})()


class InvalidStructuredThenFailureModel:
    """A malformed structured result must preserve a subsequent provider outage."""

    def with_structured_output(self, _schema: type[BaseModel]) -> "InvalidStructuredThenFailureModel":
        self.structured = True
        return self

    def invoke(self, _messages: list[object]) -> object:
        if getattr(self, "structured", False):
            self.structured = False
            return {"action": "not-valid"}
        raise RuntimeError("ordinary JSON retry is down")


class InvalidStructuredTwiceThenJsonModel:
    """A provider can recover on the one extra bounded JSON retry."""

    def __init__(self) -> None:
        self.json_attempts = 0

    def with_structured_output(
        self, _schema: type[BaseModel]
    ) -> "InvalidStructuredTwiceThenJsonModel":
        self.structured = True
        return self

    def invoke(self, _messages: list[object]) -> object:
        if getattr(self, "structured", False):
            self.structured = False
            return {"action": "not-valid"}
        self.json_attempts += 1
        content = (
            '{"action":"not-valid"}'
            if self.json_attempts == 1
            else '{"action":"need_user","user_question":"请确认城市。"}'
        )
        return type("RawResponse", (), {"content": content})()


class InvalidStructuredThenRetryFailureModel:
    """The second protocol retry must preserve a provider outage code."""

    def __init__(self) -> None:
        self.json_attempts = 0

    def with_structured_output(
        self, _schema: type[BaseModel]
    ) -> "InvalidStructuredThenRetryFailureModel":
        self.structured = True
        return self

    def invoke(self, _messages: list[object]) -> object:
        if getattr(self, "structured", False):
            self.structured = False
            return {"action": "not-valid"}
        self.json_attempts += 1
        if self.json_attempts == 1:
            return type("RawResponse", (), {"content": '{"action":"not-valid"}'})()
        raise RuntimeError("retry is down")


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


def test_gateway_retries_one_invalid_structured_response_with_local_json_validation() -> None:
    result = LangChainModelGateway(InvalidStructuredThenJsonModel()).decide(
        role=AgentRole.planner,
        instruction="形成计划",
        state={"goal": "找岗位"},
        response_model=PlannerDecision,
    )

    assert result.action == "need_user"


def test_gateway_preserves_provider_failure_after_an_invalid_structured_result() -> None:
    with pytest.raises(AgentModelGatewayError, match="model_request_failed"):
        LangChainModelGateway(InvalidStructuredThenFailureModel()).decide(
            role=AgentRole.planner,
            instruction="形成计划",
            state={"goal": "找岗位"},
            response_model=PlannerDecision,
        )


def test_gateway_retries_one_malformed_ordinary_json_completion() -> None:
    model = InvalidStructuredTwiceThenJsonModel()

    result = LangChainModelGateway(model).decide(
        role=AgentRole.planner,
        instruction="形成计划",
        state={"goal": "找岗位"},
        response_model=PlannerDecision,
    )

    assert result.action == "need_user"
    assert model.json_attempts == 2


def test_gateway_preserves_provider_failure_after_a_malformed_json_retry() -> None:
    with pytest.raises(AgentModelGatewayError, match="model_request_failed"):
        LangChainModelGateway(InvalidStructuredThenRetryFailureModel()).decide(
            role=AgentRole.planner,
            instruction="形成计划",
            state={"goal": "找岗位"},
            response_model=PlannerDecision,
        )


def test_gateway_falls_back_to_locally_validated_json_when_provider_rejects_response_format() -> None:
    """Provider compatibility fallback still enforces the exact Agent decision schema."""
    result = LangChainModelGateway(JsonOnlyModel()).decide(
        role=AgentRole.planner,
        instruction="形成计划",
        state={"goal": "找岗位"},
        response_model=PlannerDecision,
    )

    assert result.action == "need_user"
    assert result.user_question == "请确认城市。"


def test_gateway_can_prefer_locally_validated_json_without_requesting_response_format() -> None:
    model = LocalJsonPreferredModel()

    result = LangChainModelGateway(model, prefer_local_json_validation=True).decide(
        role=AgentRole.planner,
        instruction="形成计划",
        state={"goal": "找岗位"},
        response_model=PlannerDecision,
    )

    assert result.action == "need_user"
    assert model.structured_requested is False


def test_gateway_factory_fails_closed_without_a_model_key(monkeypatch) -> None:
    """An enabled harness must not fall back to a fabricated model decision."""
    monkeypatch.setattr("src.utils.get_api_key", lambda: None)

    with pytest.raises(AgentModelGatewayConfigError, match="missing_api_key"):
        build_agent_model_gateway(settings_override(agent_harness_enabled=True))


@pytest.mark.parametrize(
    "model",
    [RaisingModel(), FallbackResponseModel(RuntimeError("fallback down"))],
)
def test_gateway_maps_provider_failures_to_a_stable_error(model: object) -> None:
    with pytest.raises(AgentModelGatewayError, match="model_request_failed"):
        LangChainModelGateway(model).decide(
            role=AgentRole.planner, instruction="形成计划", state={}, response_model=PlannerDecision
        )


@pytest.mark.parametrize("content", [None, "not json", '{"action":"bad"}'])
def test_gateway_rejects_non_schema_json_fallback_output(content: object) -> None:
    with pytest.raises(AgentModelGatewayError, match="invalid_model_response"):
        LangChainModelGateway(FallbackResponseModel(content)).decide(
            role=AgentRole.planner, instruction="形成计划", state={}, response_model=PlannerDecision
        )


def test_gateway_accepts_fenced_json_and_describes_every_role_contract() -> None:
    result = LangChainModelGateway(
        FallbackResponseModel('```json\n{"action":"need_user","user_question":"北京？"}\n```')
    ).decide(role=AgentRole.planner, instruction="形成计划", state={}, response_model=PlannerDecision)

    assert result.user_question == "北京？"
    assert _strip_json_fence("```\n{}\n```") == "{}"
    assert _strip_json_fence("{}") == "{}"
    assert "complete" in _role_action_contract(AgentRole.executor)
    assert "REPLAN" in _role_action_contract(AgentRole.verifier)


def test_gateway_factory_configures_deepseek_thinking_mode(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class CapturingChatModel:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr("src.utils.get_api_key", lambda: "key")
    monkeypatch.setattr("src.utils.get_base_url", lambda: "https://api.deepseek.example")
    monkeypatch.setattr(
        "backend.app.services.agent_runtime.model_gateway.ChatOpenAI", CapturingChatModel
    )

    gateway = build_agent_model_gateway(
        settings_override(agent_harness_model="deepseek-v4-chat")
    )

    assert isinstance(gateway, LangChainModelGateway)
    assert captured["extra_body"] == {"thinking": {"type": "disabled"}}
    assert gateway._prefer_local_json_validation is True
