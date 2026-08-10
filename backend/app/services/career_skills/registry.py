"""Register reviewed business-Skill tools with the PEV ToolRegistry."""

from __future__ import annotations

from backend.app.domain.agent_runtime import AgentRole
from backend.app.services.agent_runtime.tool_registry import ToolDefinition, ToolRegistry
from backend.app.services.career_skills import (
    career_planning,
    career_sheets,
    classify_url,
    deduplicate_observed,
    job_discovery,
    job_matching,
    resume_tailoring,
    validate_candidates,
    wechat,
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
            description="批量抓取用户给出的有限官方 URL，返回每页可追溯正文或明确失败原因。JS 卡片列表页（渲染后无 JD 正文但有职位卡片链接）会自动展开：列表页本身 + 前 5 个详情页正文一并返回，可直接进入后续提取与匹配。failures 列表中的失败仅针对列出的 URL 本身（如 wechat_ocr_failed / public_fetch_failed / adapter:*），绝不代表同批其他 URL 或同类型的其他微信链接也会失败——同一批中其余 URL 仍应继续逐一尝试，全部尝试后才能下结论。",
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
            description="搜索公开招聘页；仅在用户没有提供候选 URL（或全部候选 URL 均已抓取失败：fetch 错误或 dead_link 死链）、且 smartsheet 无匹配记录时用于发现直接招聘链接；部分候选失败绝不授权搜索。",
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
            description="查询招聘 smartsheet（内推/招聘链接台账）按企业/岗位/地点关键词与近 N 天过滤，返回候选招聘 URL；每条记录带 prior_metadata（公司/投递链接/内推码/更新时间）补足页面缺失字段；主证据源，无匹配记录时才用网络搜索；当 smartsheet 接口不可用或受限（error sheet_rate_limited / sheet_call_failed，如每日访问配额 400007 用尽）时，search-public-job-pages 是授权的备用数据源，应切换到公开搜索。",
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
            name="validate-observed-candidates",
            skill_name="job-discovery",
            input_model=validate_candidates.ValidateObservedCandidatesInput,
            output_model=validate_candidates.ValidateObservedCandidatesOutput,
            allowed_roles=frozenset({AgentRole.executor, AgentRole.verifier}),
            handler=validate_candidates.validate_observed_candidates,
            description="对已观察页面证据做确定性质量校验（陈旧年份/正文过短/非 JD 文本），供 Verifier 判 PASS/REPLAN。",
        )
    )
    registry.register(
        ToolDefinition(
            name="fetch-wechat-article",
            skill_name="job-discovery",
            input_model=wechat.FetchWechatArticleInput,
            output_model=wechat.FetchWechatArticleOutput,
            allowed_roles=frozenset({AgentRole.executor, AgentRole.verifier}),
            handler=wechat.fetch_wechat_article,
            description="OCR 抓取微信公众号图文（含 ReadGZH 镜像）为可提取文本与候选。微信图文正文是图片，普通页面抓取返回空内容——目标为 mp.weixin.qq.com 链接时使用本工具（fetch-public-job-pages 也已自动路由微信链接）；门控关闭时返回 needs_manual_review（reason ocr_disabled）。注意：单个微信链接失败（如镜像返回验证墙/付费墙、文章无正文）只代表该链接本身不可用，不代表其他微信文章链接也会失败——每篇独立尝试，其余链接仍应继续抓取。",
        )
    )
    registry.register(
        ToolDefinition(
            name="deduplicate-observed-jobs",
            skill_name="job-discovery",
            input_model=deduplicate_observed.DeduplicateObservedJobsInput,
            output_model=deduplicate_observed.DeduplicateObservedJobsOutput,
            allowed_roles=frozenset({AgentRole.executor, AgentRole.verifier}),
            handler=deduplicate_observed.deduplicate_observed_jobs,
            description="对已观察页面证据按 canonical 身份（job_id/apply_url/规范化标题）做 run 内确定性去重，返回 kept/removed。",
        )
    )
    registry.register(
        ToolDefinition(
            name="classify-job-url",
            skill_name="job-discovery",
            input_model=classify_url.ClassifyJobUrlInput,
            output_model=classify_url.ClassifyJobUrlOutput,
            allowed_roles=frozenset({AgentRole.executor}),
            handler=classify_url.classify_job_url,
            description="对候选 URL 做低预算站点分类（wechat/adapter/static/spa/blocked，host 信号 + 4KB 探针，不启动浏览器）。",
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
