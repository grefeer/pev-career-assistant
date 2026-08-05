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

    @property
    def last_usage(self) -> dict[str, Any] | None:
        """Return token usage for the most recent decide() call, if available.

        Returns a dict with {"model_name": str, "input_tokens": int, "output_tokens": int}
        when usage is available, or None otherwise.
        """
        ...

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

    def __init__(
        self,
        model: Any,
        *,
        prefer_local_json_validation: bool = False,
        catalog_in_system_prompt: bool = False,
    ) -> None:
        self._model = model
        # Some otherwise compatible providers accept response_format but do
        # not reliably honour it.  They still make a real autonomous decision;
        # this flag changes only the wire protocol used to validate that one
        # decision locally.
        self._prefer_local_json_validation = prefer_local_json_validation
        # When True, the tool catalog (available_tools in state) is moved from
        # the HumanMessage into the SystemMessage. This enables prompt caching
        # for providers that support it, since the catalog is step-constant.
        self._catalog_in_system_prompt = catalog_in_system_prompt
        self._last_usage: dict[str, Any] | None = None

    @property
    def last_usage(self) -> dict[str, Any] | None:
        """Return token usage for the most recent decide() call, if available."""
        return self._last_usage

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
        self._last_usage = None
        system_content = (
            f"Role: {role.value}. {instruction} "
            "Return exactly one decision matching the requested schema. "
            f"{_role_action_contract(role)}"
        )

        if self._catalog_in_system_prompt:
            state_copy = dict(state)
            catalog = state_copy.pop("available_tools", None)
            if catalog is not None:
                system_content += f"\n\nAvailable tools (JSON):\n{json.dumps(catalog, ensure_ascii=False, separators=(',', ':'))}"
            human_state = state_copy
        else:
            human_state = state

        messages = [
            SystemMessage(content=system_content),
            HumanMessage(
                content=json.dumps(
                    human_state, ensure_ascii=False, separators=(",", ":"), default=str
                )
            ),
        ]
        if self._prefer_local_json_validation:
            return self._decide_with_local_json_retry(
                messages=messages,
                role=role,
                response_model=response_model,
            )
        try:
            structured_model = self._model.with_structured_output(
                response_model, include_raw=True
            )
            raw_result = structured_model.invoke(messages)
            parsed = raw_result.get("parsed") if isinstance(raw_result, dict) else None
            if isinstance(raw_result, dict):
                raw_message = raw_result.get("raw")
                if raw_message is not None:
                    self._extract_usage(raw_message)
        except Exception as exc:  # noqa: BLE001 - provider exceptions are untrusted.
            if _response_format_unavailable(exc):
                return self._decide_with_local_json_validation(
                    messages=messages,
                    role=role,
                    response_model=response_model,
                )
            raise AgentModelGatewayError("model_request_failed") from exc
        try:
            return response_model.model_validate(parsed)
        except Exception as exc:  # noqa: BLE001 - invalid JSON/model output is recoverable.
            # Some OpenAI-compatible providers accept the structured request
            # but occasionally return an object that violates it. One bounded
            # ordinary-JSON retry is a transport compatibility recovery, not a
            # business retry or an Agent-selected action.
            try:
                return self._decide_with_local_json_validation(
                    messages=messages,
                    role=role,
                    response_model=response_model,
                )
            except AgentModelGatewayError as fallback_error:
                if fallback_error.code == "model_request_failed":
                    raise fallback_error from exc
                # A few OpenAI-compatible endpoints occasionally emit one
                # malformed ordinary-JSON completion after accepting the
                # schema request. Retry the transport protocol once more;
                # this remains a bounded schema-validation recovery, never an
                # Agent decision or a tool-selection retry.
                try:
                    return self._decide_with_local_json_validation(
                        messages=messages,
                        role=role,
                        response_model=response_model,
                    )
                except AgentModelGatewayError as retry_error:
                    if retry_error.code == "model_request_failed":
                        raise retry_error from exc
                raise AgentModelGatewayError("invalid_model_response") from exc

    def _decide_with_local_json_retry(
        self,
        *,
        messages: list[SystemMessage | HumanMessage],
        role: AgentRole,
        response_model: type[ResponseT],
    ) -> ResponseT:
        """Allow two malformed-completion retries for a JSON-only provider.

        This matches the ordinary-JSON recovery budget of the structured-output
        path (three total attempts); each attempt is a bounded transport
        recovery, never an Agent decision or a tool-selection retry.  A
        deterministic provider (temperature 0) would reproduce the same broken
        completion on identical messages, so each retry appends a corrective
        schema-format hint that changes the input.
        """
        last_error: AgentModelGatewayError | None = None
        for attempt in range(3):
            try:
                return self._decide_with_local_json_validation(
                    messages=messages,
                    role=role,
                    response_model=response_model,
                )
            except AgentModelGatewayError as error:
                if error.code == "model_request_failed":
                    raise
                last_error = error
                if attempt < 2:
                    messages = [
                        *messages,
                        SystemMessage(
                            content=(
                                "Your previous response was not one valid JSON "
                                "object matching the requested schema. Return "
                                "ONLY a single JSON object: no prose before or "
                                "after it, no extra fields, no trailing "
                                "punctuation."
                            )
                        ),
                    ]
        raise AgentModelGatewayError("invalid_model_response") from last_error

    def _decide_with_local_json_validation(
        self,
        *,
        messages: list[SystemMessage | HumanMessage],
        role: AgentRole,
        response_model: type[ResponseT],
    ) -> ResponseT:
        """Use ordinary JSON output only when a provider rejects response_format."""
        fallback_messages = [
            *messages,
            SystemMessage(
                content=(
                    "Return only one JSON object matching this schema exactly: "
                    f"{json.dumps(response_model.model_json_schema(), ensure_ascii=False)} "
                    f"{_role_action_contract(role)}"
                )
            ),
        ]
        try:
            raw_result = self._model.invoke(fallback_messages)
        except Exception as exc:  # noqa: BLE001 - provider boundary.
            raise AgentModelGatewayError("model_request_failed") from exc
        self._extract_usage(raw_result)
        content = getattr(raw_result, "content", raw_result)
        if not isinstance(content, str):
            raise AgentModelGatewayError("invalid_model_response")
        try:
            return response_model.model_validate(json.loads(_strip_json_fence(content)))
        except Exception as exc:  # noqa: BLE001 - untrusted model content.
            raise AgentModelGatewayError("invalid_model_response") from exc

    def _extract_usage(self, raw_message: Any) -> None:
        """Extract token usage from a raw AIMessage and store in _last_usage.

        Sets self._last_usage to a dict with usage data when available,
        or None if no usage metadata is present. This follows the
        Protocol contract of returning dict[str, Any] | None.
        """
        usage_metadata = getattr(raw_message, "usage_metadata", None)
        response_metadata = getattr(raw_message, "response_metadata", {})
        token_usage = (
            response_metadata.get("token_usage")
            if isinstance(response_metadata, dict)
            else None
        )

        if usage_metadata is not None and isinstance(usage_metadata, dict):
            self._last_usage = {
                "model_name": getattr(self._model, "model", None),
                "input_tokens": usage_metadata.get("input_tokens"),
                "output_tokens": usage_metadata.get("output_tokens"),
            }
        elif token_usage is not None and isinstance(token_usage, dict):
            self._last_usage = {
                "model_name": getattr(self._model, "model", None),
                "input_tokens": token_usage.get("prompt_tokens"),
                "output_tokens": token_usage.get("completion_tokens"),
            }
        else:
            self._last_usage = None


