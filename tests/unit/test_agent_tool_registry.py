"""Safe, role-aware execution boundary for autonomous Agent tool calls."""

from __future__ import annotations

from pydantic import BaseModel, Field

from backend.app.domain.agent_runtime import AgentRole
from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.agent_runtime.tool_registry import (
    ToolDefinition,
    ToolRegistry,
)


class JobInput(BaseModel):
    job_id: str = Field(min_length=1)


class JobOutput(BaseModel):
    job_id: str
    owner_id: str


def test_executor_observes_real_tool_output_when_role_and_schema_are_allowed() -> None:
    """Executor can use an allowlisted tool and receives validated evidence."""
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="read-job",
            input_model=JobInput,
            output_model=JobOutput,
            allowed_roles=frozenset({AgentRole.executor, AgentRole.verifier}),
            handler=lambda context, payload: {
                "job_id": payload.job_id,
                "owner_id": context.user_id,
            },
        )
    )

    observation = registry.invoke(
        role=AgentRole.executor,
        name="read-job",
        context=ToolContext(user_id="user-a", run_id="run-a"),
        payload={"job_id": "job-1"},
    )

    assert observation.status == "succeeded"
    assert observation.output == {"job_id": "job-1", "owner_id": "user-a"}


def test_planner_cannot_call_executor_only_tool_and_handler_is_not_run() -> None:
    """A role violation must stop a data read before it reaches the handler."""
    invoked: list[str] = []
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="read-job",
            input_model=JobInput,
            output_model=JobOutput,
            allowed_roles=frozenset({AgentRole.executor}),
            handler=lambda context, payload: invoked.append(payload.job_id)
            or {"job_id": payload.job_id, "owner_id": context.user_id},
        )
    )

    observation = registry.invoke(
        role=AgentRole.planner,
        name="read-job",
        context=ToolContext(user_id="user-a", run_id="run-a"),
        payload={"job_id": "job-1"},
    )

    assert observation.status == "failed"
    assert observation.error_code == "tool_role_forbidden"
    assert invoked == []


def test_invalid_input_and_unknown_tool_become_safe_observations() -> None:
    """Agent loops can recover from bad actions without hidden exceptions."""
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="read-job",
            input_model=JobInput,
            output_model=JobOutput,
            allowed_roles=frozenset({AgentRole.executor}),
            handler=lambda context, payload: {
                "job_id": payload.job_id,
                "owner_id": context.user_id,
            },
        )
    )
    context = ToolContext(user_id="user-a", run_id="run-a")

    invalid = registry.invoke(
        role=AgentRole.executor,
        name="read-job",
        context=context,
        payload={"job_id": ""},
    )
    missing = registry.invoke(
        role=AgentRole.executor,
        name="not-registered",
        context=context,
        payload={},
    )

    assert (invalid.status, invalid.error_code) == ("failed", "invalid_tool_input")
    assert (missing.status, missing.error_code) == ("failed", "unknown_tool")
