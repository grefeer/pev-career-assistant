"""Replaceable structured-model boundary used by autonomous PEV Agents."""

from __future__ import annotations

from typing import Protocol, TypeVar

from pydantic import BaseModel

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
