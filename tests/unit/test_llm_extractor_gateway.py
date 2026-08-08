"""C1 (FindJobs port): LLMJobExtractor inherits the model_gateway drift ladder
(docs/findjobs-optimization-plan.zh-CN.md §6.1).

The extractor's outbound model is wired through the gateway's shared provider
builder (deepseek-v4: thinking disabled + json_mode, output capped at
max_tokens 4096), and every invocation climbs the drift ladder: one call,
then up to two corrective-hint retries, then AgentModelGatewayError, which
always folds to an honest empty output - never a bare exception.  All
fixtures are deterministic fakes; no network/LLM/DB.
"""

from __future__ import annotations

from langchain_core.messages import AIMessage

from backend.app.services.agent_runtime import model_gateway as mg
from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.career_skills.job_discovery import (
    ExtractObservedJobDetailsInput,
)
from backend.app.services.deepagents_runtime.tools import llm_extractor as le
from tests.conftest import settings_override


def _ctx(text: str = "岗位：后端工程师\n任职要求：精通 Python") -> ToolContext:
    return ToolContext(
        user_id="u",
        run_id="r",
        metadata={
            "observed_public_evidence": [
                {
                    "artifact_id": "page_01",
                    "source_url": "https://example.com/jobs",
                    "content_hash": "page_01",
                    "visible_text": text,
                }
            ]
        },
    )


def _extractor_with(model, *, structured_method: str = "json_mode") -> le.LLMJobExtractor:
    extractor = le.LLMJobExtractor(
        settings_override(deepagents_llm_extraction_enabled=True)
    )
    extractor._model = model
    extractor._structured_method = structured_method
    return extractor


class _ReplayModel:
    """Replays scripted responses in order (last response repeats) and records
    every invocation's messages and kwargs."""

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[list] = []
        self.kwargs: dict = {}

    def invoke(self, messages, **kwargs) -> AIMessage:  # noqa: ANN001
        self.calls.append(list(messages))
        self.kwargs = dict(kwargs)
        index = min(len(self.calls) - 1, len(self.responses) - 1)
        return AIMessage(content=self.responses[index])


class _NonStringModel:
    """Returns a non-string completion (list content), counting calls."""

    def __init__(self) -> None:
        self.calls = 0

    def invoke(self, messages, **kwargs) -> AIMessage:  # noqa: ANN001
        self.calls += 1
        return AIMessage(content=["not", "a", "string"])


def _capture_chat_model(monkeypatch, base_url: str) -> dict[str, object]:
    captured: dict[str, object] = {}

    class CapturingChatModel:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(
        "backend.app.services.agent_runtime.provider_config.get_api_key",
        lambda: "key",
    )
    monkeypatch.setattr(
        "backend.app.services.agent_runtime.provider_config.get_base_url",
        lambda: base_url,
    )
    monkeypatch.setattr(mg, "ChatOpenAI", CapturingChatModel)
    return captured


def test_extractor_uses_shared_deepseek_provider_wiring(monkeypatch) -> None:
    """C1: the outbound call is the gateway's provider transport (json_mode +
    thinking disabled) with the extraction-specific 4096 output cap."""
    captured = _capture_chat_model(monkeypatch, "https://api.deepseek.example")

    extractor = le.LLMJobExtractor(
        settings_override(
            deepagents_llm_extraction_enabled=True,
            agent_harness_model="deepseek-v4-chat",
        )
    )

    assert extractor._structured_method == "json_mode"
    assert captured["extra_body"] == {"thinking": {"type": "disabled"}}
    assert captured["max_tokens"] == 4096
    assert captured["temperature"] == 0


def test_keyless_construction_falls_back_without_crashing(monkeypatch) -> None:
    """A missing API key must not crash construction; the keyless fallback
    model keeps json_mode so a real invocation folds, never raises."""
    monkeypatch.setattr(
        "backend.app.services.agent_runtime.provider_config.get_api_key",
        lambda: None,
    )

    extractor = le.LLMJobExtractor(
        settings_override(deepagents_llm_extraction_enabled=True)
    )

    assert extractor._structured_method == "json_mode"
    assert isinstance(extractor._model, mg.ChatOpenAI)


