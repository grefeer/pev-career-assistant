"""Shared deterministic model for deepagents runtime tests.

The installed ``deepagents==0.6.12`` / ``langchain-core 1.4.9`` stack
cannot drive plain ``FakeListChatModel`` through ``create_deep_agent``:
the agent factory binds tools via ``model.bind_tools`` on every invocation,
and ``BaseChatModel.bind_tools`` raises ``NotImplementedError`` unless a
subclass overrides it.  ``ScriptedModel`` therefore subclasses
``GenericFakeChatModel`` (which accepts mixed ``str``/``AIMessage``
responses) and:

- ``bind_tools`` -> self, so tool/response-format binding is a no-op;
- ``profile = {"structured_output": True}``, which makes the factory pick
  the provider strategy, so a scripted JSON string in the AIMessage content
  is parsed into the requested pydantic schema and lands in the
  ``structured_response`` channel (the harness's extraction seam);
- cycles its script instead of raising on exhaustion, mirroring
  ``FakeListChatModel``'s wrap-around semantics.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar, Sequence

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable


class ScriptedModel(GenericFakeChatModel):
    """A chat model that replays a scripted sequence of str/AIMessage responses."""

    profile: Any = {"structured_output": True}
    _msgs: list[Any]
    _i: int

    def __init__(self, responses: Sequence[Any], **kwargs: Any) -> None:
        super().__init__(messages=iter([]), **kwargs)
        self._msgs = list(responses)
        self._i = 0

    def bind_tools(
        self,
        tools: Any,
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable:
        """No-op binding: the scripted replay is the whole contract."""
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if not self._msgs:
            raise RuntimeError("script exhausted")
        item = self._msgs[self._i]
        self._i = (self._i + 1) % len(self._msgs)
        message = AIMessage(content=item) if isinstance(item, str) else item
        return ChatResult(generations=[ChatGeneration(message=message)])

def scripted_executor_model(script: list[dict]) -> ScriptedModel:
    """Convert legacy decide-style executor scripts to Deep-path model responses.

    Each 'call_tool' decision becomes an AIMessage tool call; each
    'complete' / 'need_user' decision becomes a terminal JSON string the
    Deep harness parses into its structured response channel. This lets the
    pre-Stage-1.2 executor tests drive the production Deep loop without
    re-authoring every script.
    """
    responses: list[Any] = []
    for index, item in enumerate(script, 1):
        action = item.get("action")
        if action == "call_tool":
            responses.append(
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": item["tool_name"],
                            "args": item.get("tool_input") or {},
                            "id": f"call-{index}",
                            "type": "tool_call",
                        }
                    ],
                )
            )
        elif action == "complete":
            payload: dict[str, Any] = {
                "status": "succeeded",
                "summary": item.get("summary", ""),
            }
            if item.get("artifact_refs") is not None:
                payload["artifact_refs"] = item["artifact_refs"]
            responses.append(json.dumps(payload, ensure_ascii=False))
        elif action == "need_user":
            responses.append(
                json.dumps(
                    {
                        "status": "needs_user",
                        "summary": item.get("summary", ""),
                        "user_question": item.get("user_question"),
                        "artifact_refs": item.get("artifact_refs", []),
                    },
                    ensure_ascii=False,
                )
            )
        else:
            raise ValueError(f"unsupported executor script action: {action}")
    return ScriptedModel(responses)


class RecordingModel(ScriptedModel):
    """ScriptedModel that records every generation for assertion support."""

    calls: ClassVar[list[list[Any]]] = []

    def __init__(self, responses: list[Any]) -> None:
        # Accept either a response list or the ScriptedModel produced by
        # scripted_executor_model (iterating a model yields (name, value)
        # pairs, not responses).
        if isinstance(responses, ScriptedModel):
            responses = list(responses._msgs)
        super().__init__(responses)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        type(self).calls.append(list(messages))
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


class DeepGateway:
    """Deterministic model boundary exposing a scripted chat model."""

    def __init__(self, model: ScriptedModel) -> None:
        self._model = model
