"""Replaceable structured-model boundary used by autonomous PEV Agents."""

from __future__ import annotations

import json
from typing import Any, Protocol, TypeVar

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from backend.app.config import Settings
from backend.app.domain.agent_runtime import AgentRole

ResponseT = TypeVar("ResponseT", bound=BaseModel)


class AgentModelGateway(Protocol):
    """Return one schema-validated decision for the specified autonomous role.

    A concrete gateway can use any model provider.  It may not choose tools on
    behalf of the Agent runtime: its structured decision is validated and then
    the role itself decides whether to execute the requested permitted action.
    """

    def decide(
        self,
        *,
        role: AgentRole,
        instruction: str,
        state: dict[str, object],
        response_model: type[ResponseT],
    ) -> ResponseT: ...


class AgentModelGatewayError(RuntimeError):
    """Stable provider boundary failure that agents/services can safely surface."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class AgentModelGatewayConfigError(RuntimeError):
    """Raised when a configured PEV gateway cannot safely be constructed."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class LangChainModelGateway:
    """Schema-first gateway for any LangChain OpenAI-compatible chat model."""

    def __init__(self, model: Any) -> None:
        self._model = model

    def decide(
        self,
        *,
        role: AgentRole,
        instruction: str,
        state: dict[str, object],
        response_model: type[ResponseT],
    ) -> ResponseT:
        """Ask a provider for exactly one validated role decision.

        The model has no direct tools here.  It only returns an action; the
        corresponding Agent loop validates and performs it through ToolRegistry.
        """
        messages = [
            SystemMessage(content=f"Role: {role.value}. {instruction}"),
            HumanMessage(
                content=json.dumps(
                    state, ensure_ascii=False, separators=(",", ":"), default=str
                )
            ),
        ]
        try:
            structured_model = self._model.with_structured_output(response_model)
            raw_result = structured_model.invoke(messages)
        except Exception as exc:  # noqa: BLE001 - provider exceptions are untrusted.
            raise AgentModelGatewayError("model_request_failed") from exc
        try:
            return response_model.model_validate(raw_result)
        except Exception as exc:  # noqa: BLE001 - invalid JSON/model output is recoverable.
            raise AgentModelGatewayError("invalid_model_response") from exc


def build_agent_model_gateway(settings: Settings) -> LangChainModelGateway:
    """Build the live OpenAI-compatible decision provider for all three roles."""
    from src.utils import get_api_key, get_base_url

    api_key = get_api_key()
    if not api_key:
        raise AgentModelGatewayConfigError("missing_api_key")
    base_url = get_base_url()
    kwargs: dict[str, Any] = {
        "model": settings.agent_harness_model,
        "temperature": 0,
        "request_timeout": 120,
        "max_retries": 2,
        "api_key": api_key,
        "base_url": base_url,
    }
    if "deepseek" in base_url.lower() and settings.agent_harness_model.startswith(
        "deepseek-v4"
    ):
        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
    return LangChainModelGateway(ChatOpenAI(**kwargs))
