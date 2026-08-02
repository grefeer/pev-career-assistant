"""Register reviewed business-Skill tools with the PEV ToolRegistry."""

from __future__ import annotations

from backend.app.domain.agent_runtime import AgentRole
from backend.app.services.agent_runtime.tool_registry import ToolDefinition, ToolRegistry
from backend.app.services.career_skills import (
    career_planning,
    job_discovery,
    job_matching,
    resume_tailoring,
)


def build_career_tool_registry() -> ToolRegistry:
    """Build the concrete, role-limited tools available to live PEV runs."""
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="search-public-job-pages",
            skill_name="job-discovery",
            input_model=job_discovery.SearchPublicJobPagesInput,
            output_model=job_discovery.SearchPublicJobPagesOutput,
            allowed_roles=frozenset({AgentRole.executor}),
            handler=job_discovery.search_public_job_pages,
        )
    )
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
    registry.register(
        ToolDefinition(
            name="extract-observed-job-details",
            skill_name="job-discovery",
            input_model=job_discovery.ExtractObservedJobDetailsInput,
            output_model=job_discovery.ExtractObservedJobDetailsOutput,
            allowed_roles=frozenset({AgentRole.executor, AgentRole.verifier}),
            handler=job_discovery.extract_observed_job_details,
        )
    )
    registry.register(
        ToolDefinition(
            name="match-observed-jobs",
            skill_name="job-matching",
            input_model=job_matching.MatchObservedJobsInput,
            output_model=job_matching.MatchObservedJobsOutput,
            allowed_roles=frozenset({AgentRole.executor, AgentRole.verifier}),
            handler=job_matching.match_observed_jobs,
        )
    )
    registry.register(
        ToolDefinition(
            name="build-resume-tailoring-brief",
            skill_name="resume-tailoring",
            input_model=resume_tailoring.BuildResumeTailoringBriefInput,
            output_model=resume_tailoring.ResumeTailoringBriefOutput,
            allowed_roles=frozenset({AgentRole.executor, AgentRole.verifier}),
            handler=resume_tailoring.build_resume_tailoring_brief,
        )
    )
    registry.register(
        ToolDefinition(
            name="build-preparation-plan",
            skill_name="career-planning",
            input_model=career_planning.BuildPreparationPlanInput,
            output_model=career_planning.PreparationPlanOutput,
            allowed_roles=frozenset({AgentRole.executor, AgentRole.verifier}),
            handler=career_planning.build_preparation_plan,
        )
    )
    return registry
