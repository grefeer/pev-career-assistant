"""Replaceable structured-model boundary used by autonomous PEV Agents."""

from __future__ import annotations

import json
import logging
import hashlib
from typing import Any, Protocol, TypeVar

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from backend.app.config import Settings
from backend.app.domain.agent_runtime import AgentRole
from backend.app.services.agent_runtime.prompt_rules import json_repair_rules

logger = logging.getLogger(__name__)

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
        structured_method: str = "json_schema",
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
        # Structured-output wire protocol: "json_schema" (langchain default,
        # OpenAI Structured Outputs) or "json_mode" (response_format
        # {"type":"json_object"}).  The default equals langchain's default so
        # the non-deepSeek path is byte-identical to omitting the arg.
        self._structured_method = structured_method
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

        if self._structured_method == "json_mode":
            # DeepSeek's response_format={"type":"json_object"} (json_mode)
            # requires the word "json" in the prompt; this minimal hint
            # satisfies that protocol requirement without changing the
            # action contract.
            system_content += " Return one JSON object matching the requested schema."

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
        raw_content: str | None = None
        try:
            structured_model = self._model.with_structured_output(
                response_model, include_raw=True, method=self._structured_method
            )
            raw_result = structured_model.invoke(messages)
            parsed = raw_result.get("parsed") if isinstance(raw_result, dict) else None
            if isinstance(raw_result, dict):
                raw_message = raw_result.get("raw")
                if raw_message is not None:
                    self._extract_usage(raw_message)
                    candidate = getattr(raw_message, "content", None)
                    if isinstance(candidate, str):
                        raw_content = candidate
        except Exception as exc:  # noqa: BLE001 - provider exceptions are untrusted.
            if _response_format_unavailable(exc):
                return self._decide_with_local_json_validation(
                    messages=messages,
                    role=role,
                    response_model=response_model,
                )
            raise AgentModelGatewayError("model_request_failed") from exc
        try:
            return response_model.model_validate(
                _coerce_response_fields(parsed, response_model)
            )
        except Exception as exc:  # noqa: BLE001 - invalid JSON/model output is recoverable.
            # The structured-output path returned None or a schema-violating
            # object. Before a second model call, try to recover the raw
            # completion locally: strip fences, unwrap a single-key wrapper,
            # infer a missing action, rename wrong field names. This is a
            # deterministic local repair, never an Agent decision or a
            # tool selection. If the raw completion is itself unrecoverable
            # (e.g. steps emitted as strings), fall through to a bounded model
            # retry with a corrective schema-format hint per attempt.
            if raw_content is not None:
                try:
                    return _parse_and_validate(raw_content, response_model)
                except Exception:  # noqa: BLE001 - fall through to model retry.
                    logger.warning(
                        "gateway stage1 raw unrecoverable; role=%s model=%s "
                        "content_sha256=%s chars=%d",
                        role.value,
                        response_model.__name__,
                        _content_fingerprint(raw_content),
                        len(raw_content),
                    )
            try:
                return self._decide_with_local_json_retry(
                    messages=messages,
                    role=role,
                    response_model=response_model,
                )
            except AgentModelGatewayError as retry_error:
                if retry_error.code == "model_request_failed":
                    raise retry_error from exc
                raise

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
                                "punctuation. "
                                + json_repair_rules(role.value)
                            )
                        ),
                    ]
        logger.warning(
            "gateway exhausted local-json retry; role=%s model=%s attempts=3",
            role.value,
            response_model.__name__,
        )
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
                    f"{_role_action_contract(role)} "
                    f"{json_repair_rules(role.value)}"
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
            return _parse_and_validate(content, response_model)
        except Exception as exc:  # noqa: BLE001 - untrusted model content.
            logger.warning(
                "gateway local-json parse failed; role=%s model=%s "
                "content_sha256=%s chars=%d",
                role.value,
                response_model.__name__,
                _content_fingerprint(content),
                len(content),
            )
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


def _content_fingerprint(content: str) -> str:
    """Return an audit-safe fingerprint without retaining model/user text."""
    return hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()[:16]


def _strip_json_fence(content: str) -> str:
    """Accept a provider's harmless Markdown JSON fence or leading prose.

    Some providers prefix the (possibly fenced) JSON object with explanatory
    prose. When the content no longer starts with a fence, truncate at the
    first object opener ``{`` so ``json.loads`` sees a clean leading token,
    and drop a trailing fence that the prose pushed off the start.
    """
    cleaned = content.strip()
    if cleaned.startswith("```json") and cleaned.endswith("```"):
        return cleaned[7:-3].strip()
    if cleaned.startswith("```") and cleaned.endswith("```"):
        return cleaned[3:-3].strip()
    brace = cleaned.find("{")
    if brace > 0:
        cleaned = cleaned[brace:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3].strip()
    return cleaned


# Known provider-emitted wrong field names that should map to a schema field
# before validation. Applied only when the target field exists in the response
# schema and is not already present, so schemas without the target field are
# left untouched. ``input`` is the OpenAI tool-call convention (schema field
# ``tool_input``); ``decision`` is a provider synonym for the ``action`` field
# emitted by some json_mode draws.
_WRONG_RESPONSE_FIELD_NAMES = {"input": "tool_input", "decision": "action"}


