"""Register reviewed business-Skill tools with the PEV ToolRegistry."""

from __future__ import annotations

from backend.app.domain.agent_runtime import AgentRole
from backend.app.services.agent_runtime.tool_registry import ToolDefinition, ToolRegistry
from backend.app.services.career_skills import (
    career_planning,
    career_sheets,
    job_discovery,
    job_matching,
    resume_tailoring,
)


def build_career_tool_registry() -> ToolRegistry:
    """Build the concrete, role-limited tools available to live PEV runs."""
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="extract-observed-job-details-batch",
            skill_name="job-discovery",
            input_model=job_discovery.ExtractObservedJobDetailsBatchInput,
            output_model=job_discovery.ExtractObservedJobDetailsBatchOutput,
            allowed_roles=frozenset({AgentRole.executor, AgentRole.verifier}),
            handler=job_discovery.extract_observed_job_details_batch,
            description="批量把已观察页面证据规范化为详细 JD；不接受模型生成的正文。",
        )
    )
    registry.register(
        ToolDefinition(
            name="fetch-public-job-pages",
            skill_name="job-discovery",
            input_model=job_discovery.FetchPublicJobPagesInput,
            output_model=job_discovery.FetchPublicJobPagesOutput,
            allowed_roles=frozenset({AgentRole.executor, AgentRole.verifier}),
            handler=job_discovery.fetch_public_job_pages,
            description="批量抓取用户给出的有限官方 URL，返回每页可追溯正文或明确失败原因。",
        )
    )
    registry.register(
        ToolDefinition(
            name="search-public-job-pages",
            skill_name="job-discovery",
            input_model=job_discovery.SearchPublicJobPagesInput,
            output_model=job_discovery.SearchPublicJobPagesOutput,
            allowed_roles=frozenset({AgentRole.executor}),
            handler=job_discovery.search_public_job_pages,
            description="搜索公开招聘页；仅在用户没有提供候选 URL、且 smartsheet 无匹配记录时用于发现直接招聘链接。",
        )
    )
    registry.register(
        ToolDefinition(
            name="query-career-sheet-records",
            skill_name="job-discovery",
            input_model=career_sheets.QueryCareerSheetRecordsInput,
            output_model=career_sheets.QueryCareerSheetRecordsOutput,
            allowed_roles=frozenset({AgentRole.executor}),
            handler=career_sheets.query_career_sheet_records,
            description="查询招聘 smartsheet（内推/招聘链接台账）按企业/岗位/地点关键词与近 N 天过滤，返回候选招聘 URL；主证据源，无匹配时才用网络搜索。",
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
            description="抓取一页公开招聘页面并生成带来源和内容哈希的证据。",
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
            description="把一份已观察页面证据规范化为详细 JD。",
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
            description="对已观察 JD 按已确认能力、地点和可验证待遇/公司属性做透明匹配排序；推荐任务必须调用。",
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
            description="基于已确认简历事实与一个 JD 生成不可虚构、可审阅的简历修改建议。",
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
            description="基于一个 JD 生成带截止日期和复盘点的面试准备计划。",
        )
    )
    return registry
