"""Register reviewed business-Skill tools with the PEV ToolRegistry."""

from __future__ import annotations

from backend.app.domain.agent_runtime import AgentRole
from backend.app.services.agent_runtime.tool_registry import ToolDefinition, ToolRegistry
from backend.app.services.career_skills import job_discovery


def build_career_tool_registry() -> ToolRegistry:
    """Build the concrete, role-limited tools available to live PEV runs."""
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="fetch-public-job-page",
            skill_name="job-discovery",
            input_model=job_discovery.FetchPublicJobPageInput,
            output_model=job_discovery.FetchPublicJobPageOutput,
            allowed_roles=frozenset({AgentRole.executor, AgentRole.verifier}),
            handler=job_discovery.fetch_public_job_page,
        )
    )
    return registry
