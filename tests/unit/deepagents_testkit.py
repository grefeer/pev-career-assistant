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

from typing import Any, Sequence

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