def _response_format_unavailable(error: Exception) -> bool:
    message = str(error).lower()
    return "response_format" in message and (
        "unavailable" in message or "not supported" in message
    )


def _strip_json_fence(content: str) -> str:
    """Accept a provider's harmless Markdown JSON fence, never surrounding prose."""
    cleaned = content.strip()
    if cleaned.startswith("```json") and cleaned.endswith("```"):
        return cleaned[7:-3].strip()
    if cleaned.startswith("```") and cleaned.endswith("```"):
        return cleaned[3:-3].strip()
    return cleaned


def _role_action_contract(role: AgentRole) -> str:
    """Spell out validator-only action constraints absent from JSON Schema."""
    if role is AgentRole.planner:
        return (
            "The action must be exactly one of: call_tool, plan, need_user. "
            "call_tool needs tool_name; plan needs complexity, success_criteria and steps; "
            "need_user needs user_question."
        )
    if role is AgentRole.executor:
        return (
            "The action must be exactly one of: call_tool, complete, need_user. "
            "call_tool needs tool_name; complete needs summary; need_user needs user_question."
        )
    return (
        "The action must be exactly one of: call_tool, decide. "
        "call_tool needs tool_name; decide needs verification_decision, which must be "
        "PASS, RETRY_EXECUTOR, REPLAN, NEED_USER or FAIL."
    )


def build_agent_model_gateway(settings: Settings) -> LangChainModelGateway:
    """Build the live OpenAI-compatible decision provider for all three roles."""
    from backend.app.services.agent_runtime.provider_config import get_api_key, get_base_url

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
    prefers_local_json = "deepseek" in base_url.lower() and settings.agent_harness_model.startswith(
        "deepseek-v4"
    )
    return LangChainModelGateway(
        ChatOpenAI(**kwargs),
        prefer_local_json_validation=prefers_local_json,
        catalog_in_system_prompt=settings.agent_harness_catalog_in_system_prompt,
    )
