"""Role-aware registry for safe Agent-selected tool execution."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from backend.app.domain.agent_runtime import AgentRole
from backend.app.services.agent_runtime.schemas import ToolObservation
from backend.app.services.agent_runtime.tool_context import ToolContext


def _sanitize_error_message(text: str, max_length: int = 500) -> str:
    """Strip sensitive substrings (credentials in URLs) and truncate safely.

    Never expose raw tokens, passwords, or user-info-bearing URLs to the agent.
    """
    # Strip userinfo from URLs: https://user:pass@host -> https://[redacted]@host
    stripped = re.sub(r"([a-zA-Z][a-zA-Z0-9+.-]*://)[^@]+@", r"\1[redacted]@", text)
    # Strip common credential forms from provider and adapter exceptions.
    stripped = re.sub(
        r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+",
        "Bearer [redacted]",
        stripped,
    )
    stripped = re.sub(
        r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|secret|authorization)\s*[:=]\s*([^\s,;]+)",
        r"\1=[redacted]",
        stripped,
    )
    return stripped[:max_length]


def _validation_error_summary(exc: ValidationError, max_length: int = 500) -> str:
    """Condense schema validation failures into actionable field guidance.

    pydantic error entries carry ``loc`` (the failing field path), ``type``
    (the violated constraint) and ``msg`` (a human description). Joining them
    lets the Executor fix its call instead of blindly retrying the same
    invalid payload until it stalls. Submitted values (``entry["input"]``) are
    deliberately never echoed: a payload may embed tokens or credentials,
    which must not reach the agent or logs.
    """
    entries: list[str] = []
    for entry in exc.errors():
        loc = ".".join(str(part) for part in entry.get("loc", ()))
        etype = entry.get("type", "validation_error")
        if loc:
            entries.append(f"{loc}: {etype}")
        else:
            # Model-level errors (e.g. wrong top-level type) have no field loc.
            entries.append(str(etype))
    return _sanitize_error_message("invalid input: " + "; ".join(entries), max_length=max_length)

ToolHandler = Callable[[ToolContext, BaseModel], BaseModel | Mapping[str, Any]]


@dataclass(frozen=True)
class ToolDefinition:
    """One explicitly registered handler and its role/schema boundary."""

    name: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    allowed_roles: frozenset[AgentRole]
    handler: ToolHandler
    skill_name: str | None = None
    description: str = ""
    # ``None`` keeps old integrations source-compatible. Production skill
    # adapters should set this explicitly so completion contracts do not infer
    # deliverables from every executable helper.
    is_deliverable: bool | None = None


class ToolRegistry:
    """Execute only registered, role-authorized, schema-valid Agent actions."""

    def __init__(self) -> None:
        self._definitions: dict[str, ToolDefinition] = {}
        self._catalog_cache: dict[
            tuple[AgentRole, frozenset[str] | None], list[dict[str, Any]]
        ] = {}

    def register(self, definition: ToolDefinition) -> None:
        """Register a unique, non-empty tool name before runtime startup."""
        if not definition.name.strip():
            raise ValueError("tool name must not be empty")
        if not definition.allowed_roles:
            raise ValueError("tool must authorize at least one Agent role")
        if definition.name in self._definitions:
            raise ValueError(f"tool already registered: {definition.name}")
        self._definitions[definition.name] = definition
        # A new tool changes every role/skill projection, so cached catalogs
        # are now stale. Registration only happens at startup, so this clear
        # is free in the steady state.
        self._catalog_cache.clear()

    @property
    def definitions(self) -> tuple[ToolDefinition, ...]:
        """Expose immutable registration metadata to the skill registry."""
        return tuple(self._definitions.values())

    def tool_catalog(
        self,
        *,
        role: AgentRole,
        allowed_skills: frozenset[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Describe only the tools an autonomous role may safely request.

        The registry is immutable after startup (``register`` clears this
        cache), so a catalog is a pure projection of ``(role, allowed_skills)``
        and is memoized: the first call per view pays the Pydantic
        ``model_json_schema`` cost; every later turn that requests the same
        view (across steps and runs) reuses it. Callers must not mutate the
        returned list -- it is the cached object shared across PEV turns.
        """
        cache_key: tuple[AgentRole, frozenset[str] | None] = (role, allowed_skills)
        cached = self._catalog_cache.get(cache_key)
        if cached is not None:
            return cached
        catalog: list[dict[str, Any]] = []
        for definition in sorted(self._definitions.values(), key=lambda item: item.name):
            if role not in definition.allowed_roles:
                continue
            # When a skill scope is in effect, exclude tools with no skill
            # affiliation: they cannot satisfy ``invoke``'s scope check anyway
            # (None is never in ``allowed_skills``), so advertising them only
            # tempts the Executor to call a tool that would be rejected as
            # ``tool_skill_forbidden``. ``allowed_skills is None`` (planner /
            # unscoped verifier) keeps seeing every tool.
            if allowed_skills is not None and (
                definition.skill_name is None
                or definition.skill_name not in allowed_skills
            ):
                continue
            catalog.append(
                {
                    "name": definition.name,
                    "skill_name": definition.skill_name,
                    "description": definition.description,
                    "input_schema": definition.input_model.model_json_schema(),
                    "output_schema": definition.output_model.model_json_schema(),
                }
            )
        self._catalog_cache[cache_key] = catalog
        return catalog

    def invoke(
        self,
        *,
        role: AgentRole,
        name: str,
        context: ToolContext,
        payload: Mapping[str, Any],
        allowed_skills: frozenset[str] | None = None,
    ) -> ToolObservation:
        """Return an observation instead of leaking handler exceptions to Agents."""
        definition = self._definitions.get(name)
        if definition is None:
            return ToolObservation(
                tool_name=name,
                status="failed",
                error_code="unknown_tool",
            )
        if role not in definition.allowed_roles:
            return ToolObservation(
                tool_name=name,
                status="failed",
                error_code="tool_role_forbidden",
            )
        if allowed_skills is not None and definition.skill_name not in allowed_skills:
            return ToolObservation(
                tool_name=name,
                status="failed",
                error_code="tool_skill_forbidden",
            )
        try:
            validated_input = definition.input_model.model_validate(payload)
        except ValidationError as exc:
            return ToolObservation(
                tool_name=name,
                status="failed",
                error_code="invalid_tool_input",
                error_message=_validation_error_summary(exc),
            )
        try:
            result = definition.handler(context, validated_input)
            validated_output = definition.output_model.model_validate(result)
        except ValidationError:
            return ToolObservation(
                tool_name=name,
                status="failed",
                error_code="invalid_tool_output",
            )
        except Exception as exc:  # noqa: BLE001 - external tools need a stable observation.
            code = getattr(exc, "code", None) or "tool_execution_failed"
            message = _sanitize_error_message(str(exc))
            return ToolObservation(
                tool_name=name,
                status="failed",
                error_code=code,
                error_message=message,
            )
        return ToolObservation(
            tool_name=name,
            status="succeeded",
            output=validated_output.model_dump(mode="json"),
        )
