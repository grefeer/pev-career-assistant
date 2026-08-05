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

    def __init__(self, response: object, usage_metadata: dict[str, int] | None = None) -> None:
        self.response = response
        self.usage_metadata = usage_metadata
        self.messages: list[object] = []
        self.schema: type[BaseModel] | None = None
        self.include_raw: bool = False
        self.model: str | None = None

    def with_structured_output(
        self, schema: type[BaseModel], include_raw: bool = False
    ) -> "RecordingModel":
        self.schema = schema
        self.include_raw = include_raw
        return self

    def invoke(self, messages: list[object]) -> object:
        self.messages = messages
        if self.include_raw:
            raw_message = type(
                "RawMessage", (), {"usage_metadata": self.usage_metadata}
            )()
            return {"parsed": self.response, "raw": raw_message}
        return self.response


class JsonOnlyModel:
    """OpenAI-compatible provider double that rejects response_format schemas."""

    def with_structured_output(
        self, _schema: type[BaseModel], include_raw: bool = False
    ) -> "JsonOnlyModel":
        return self

    def invoke(self, _messages: list[object]) -> object:
        if not hasattr(self, "rejected"):
            self.rejected = True
            raise RuntimeError("response_format type is unavailable now")
        return type(
            "RawResponse",
            (),
            {
                "content": '{"action":"need_user","user_question":"请确认城市。"}',
                "usage_metadata": {"input_tokens": 10, "output_tokens": 5},
            },
        )()


class RaisingModel:
    def with_structured_output(
        self, _schema: type[BaseModel], include_raw: bool = False
    ) -> "RaisingModel":
        raise RuntimeError("provider is down")


class FallbackResponseModel:
    def with_structured_output(
        self, _schema: type[BaseModel], include_raw: bool = False
    ) -> "FallbackResponseModel":
        raise RuntimeError("response_format not supported")

    def __init__(self, content: object | Exception) -> None:
        self.content = content

    def invoke(self, _messages: list[object]) -> object:
        if isinstance(self.content, Exception):
            raise self.content
        return type(
            "RawResponse",
            (),
            {
                "content": self.content,
                "usage_metadata": {"input_tokens": 10, "output_tokens": 5},
            },
        )()


class LocalJsonPreferredModel:
    """A provider that must never receive LangChain response_format wiring."""

    def __init__(self) -> None:
        self.structured_requested = False

    def with_structured_output(
        self, _schema: type[BaseModel], include_raw: bool = False
    ) -> "LocalJsonPreferredModel":
        self.structured_requested = True
        raise AssertionError("structured protocol must not be requested")

    def invoke(self, _messages: list[object]) -> object:
        return type(
            "RawResponse",
            (),
            {
                "content": '{"action":"need_user","user_question":"请确认城市。"}',
                "usage_metadata": {"input_tokens": 10, "output_tokens": 5},
            },
        )()


class SequencedLocalJsonModel(LocalJsonPreferredModel):
    """JSON-only provider whose bounded recovery responses are controllable."""

    def __init__(self, responses: list[str | Exception]) -> None:
        super().__init__()
        self.responses = responses

    def invoke(self, _messages: list[object]) -> object:
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return type(
            "RawResponse",
            (),
            {
                "content": response,
                "usage_metadata": {"input_tokens": 10, "output_tokens": 5},
            },
        )()


class InvalidStructuredThenJsonModel:
    """Provider returns an invalid structured object but supports ordinary JSON retry."""

    def with_structured_output(
        self, _schema: type[BaseModel], include_raw: bool = False
    ) -> "InvalidStructuredThenJsonModel":
        self.structured = True
        return self

    def invoke(self, _messages: list[object]) -> object:
        if getattr(self, "structured", False):
            self.structured = False
            return {"parsed": {"action": "not-valid"}, "raw": None}
        return type(
            "RawResponse",
            (),
            {
                "content": '{"action":"need_user","user_question":"请确认城市。"}',
                "usage_metadata": {"input_tokens": 10, "output_tokens": 5},
            },
        )()


class InvalidStructuredThenFailureModel:
    """A malformed structured result must preserve a subsequent provider outage."""

    def with_structured_output(
        self, _schema: type[BaseModel], include_raw: bool = False
    ) -> "InvalidStructuredThenFailureModel":
        self.structured = True
        return self

    def invoke(self, _messages: list[object]) -> object:
        if getattr(self, "structured", False):
            self.structured = False
            return {"parsed": {"action": "not-valid"}, "raw": None}
        raise RuntimeError("ordinary JSON retry is down")