def _coerce_response_fields(
    data: object, response_model: type[BaseModel]
) -> object:
    """Tolerantly repair provider-emitted drift before schema validation.

    Four safe, schema-aware repairs, each a no-op when the precondition does
    not hold:

    1. Unwrap a single-key wrapper the model emitted instead of the flat
       decision (e.g. ``{"plan": {...}}``), when the wrapper key is not itself
       a schema field and its value is a dict.
    2. Infer a missing ``action="plan"`` when the model returned a plan
       payload (``complexity``/``success_criteria``/``steps``) without the
       enclosing action. Gated on plan-only fields, so Executor/Verifier
       schemas (which have no ``plan`` action) are unaffected.
    3. Rename provider-emitted wrong field names (``input`` -> ``tool_input``,
       ``decision`` -> ``action``) only when the target is a schema field and
       not already present.
    4. Infer a missing ``step_id`` on each plan step (the model sometimes
       emits ``objective``/``allowed_skills`` without the required id) when the
       response model declares a ``steps`` field. Non-plan schemas are
       unaffected.

    Returns non-dict payloads unchanged, so this is a safe no-op for every
    response model that lacks the relevant fields.
    """
    if not isinstance(data, dict):
        return data
    schema_fields = _response_schema_fields(response_model)
    if len(data) == 1:
        only_key, only_value = next(iter(data.items()))
        if only_key not in schema_fields and isinstance(only_value, dict):
            data = only_value
    if (
        "action" in schema_fields
        and "action" not in data
        and ("steps" in data or "complexity" in data or "success_criteria" in data)
    ):
        data["action"] = "plan"
    for wrong_name, correct_name in _WRONG_RESPONSE_FIELD_NAMES.items():
        if (
            wrong_name in data
            and correct_name in schema_fields
            and correct_name not in data
        ):
            data[correct_name] = data.pop(wrong_name)
    if "steps" in schema_fields:
        steps = data.get("steps")
        if isinstance(steps, list):
            for index, step in enumerate(steps):
                if isinstance(step, dict) and not step.get("step_id"):
                    step["step_id"] = f"step-{index + 1}"
                if not isinstance(step, dict):
                    continue
                # A model often declares an artifact input's source step but
                # omits the duplicated DAG edge. This is a deterministic,
                # lossless normalization: only an explicit ``from_step`` can
                # be promoted into ``depends_on``; unknown sources still fail
                # the PlanStep/ExecutionPlan validators.
                dependencies = step.setdefault("depends_on", [])
                if not isinstance(dependencies, list):
                    continue
                inputs = step.get("inputs", [])
                if isinstance(inputs, list):
                    for input_ref in inputs:
                        if not isinstance(input_ref, dict):
                            continue
                        source = input_ref.get("from_step")
                        if (
                            input_ref.get("kind") == "artifact"
                            and isinstance(source, str)
                            and source.strip()
                            and source not in dependencies
                        ):
                            dependencies.append(source)
    return data


def _response_schema_fields(response_model: type[BaseModel]) -> dict[str, object]:
    """Return the union of top-level fields for flat and discriminated models.

    The gateway's tolerant provider repair predates the discriminated RootModel
    decisions.  A RootModel intentionally exposes ``oneOf`` instead of a
    top-level ``properties`` object, so repair must inspect each branch without
    weakening the actual Pydantic validation that follows.
    """
    schema = response_model.model_json_schema()
    properties = dict(schema.get("properties", {}))
    definitions = schema.get("$defs", {})
    for branch in schema.get("oneOf", []):
        if not isinstance(branch, dict):
            continue
        resolved = branch
        reference = branch.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/$defs/"):
            candidate = definitions.get(reference.rsplit("/", 1)[-1])
            if isinstance(candidate, dict):
                resolved = candidate
        branch_properties = resolved.get("properties", {})
        if isinstance(branch_properties, dict):
            properties.update(branch_properties)
    return properties


def _parse_and_validate(
    content: str, response_model: type[ResponseT]
) -> ResponseT:
    """Strip fences -> JSON parse -> coerce drift -> validate against the schema.

    No exception handling: callers wrap this in a try/except so they can map
    failures to the appropriate recoverable gateway error.
    """
    data = json.loads(_strip_json_fence(content))
    data = _coerce_response_fields(data, response_model)
    return response_model.model_validate(data)


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


def build_agent_chat_model(
    settings: Settings, *, max_tokens: int | None = None
) -> tuple[ChatOpenAI, str]:
    """OpenAI-compatible chat model wired for the live PEV provider.

    Returns ``(model, structured_method)``: deepseek-v4 (the current live
    provider) gets thinking disabled plus ``json_mode``; any other provider
    keeps the plain ``json_schema`` transport.  ``max_tokens`` is optional:
    the decision gateways rely on the provider default, while long-output
    callers (the JD extractor, C1) cap it explicitly.  Raises
    ``AgentModelGatewayConfigError`` when the API key is missing, so a
    keyless construction never fabricates a model.
    """
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
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    is_deepseek_v4 = "deepseek" in base_url.lower() and settings.agent_harness_model.startswith(
        "deepseek-v4"
    )
    if is_deepseek_v4:
        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
    structured_method = "json_mode" if is_deepseek_v4 else "json_schema"
    return ChatOpenAI(**kwargs), structured_method


def build_agent_model_gateway(settings: Settings) -> LangChainModelGateway:
    """Build the live OpenAI-compatible decision provider for all three roles."""
    model, structured_method = build_agent_chat_model(
        settings, max_tokens=settings.agent_harness_model_max_output_tokens
    )
    return LangChainModelGateway(
        model,
        prefer_local_json_validation=False,
        catalog_in_system_prompt=settings.agent_harness_catalog_in_system_prompt,
        structured_method=structured_method,
    )