def test_recovery_retries_then_folds_to_empty() -> None:
    """Three junk completions: 3 attempts (1 + 2 corrective retries), then the
    ladder ends in an honest empty output, never a raise."""
    model = _ReplayModel(responses=["没有职位", "仍是文本", "还是没有 JSON"])
    output = _extractor_with(model)(_ctx(), ExtractObservedJobDetailsInput(artifact_id="page_01"))
    assert output.candidates == []
    assert output.content_hash == "page_01"
    assert len(model.calls) == 3  # 1 call + 2 corrective retries


def test_recovery_succeeds_after_corrective_retry() -> None:
    """A parseable completion on the third attempt: the corrective hint is
    appended after each junk completion and the surviving payload parses."""
    model = _ReplayModel(
        responses=[
            "以下是职位概览：",
            "抱歉，我重新组织一下：",
            '{"title": "后端工程师", "company_name": "示例公司"}',
        ]
    )
    output = _extractor_with(model)(_ctx(), ExtractObservedJobDetailsInput(artifact_id="page_01"))
    assert [c.title for c in output.candidates] == ["后端工程师"]
    assert len(model.calls) == 3
    assert model.calls[0][0].content.startswith("你是一个职位信息结构化提取器")
    assert model.calls[0][1].content == _ctx().metadata["observed_public_evidence"][0]["visible_text"]
    assert model.calls[2][-1].content == le._RECOVERY_HINT


def test_non_string_completion_folds_immediately() -> None:
    """A non-string completion is not retried: it is a provider protocol
    violation, not a parse miss (one call, then fold)."""
    model = _NonStringModel()
    output = _extractor_with(model)(_ctx(), ExtractObservedJobDetailsInput(artifact_id="page_01"))
    assert output.candidates == []
    assert model.calls == 1


def test_json_mode_invokes_with_json_object_response_format() -> None:
    """json_mode providers receive the json_object response_format on the wire
    (the deepseek json_mode protocol), observable in the invocation kwargs."""
    model = _ReplayModel(responses=['{"title": "后端工程师", "company_name": "示例公司"}'])
    output = _extractor_with(model)(_ctx(), ExtractObservedJobDetailsInput(artifact_id="page_01"))
    assert [c.title for c in output.candidates] == ["后端工程师"]
    assert model.kwargs == {"response_format": {"type": "json_object"}}


def test_json_schema_method_invokes_without_response_format() -> None:
    """Non-deepseek providers keep the plain transport (no response_format);
    the lenient parse still accepts the completion."""
    model = _ReplayModel(responses=['{"title": "后端工程师", "company_name": "示例公司"}'])
    output = _extractor_with(model, structured_method="json_schema")(
        _ctx(), ExtractObservedJobDetailsInput(artifact_id="page_01")
    )
    assert [c.title for c in output.candidates] == ["后端工程师"]
    assert model.kwargs == {}


def test_shared_builder_omits_max_tokens_for_decision_gateways(monkeypatch) -> None:
    """The decision gateways keep the provider default output cap (no
    max_tokens kwarg) - the extractor's cap is caller-specific."""
    captured = _capture_chat_model(monkeypatch, "https://api.deepseek.example")

    model, structured_method = mg.build_agent_chat_model(
        settings_override(agent_harness_model="deepseek-v4-chat")
    )

    assert structured_method == "json_mode"
    assert "max_tokens" not in captured
    assert model is not None


def test_shared_builder_non_deepseek_provider_stays_plain(monkeypatch) -> None:
    """A non-deepseek provider keeps json_schema and no thinking toggle."""
    captured = _capture_chat_model(monkeypatch, "https://api.openai.example")

    model, structured_method = mg.build_agent_chat_model(
        settings_override(agent_harness_model="gpt-4o")
    )

    assert structured_method == "json_schema"
    assert "extra_body" not in captured
    assert model is not None


def test_shared_builder_fails_closed_without_api_key(monkeypatch) -> None:
    monkeypatch.setattr(
        "backend.app.services.agent_runtime.provider_config.get_api_key",
        lambda: None,
    )
    import pytest

    with pytest.raises(mg.AgentModelGatewayConfigError, match="missing_api_key"):
        mg.build_agent_chat_model(settings_override())