class InvalidStructuredTwiceThenJsonModel:
    """A provider can recover on the one extra bounded JSON retry."""

    def __init__(self) -> None:
        self.json_attempts = 0

    def with_structured_output(
        self, _schema: type[BaseModel], include_raw: bool = False
    ) -> "InvalidStructuredTwiceThenJsonModel":
        self.structured = True
        return self

    def invoke(self, _messages: list[object]) -> object:
        if getattr(self, "structured", False):
            self.structured = False
            return {"parsed": {"action": "not-valid"}, "raw": None}
        self.json_attempts += 1
        content = (
            '{"action":"not-valid"}'
            if self.json_attempts == 1
            else '{"action":"need_user","user_question":"请确认城市。"}'
        )
        return type(
            "RawResponse",
            (),
            {"content": content, "usage_metadata": {"input_tokens": 10, "output_tokens": 5}},
        )()


class InvalidStructuredThenRetryFailureModel:
    """The second protocol retry must preserve a provider outage code."""

    def __init__(self) -> None:
        self.json_attempts = 0

    def with_structured_output(
        self, _schema: type[BaseModel], include_raw: bool = False
    ) -> "InvalidStructuredThenRetryFailureModel":
        self.structured = True
        return self

    def invoke(self, _messages: list[object]) -> object:
        if getattr(self, "structured", False):
            self.structured = False
            return {"parsed": {"action": "not-valid"}, "raw": None}
        self.json_attempts += 1
        if self.json_attempts == 1:
            return type(
                "RawResponse",
                (),
                {
                    "content": '{"action":"not-valid"}',
                    "usage_metadata": {"input_tokens": 10, "output_tokens": 5},
                },
            )()
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


def test_gateway_retries_one_malformed_preferred_local_json_completion() -> None:
    model = SequencedLocalJsonModel([
        '{"action":"not-valid"}',
        '{"action":"need_user","user_question":"请确认城市。"}',
    ])

    result = LangChainModelGateway(model, prefer_local_json_validation=True).decide(
        role=AgentRole.planner,
        instruction="形成计划",
        state={"goal": "找岗位"},
        response_model=PlannerDecision,
    )

    assert result.action == "need_user"
    assert model.responses == []


def test_gateway_retries_two_malformed_preferred_local_json_completions() -> None:
    model = SequencedLocalJsonModel([
        '{"action":"not-valid"}',
        '{"action":"not-valid"}',
        '{"action":"need_user","user_question":"请确认城市。"}',
    ])

    result = LangChainModelGateway(model, prefer_local_json_validation=True).decide(
        role=AgentRole.planner,
        instruction="形成计划",
        state={"goal": "找岗位"},
        response_model=PlannerDecision,
    )

    assert result.action == "need_user"
    assert model.responses == []


@pytest.mark.parametrize(
    "responses, error_code",
    [
        ([RuntimeError("provider down")], "model_request_failed"),
        (['{"action":"not-valid"}', RuntimeError("provider down")], "model_request_failed"),
        (
            ['{"action":"not-valid"}', '{"action":"not-valid"}', RuntimeError("provider down")],
            "model_request_failed",
        ),
        (
            [
                '{"action":"not-valid"}',
                '{"action":"not-valid"}',
                '{"action":"not-valid"}',
            ],
            "invalid_model_response",
        ),
    ],
)
def test_gateway_fails_safely_when_preferred_local_json_retry_cannot_recover(
    responses: list[str | Exception], error_code: str
) -> None:
    with pytest.raises(AgentModelGatewayError, match=error_code):
        LangChainModelGateway(
            SequencedLocalJsonModel(responses), prefer_local_json_validation=True
        ).decide(
            role=AgentRole.planner,
            instruction="形成计划",
            state={"goal": "找岗位"},
            response_model=PlannerDecision,
        )


def test_gateway_factory_fails_closed_without_a_model_key(monkeypatch) -> None:
    """An enabled harness must not fall back to a fabricated model decision."""
    monkeypatch.setattr(
        "backend.app.services.agent_runtime.provider_config.get_api_key", lambda: None
    )

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

    monkeypatch.setattr(
        "backend.app.services.agent_runtime.provider_config.get_api_key", lambda: "key"
    )
    monkeypatch.setattr(
        "backend.app.services.agent_runtime.provider_config.get_base_url",
        lambda: "https://api.deepseek.example",
    )
    monkeypatch.setattr(
        "backend.app.services.agent_runtime.model_gateway.ChatOpenAI", CapturingChatModel
    )

    gateway = build_agent_model_gateway(
        settings_override(agent_harness_model="deepseek-v4-chat")
    )

    assert isinstance(gateway, LangChainModelGateway)
    assert captured["extra_body"] == {"thinking": {"type": "disabled"}}
    assert gateway._prefer_local_json_validation is True


def test_gateway_last_usage_none_when_no_usage_metadata() -> None:
    """last_usage is None when the model provides no usage metadata."""
    model = RecordingModel({"action": "need_user", "user_question": "请确认城市。"})
    gateway = LangChainModelGateway(model)

    result = gateway.decide(
        role=AgentRole.planner,
        instruction="形成计划",
        state={"goal": "找岗位"},
        response_model=PlannerDecision,
    )

    assert result.action == "need_user"
    assert gateway.last_usage is None


def test_gateway_last_usage_populated_from_usage_metadata() -> None:
    """last_usage is populated correctly from the model's usage_metadata."""
    model = RecordingModel(
        {"action": "need_user", "user_question": "请确认城市。"},
        usage_metadata={"input_tokens": 150, "output_tokens": 50},
    )
    model.model = "test-model"
    gateway = LangChainModelGateway(model)

    result = gateway.decide(
        role=AgentRole.planner,
        instruction="形成计划",
        state={"goal": "找岗位"},
        response_model=PlannerDecision,
    )

    assert result.action == "need_user"
    assert gateway.last_usage is not None
    assert gateway.last_usage["model_name"] == "test-model"
    assert gateway.last_usage["input_tokens"] == 150
    assert gateway.last_usage["output_tokens"] == 50


def test_gateway_last_usage_reset_between_decide_calls() -> None:
    """last_usage is overwritten/reset between decide() calls on the same gateway."""

    class SequentialRecordingModel:
        """Return different responses with different usage on sequential calls."""

        def __init__(self, responses_with_usage: list[tuple[object, dict | None]]) -> None:
            self.responses_with_usage = responses_with_usage
            self.messages: list[object] = []
            self.model: str | None = "sequential-model"

        def with_structured_output(
            self, _schema: type[BaseModel], include_raw: bool = False
        ) -> "SequentialRecordingModel":
            return self

        def invoke(self, messages: list[object]) -> object:
            self.messages = messages
            response, usage = self.responses_with_usage.pop(0)
            raw_message = type("RawMessage", (), {"usage_metadata": usage})()
            return {"parsed": response, "raw": raw_message}

    model = SequentialRecordingModel([
        ({"action": "need_user", "user_question": "请确认城市。"},
         {"input_tokens": 150, "output_tokens": 50}),
        ({"action": "need_user", "user_question": "请确认城市。"},
         {"input_tokens": 300, "output_tokens": 75}),
    ])
    gateway = LangChainModelGateway(model)

    gateway.decide(
        role=AgentRole.planner,
        instruction="形成计划",
        state={"goal": "找岗位"},
        response_model=PlannerDecision,
    )
    assert gateway.last_usage is not None
    assert gateway.last_usage["input_tokens"] == 150
    assert gateway.last_usage["output_tokens"] == 50

    gateway.decide(
        role=AgentRole.planner,
        instruction="形成计划",
        state={"goal": "找岗位"},
        response_model=PlannerDecision,
    )
    assert gateway.last_usage is not None
    # Second call's usage should overwrite the first call's values
    assert gateway.last_usage["input_tokens"] == 300
    assert gateway.last_usage["output_tokens"] == 75


def test_gateway_last_usage_populated_for_local_json_validation_path() -> None:
    """last_usage works for the local JSON validation fallback path."""
    model = JsonOnlyModel()
    gateway = LangChainModelGateway(model)

    result = gateway.decide(
        role=AgentRole.planner,
        instruction="形成计划",
        state={"goal": "找岗位"},
        response_model=PlannerDecision,
    )

    assert result.action == "need_user"
    assert gateway.last_usage is not None
    assert gateway.last_usage["input_tokens"] == 10
    assert gateway.last_usage["output_tokens"] == 5
