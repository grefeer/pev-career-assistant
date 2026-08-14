"""Executor Agent behavior: observe tool failure and autonomously choose recovery."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from backend.app.domain.agent_runtime import AgentRole, ComplexityLevel
from backend.app.services.agent_runtime.executor_agent import (
    ExecutorAgent,
    _carried_counter,
    _fetch_route_key,
    _input_hash,
    _load_execution_state,
    _observed_fetch_urls,
    _observed_fetch_route_counts,
    _normalized_step_tool_input,
    _snapshot_execution_state,
)
from backend.app.services.agent_runtime.observation_projection import (
    observation_for_decision,
)
from backend.app.services.agent_runtime.schemas import (
    AgentTaskRequest,
    ExecutionPlan,
    PlanStep,
    ToolObservation,
)
from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.agent_runtime.tool_registry import ToolDefinition, ToolRegistry
from backend.app.services.agent_runtime.tool_budget import ToolCallBudget
from backend.app.services.agent_runtime.turn_budget import AgentTurnBudget


class FetchInput(BaseModel):
    url: str


class FetchOutput(BaseModel):
    source_url: str
    title: str


class EvidenceOutput(BaseModel):
    artifact_id: str
    source_url: str
    title: str
    visible_text: str
    content_hash: str


class BatchEvidenceOutput(BaseModel):
    pages: list[EvidenceOutput]
    failures: list[dict[str, str]] = []


class DetailsOutput(BaseModel):
    title: str


class SheetQueryInput(BaseModel):
    company_keywords: list[str] = []
    role_keywords: list[str] = []
    location_keywords: list[str] = []
    recent_days: int | None = None


class SheetQueryOutput(BaseModel):
    records: list[dict[str, Any]]
    source_url: str
    content_hash: str


class ScriptedGateway:
    """A deterministic model boundary double; executor and registry remain real."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = responses
        self.states: list[dict[str, Any]] = []

    def decide(
        self,
        *,
        role: AgentRole,
        instruction: str,
        state: dict[str, Any],
        response_model: type[BaseModel],
    ) -> BaseModel:
        assert role is AgentRole.executor
        assert instruction
        self.states.append(state)
        return response_model.model_validate(self.responses.pop(0))


def test_executor_completes_recent_company_routing_step_after_sheet_result() -> None:
    captured: list[SheetQueryInput] = []
    registry = ToolRegistry()

    def query_handler(_context, payload):  # noqa: ANN001
        captured.append(payload)
        return {
            "records": [
                {
                    "company_name": "BIGO",
                    "apply_url": "https://jobs.example/bigo",
                    "updated_at": "2026-08-14",
                }
            ],
            "source_url": "https://docs.example/recent-companies",
            "content_hash": "a" * 64,
        }

    registry.register(
        ToolDefinition(
            name="query-career-sheet-records",
            skill_name="job-discovery",
            input_model=SheetQueryInput,
            output_model=SheetQueryOutput,
            allowed_roles=frozenset({AgentRole.executor}),
            handler=query_handler,
        )
    )
    gateway = ScriptedGateway(
        [
            {
                "action": "call_tool",
                "tool_name": "query-career-sheet-records",
                "tool_input": {
                    "role_keywords": ["AIGC 产品经理"],
                    "recent_days": 1,
                },
            }
        ]
    )
    task = AgentTaskRequest(
        goal="先列出最近1天更新的公司清单，再逐公司核实 AIGC 产品经理岗位。",
        allowed_skills=["job-discovery"],
    )
    plan = ExecutionPlan(
        task=task,
        created_by=AgentRole.planner,
        complexity=ComplexityLevel.L3,
        success_criteria=["公司清单和岗位证据"],
        steps=[
            PlanStep(
                step_id="companies",
                objective="查询招聘数据源中最近1天更新的公司记录",
                allowed_skills=["job-discovery"],
                outputs=[
                    {
                        "name": "recent_company_records",
                        "artifact_type": "job_search_results",
                    }
                ],
            ),
            PlanStep(
                step_id="jobs",
                objective="逐公司抓取并提取岗位详情",
                allowed_skills=["job-discovery"],
                depends_on=["companies"],
            ),
        ],
    )

    result = ExecutorAgent(gateway=gateway, tools=registry).run(
        task=task,
        plan=plan,
        step=plan.steps[0],
        context=ToolContext(user_id="user-a", run_id="run-a"),
    )

    assert result.status == "succeeded"
    assert len(gateway.states) == 1
    assert captured[0].recent_days == 1
    assert captured[0].role_keywords == []
    assert [observation.tool_name for observation in result.observations] == [
        "query-career-sheet-records"
    ]


def test_executor_normalizes_tailoring_target_to_goal_constrained_source() -> None:
    """A model-selected unrelated artifact cannot consume the tailoring call."""
    task = AgentTaskRequest(
        goal="在猎聘网找北京的 AIGC 产品经理（应届生）岗位，并定制简历。",
        allowed_skills=["resume-tailoring"],
    )
    step = PlanStep(
        step_id="tailor",
        objective="针对匹配岗位生成简历建议",
        allowed_skills=["resume-tailoring"],
    )
    plan = ExecutionPlan(
        task=task,
        created_by=AgentRole.planner,
        complexity=ComplexityLevel.L2,
        success_criteria=["简历建议"],
        steps=[step],
    )
    context = ToolContext(
        user_id="user-a",
        run_id="run-a",
        metadata={
            "structured_job_candidates": [
                {
                    "candidate_id": "structured-wrong:candidate:0",
                    "artifact_id": "structured-wrong",
                    "source_artifact_id": "page-wrong",
                    "source_url": "https://agirobot.jobs.feishu.cn/s/robot",
                    "title": "机器人产品实习生",
                    "locations": ["上海"],
                    "recruitment_types": ["internship"],
                    "responsibilities": "协助机器人产品设计。",
                    "requirements": "在校生。",
                },
                {
                    "candidate_id": "structured-valid:candidate:0",
                    "artifact_id": "structured-valid",
                    "source_artifact_id": "page-valid",
                    "source_url": "https://cn.linkedin.com/jobs/view/ai-pm-123",
                    "page_source_url": "https://cn.linkedin.com/jobs/view/ai-pm-123",
                    "title": "AI产品经理实习生",
                    "locations": ["北京市"],
                    "recruitment_types": ["internship"],
                    "responsibilities": "该职位来源于猎聘，参与大模型和 Agent 产品设计。",
                    "requirements": "在校本科生，熟悉 Prompt 和 RAG。",
                    "full_text": (
                        "AI产品经理实习生 北京市 该职位来源于猎聘 "
                        "参与大模型和 Agent 产品设计，在校本科生，熟悉 Prompt 和 RAG。"
                    ),
                },
            ]
        },
    )

    normalized = _normalized_step_tool_input(
        plan,
        step,
        "build-resume-tailoring-brief",
        {
            "target_artifact_id": "structured-wrong:candidate:0",
            "target_keywords": ["AIGC"],
        },
        task=task,
        context=context,
    )

    assert normalized["target_artifact_id"] == "structured-valid:candidate:0"
    assert normalized["target_keywords"] == ["RAG", "Prompt", "Agent", "大模型", "AI"]


def test_executor_disambiguates_shared_artifact_with_requested_keywords() -> None:
    """A multi-role page artifact must resolve to its relevant candidate, not index 0."""
    task = AgentTaskRequest(
        goal="基于上一环节找到的岗位和我的简历，为最匹配的岗位生成修改建议。",
        allowed_skills=["resume-tailoring"],
    )
    step = PlanStep(
        step_id="tailor",
        objective="针对匹配岗位生成简历建议",
        allowed_skills=["resume-tailoring"],
    )
    plan = ExecutionPlan(
        task=task,
        created_by=AgentRole.planner,
        complexity=ComplexityLevel.L2,
        success_criteria=["简历建议"],
        steps=[step],
    )
    source_url = "https://www.baiontcapital.com/careers.html"
    context = ToolContext(
        user_id="user-a",
        run_id="run-a",
        metadata={
            "structured_job_candidates": [
                {
                    "candidate_id": "baiont:candidate:0",
                    "artifact_id": "baiont",
                    "source_url": source_url,
                    "title": "量化策略研究员",
                    "responsibilities": "开发量化策略",
                    "requirements": "熟悉 Python",
                },
                {
                    "candidate_id": "baiont:candidate:1",
                    "artifact_id": "baiont",
                    "source_url": source_url,
                    "title": "Agent 后端工程师",
                    "responsibilities": "设计 AI Agent 与 RAG 工作流",
                    "requirements": "熟悉 Tool Calling 和 Agent Loop",
                },
            ]
        },
    )

    normalized = _normalized_step_tool_input(
        plan,
        step,
        "build-resume-tailoring-brief",
        {"target_artifact_id": "baiont", "target_keywords": ["Agent", "RAG"]},
        task=task,
        context=context,
    )

    assert normalized["target_artifact_id"] == "baiont:candidate:1"
    assert normalized["target_keywords"] == ["Agent", "RAG", "AI"]


def test_observed_fetch_urls_only_tracks_successful_fetch_evidence() -> None:
    observations = [
        ToolObservation(
            tool_name="fetch-public-job-pages",
            status="succeeded",
            output={
                "pages": [
                    {
                        "source_url": " https://jobs.example/1 ",
                        "effective_url": "https://jobs.example/1?final=1",
                    }
                ]
            },
        ),
        ToolObservation(
            tool_name="fetch-public-job-pages",
            status="failed",
            error_code="anti_bot_challenge",
            output={"pages": [{"source_url": "https://jobs.example/2"}]},
        ),
        ToolObservation(
            tool_name="search-public-job-pages",
            status="succeeded",
            output={"pages": [{"source_url": "https://jobs.example/3"}]},
        ),
    ]

    assert _observed_fetch_urls(observations) == {
        "https://jobs.example/1",
        "https://jobs.example/1?final=1",
    }


def test_fetch_route_identity_ignores_query_churn_but_keeps_path() -> None:
    assert _fetch_route_key("HTTPS://Jobs.Example/list?page=2") == (
        "https://jobs.example/list"
    )
    assert _fetch_route_key("https://jobs.example/detail/1") != _fetch_route_key(
        "https://jobs.example/detail/2"
    )


def test_fetch_route_counts_ignore_failed_observations() -> None:
    observations = [
        ToolObservation(
            tool_name="fetch-public-job-pages",
            status="succeeded",
            output={"pages": [{"source_url": "https://jobs.example/list?page=1"}]},
        ),
        ToolObservation(
            tool_name="fetch-public-job-pages",
            status="failed",
            error_code="anti_bot_challenge",
            output={"pages": [{"source_url": "https://jobs.example/list?page=2"}]},
        ),
    ]
    assert _observed_fetch_route_counts(observations) == {
        "https://jobs.example/list": 1
    }


def test_executor_observes_failure_and_uses_a_second_allowed_tool() -> None:
    """The recovery choice comes from Executor's next turn, not Harness routing."""
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="primary-fetch",
            skill_name="job-discovery",
            input_model=FetchInput,
            output_model=FetchOutput,
            allowed_roles=frozenset({AgentRole.executor}),
            handler=lambda _context, _payload: (_ for _ in ()).throw(RuntimeError("down")),
        )
    )
    registry.register(
        ToolDefinition(
            name="fallback-fetch",
            skill_name="job-discovery",
            input_model=FetchInput,
            output_model=FetchOutput,
            allowed_roles=frozenset({AgentRole.executor}),
            handler=lambda _context, payload: {
                "source_url": payload.url,
                "title": "AI 应用开发工程师",
            },
        )
    )
    gateway = ScriptedGateway(
        [
            {
                "action": "call_tool",
                "tool_name": "primary-fetch",
                "tool_input": {"url": "https://jobs.example/1"},
            },
            {
                "action": "call_tool",
                "tool_name": "fallback-fetch",
                "tool_input": {"url": "https://jobs.example/1"},
            },
            {
                "action": "complete",
                "summary": "已提取公开岗位 JD",
                "artifact_refs": [{"uri": "artifact://job/1"}],
            },
        ]
    )
    task = AgentTaskRequest(goal="找岗位", allowed_skills=["job-discovery"])
    plan = ExecutionPlan(
        task=task,
        created_by=AgentRole.planner,
        complexity=ComplexityLevel.L2,
        success_criteria=["获取岗位 JD"],
        steps=[
            PlanStep(
                step_id="discover",
                objective="提取公开 JD",
                allowed_skills=["job-discovery"],
            )
        ],
    )

    result = ExecutorAgent(gateway=gateway, tools=registry).run(
        task=task,
        plan=plan,
        step=plan.steps[0],
        context=ToolContext(user_id="user-a", run_id="run-a"),
    )

    assert result.status == "succeeded"
    assert [item.error_code for item in result.observations] == [
        "tool_execution_failed",
        None,
    ]
    assert result.artifact_refs == [{"uri": "artifact://job/1"}]
    assert gateway.states[1]["observations"][0]["error_code"] == "tool_execution_failed"
    assert [tool["name"] for tool in gateway.states[0]["available_tools"]] == [
        "fallback-fetch",
        "primary-fetch",
    ]
    assert all(tool["skill_name"] == "job-discovery" for tool in gateway.states[0]["available_tools"])


def test_executor_makes_a_fresh_public_page_observation_available_to_its_next_tool_call() -> None:
    """The extract tool must receive real fetched evidence, not a model-repeated page body."""
    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="fetch-page", skill_name="job-discovery", input_model=FetchInput,
        output_model=EvidenceOutput, allowed_roles=frozenset({AgentRole.executor}),
        handler=lambda _context, payload: {
            "artifact_id": "observed:a", "source_url": payload.url, "title": "AI Agent 开发工程师",
            "visible_text": "岗位职责：负责 Agent 开发。", "content_hash": "a" * 64,
        },
    ))
    registry.register(ToolDefinition(
        name="extract-page", skill_name="job-discovery", input_model=FetchInput,
        output_model=DetailsOutput, allowed_roles=frozenset({AgentRole.executor}),
        handler=lambda context, _payload: {
            "title": context.metadata["observed_public_evidence"][0]["title"]
        },
    ))
    gateway = ScriptedGateway([
        {"action": "call_tool", "tool_name": "fetch-page", "tool_input": {"url": "https://jobs.example/1"}},
        {"action": "call_tool", "tool_name": "extract-page", "tool_input": {"url": "https://jobs.example/1"}},
        {"action": "complete", "summary": "已提取完整 JD"},
    ])
    task = AgentTaskRequest(goal="提取 JD", allowed_skills=["job-discovery"])
    plan = ExecutionPlan(
        task=task, created_by=AgentRole.planner, complexity=ComplexityLevel.L2,
        success_criteria=["完整 JD"],
        steps=[PlanStep(step_id="discover", objective="抓取并提取", allowed_skills=["job-discovery"])],
    )

    result = ExecutorAgent(gateway=gateway, tools=registry).run(
        task=task, plan=plan, step=plan.steps[0],
        context=ToolContext(user_id="user-a", run_id="run-a"),
    )

    assert result.status == "succeeded"
    assert result.observations[1].output == {"title": "AI Agent 开发工程师"}


def test_executor_exposes_every_page_from_a_batch_observation_to_the_next_tool() -> None:
    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="fetch-pages", skill_name="job-discovery", input_model=FetchInput,
        output_model=BatchEvidenceOutput, allowed_roles=frozenset({AgentRole.executor}),
        handler=lambda _context, _payload: {"pages": [
            {
                "artifact_id": "observed:a", "source_url": "https://jobs.example/a",
                "title": "岗位 A", "visible_text": "x" * 1_201, "content_hash": "a" * 64,
            },
            {
                "artifact_id": "observed:b", "source_url": "https://jobs.example/b",
                "title": "岗位 B", "visible_text": "JD B", "content_hash": "b" * 64,
            },
            {
                "artifact_id": "observed:a", "source_url": "https://jobs.example/a",
                "title": "岗位 A", "visible_text": "JD A", "content_hash": "a" * 64,
            },
        ]},
    ))
    registry.register(ToolDefinition(
        name="inspect-pages", skill_name="job-discovery", input_model=FetchInput,
        output_model=DetailsOutput, allowed_roles=frozenset({AgentRole.executor}),
        handler=lambda context, _payload: {
            "title": ",".join(item["title"] for item in context.metadata["observed_public_evidence"])
        },
    ))
    gateway = ScriptedGateway([
        {"action": "call_tool", "tool_name": "fetch-pages", "tool_input": {"url": "unused"}},
        {"action": "call_tool", "tool_name": "inspect-pages", "tool_input": {"url": "unused"}},
        {"action": "complete", "summary": "已检查批量 JD"},
    ])
    task = AgentTaskRequest(goal="提取 JD", allowed_skills=["job-discovery"])
    plan = ExecutionPlan(
        task=task, created_by=AgentRole.planner, complexity=ComplexityLevel.L2,
        success_criteria=["完整 JD"],
        steps=[PlanStep(step_id="discover", objective="批量抓取", allowed_skills=["job-discovery"])],
    )

    result = ExecutorAgent(gateway=gateway, tools=registry).run(
        task=task, plan=plan, step=plan.steps[0],
        context=ToolContext(user_id="user-a", run_id="run-a"),
    )

    assert result.observations[1].output == {"title": "岗位 A,岗位 B"}
    assert len(gateway.states[1]["observations"][0]["output"]["pages"][0]["visible_text"]) == 1_200


def test_executor_projects_batch_details_to_identifiers_and_titles_only() -> None:
    projected = observation_for_decision(ToolObservation(
        tool_name="extract-observed-job-details-batch", status="succeeded",
        output={"details": [{
            "source_artifact_id": "observed:a", "source_url": "https://jobs.example/a",
            "content_hash": "a" * 64,
            "candidates": [{"title": "岗位 A", "responsibilities": "x" * 5_000}],
        }]},
    ))

    assert projected["output"]["details"] == [{
        "source_artifact_id": "observed:a", "source_url": "https://jobs.example/a",
        "content_hash": "a" * 64, "candidate_titles": ["岗位 A"],
    }]


def test_executor_keeps_agent_in_control_after_blocking_redundant_search() -> None:
    gateway = ScriptedGateway([
        {"action": "call_tool", "tool_name": "search-public-job-pages", "tool_input": {"url": "unused"}},
        {"action": "complete", "summary": "改用已提供的候选页面"},
    ])
    task = AgentTaskRequest(
        goal="处理候选 JD", allowed_skills=["job-discovery"],
        context={"candidate_urls": ["https://jobs.example/agent"]},
    )
    plan = ExecutionPlan(
        task=task, created_by=AgentRole.planner, complexity=ComplexityLevel.L2,
        success_criteria=["处理候选 JD"],
        steps=[PlanStep(step_id="discover", objective="处理候选", allowed_skills=["job-discovery"])],
    )

    result = ExecutorAgent(gateway=gateway, tools=ToolRegistry()).run(
        task=task, plan=plan, step=plan.steps[0],
        context=ToolContext(user_id="user-a", run_id="run-a"),
    )

    assert result.status == "succeeded"
    assert result.observations[0].error_code == "candidate_urls_already_supplied"
    assert gateway.states[1]["observations"][0]["error_code"] == "candidate_urls_already_supplied"


def test_executor_surfaces_prior_observations_and_verifier_feedback_on_retry() -> None:
    """On a Verifier retry the Executor must see what was already done and what is missing."""
    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="match-jobs", skill_name="job-matching", input_model=FetchInput,
        output_model=DetailsOutput, allowed_roles=frozenset({AgentRole.executor}),
        handler=lambda _context, _payload: {"title": "AI 应用开发工程师"},
    ))
    gateway = ScriptedGateway([
        {"action": "call_tool", "tool_name": "match-jobs", "tool_input": {"url": "unused"}},
        {"action": "complete", "summary": "已生成匹配报告"},
    ])
    task = AgentTaskRequest(
        goal="推荐岗位", allowed_skills=["job-matching"],
        context={"verifier_feedback": ["missing match-observed-jobs"]},
    )
    plan = ExecutionPlan(
        task=task, created_by=AgentRole.planner, complexity=ComplexityLevel.L2,
        success_criteria=["匹配报告"],
        steps=[PlanStep(step_id="match", objective="匹配 JD", allowed_skills=["job-matching"])],
    )
    prior_fetch = ToolObservation(
        tool_name="fetch-public-job-pages", status="succeeded",
        output={"artifact_id": "observed:a", "source_url": "https://jobs.example/a",
                "title": "AI Agent 开发", "visible_text": "JD 正文", "content_hash": "a" * 64},
    )

    result = ExecutorAgent(gateway=gateway, tools=registry).run(
        task=task, plan=plan, step=plan.steps[0],
        context=ToolContext(user_id="user-a", run_id="run-a"),
        prior_observations=[prior_fetch],
    )

    assert result.status == "succeeded"
    # The prior fetch is surfaced so the model does not repeat discovery.
    assert gateway.states[0]["prior_observations"][0]["tool_name"] == "fetch-public-job-pages"
    assert gateway.states[0]["verifier_feedback"] == ["missing match-observed-jobs"]
    # The Executor called the named missing tool, not a repeat discovery fetch.
    assert result.observations[0].tool_name == "match-jobs"


def test_executor_returns_need_user_and_honors_hard_budgets() -> None:
    task = AgentTaskRequest(goal="提取 JD", allowed_skills=["job-discovery"])
    plan = ExecutionPlan(
        task=task, created_by=AgentRole.planner, complexity=ComplexityLevel.L2,
        success_criteria=["完整 JD"],
        steps=[PlanStep(step_id="discover", objective="抓取", allowed_skills=["job-discovery"])],
    )
    context = ToolContext(user_id="user-a", run_id="run-a")
    need_user = ExecutorAgent(
        gateway=ScriptedGateway([{"action": "need_user", "user_question": "请给 URL"}]),
        tools=ToolRegistry(),
    ).run(task=task, plan=plan, step=plan.steps[0], context=context)
    assert need_user.status == "needs_user"
    call_tool = {"action": "call_tool", "tool_name": "missing", "tool_input": {}}
    tool_limited = ExecutorAgent(gateway=ScriptedGateway([call_tool]), tools=ToolRegistry()).run(
        task=task, plan=plan, step=plan.steps[0], context=context, tool_budget=ToolCallBudget(1, used=1),
    )
    turn_limited = ExecutorAgent(gateway=ScriptedGateway([]), tools=ToolRegistry()).run(
        task=task, plan=plan, step=plan.steps[0], context=context, turn_budget=AgentTurnBudget(1, used=1),
    )
    exhausted = ExecutorAgent(gateway=ScriptedGateway([call_tool]), tools=ToolRegistry()).run(
        task=task.model_copy(update={"budget": task.budget.model_copy(update={"max_agent_turns": 1})}),
        plan=plan, step=plan.steps[0], context=context,
    )
    assert tool_limited.error_code == "tool_budget_exhausted"
    assert turn_limited.error_code == "agent_turn_budget_exhausted"
    assert ExecutorAgent(gateway=ScriptedGateway([]), tools=ToolRegistry()).run(
        task=task, plan=plan, step=plan.steps[0], context=context, deadline=0,
    ).error_code == "wall_clock_budget_exhausted"
    assert exhausted.status == "failed"


def test_executor_deduplicates_consecutive_identical_tool_calls_without_consuming_budget() -> None:
    """A repeat of a just-succeeded identical call is short-circuited, not re-invoked."""
    invocations = {"count": 0}

    def handler(_context, _payload):
        invocations["count"] += 1
        return {"title": "AI 应用开发工程师"}

    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="extract-page", skill_name="job-discovery", input_model=FetchInput,
        output_model=DetailsOutput, allowed_roles=frozenset({AgentRole.executor}),
        handler=handler,
    ))
    gateway = ScriptedGateway([
        {"action": "call_tool", "tool_name": "extract-page", "tool_input": {"url": "https://jobs.example/1"}},
        {"action": "call_tool", "tool_name": "extract-page", "tool_input": {"url": "https://jobs.example/1"}},
        {"action": "complete", "summary": "已提取 JD"},
    ])
    task = AgentTaskRequest(goal="提取 JD", allowed_skills=["job-discovery"])
    plan = ExecutionPlan(
        task=task, created_by=AgentRole.planner, complexity=ComplexityLevel.L2,
        success_criteria=["完整 JD"],
        steps=[PlanStep(step_id="discover", objective="提取", allowed_skills=["job-discovery"])],
    )

    result = ExecutorAgent(gateway=gateway, tools=registry).run(
        task=task, plan=plan, step=plan.steps[0],
        context=ToolContext(user_id="user-a", run_id="run-a"),
        # Only one tool call is budgeted; the duplicate must be deduped, not retried,
        # or this run would fail with tool_budget_exhausted.
        tool_budget=ToolCallBudget(1),
    )

    assert result.status == "succeeded"
    assert invocations["count"] == 1
    assert [obs.error_code for obs in result.observations] == [None, "duplicate_tool_call"]
    assert result.observations[1].tool_name == "extract-page"


def test_executor_allows_repeated_tool_call_when_input_differs() -> None:
    """A same-named call with different input is a distinct action, not a duplicate."""
    titles = iter(["岗位一", "岗位二"])

    def handler(_context, _payload):
        return {"title": next(titles)}

    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="fetch-page", skill_name="job-discovery", input_model=FetchInput,
        output_model=DetailsOutput, allowed_roles=frozenset({AgentRole.executor}),
        handler=handler,
    ))
    gateway = ScriptedGateway([
        {"action": "call_tool", "tool_name": "fetch-page", "tool_input": {"url": "https://jobs.example/1"}},
        {"action": "call_tool", "tool_name": "fetch-page", "tool_input": {"url": "https://jobs.example/2"}},
        {"action": "complete", "summary": "已抓取两个页面"},
    ])
    task = AgentTaskRequest(goal="抓取 JD", allowed_skills=["job-discovery"])
    plan = ExecutionPlan(
        task=task, created_by=AgentRole.planner, complexity=ComplexityLevel.L2,
        success_criteria=["两个 JD"],
        steps=[PlanStep(step_id="discover", objective="抓取", allowed_skills=["job-discovery"])],
    )

    result = ExecutorAgent(gateway=gateway, tools=registry).run(
        task=task, plan=plan, step=plan.steps[0],
        context=ToolContext(user_id="user-a", run_id="run-a"),
        tool_budget=ToolCallBudget(2),
    )

    assert result.status == "succeeded"
    assert [obs.output for obs in result.observations] == [
        {"title": "岗位一"}, {"title": "岗位二"},
    ]
    assert all(obs.error_code is None for obs in result.observations)


def test_executor_retries_an_identical_call_after_the_prior_one_failed() -> None:
    """A duplicate after a *failed* call is a legitimate retry, not a short-circuit."""
    attempts = {"count": 0}

    def flaky(_context, _payload):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("transient failure")
        return {"title": "AI Agent 开发工程师"}

    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="fetch-page", skill_name="job-discovery", input_model=FetchInput,
        output_model=DetailsOutput, allowed_roles=frozenset({AgentRole.executor}),
        handler=flaky,
    ))
    gateway = ScriptedGateway([
        {"action": "call_tool", "tool_name": "fetch-page", "tool_input": {"url": "https://jobs.example/1"}},
        {"action": "call_tool", "tool_name": "fetch-page", "tool_input": {"url": "https://jobs.example/1"}},
        {"action": "complete", "summary": "重试后抓取成功"},
    ])
    task = AgentTaskRequest(goal="抓取 JD", allowed_skills=["job-discovery"])
    plan = ExecutionPlan(
        task=task, created_by=AgentRole.planner, complexity=ComplexityLevel.L2,
        success_criteria=["JD"],
        steps=[PlanStep(step_id="discover", objective="抓取", allowed_skills=["job-discovery"])],
    )

    result = ExecutorAgent(gateway=gateway, tools=registry).run(
        task=task, plan=plan, step=plan.steps[0],
        context=ToolContext(user_id="user-a", run_id="run-a"),
        tool_budget=ToolCallBudget(2),
    )

    assert result.status == "succeeded"
    assert attempts["count"] == 2
    assert [obs.error_code for obs in result.observations] == ["tool_execution_failed", None]


def test_executor_deduplicates_interleaved_repeated_call_without_reinvoking_it() -> None:
    """A call that succeeded earlier is a duplicate even after other calls intervened."""
    invocations = {"a": 0, "b": 0}

    def handler_a(_context, _payload):
        invocations["a"] += 1
        return {"title": "岗位一"}

    def handler_b(_context, _payload):
        invocations["b"] += 1
        return {"title": "岗位二"}

    registry = ToolRegistry()
    for name, handler in (("fetch-a", handler_a), ("fetch-b", handler_b)):
        registry.register(ToolDefinition(
            name=name, skill_name="job-discovery", input_model=FetchInput,
            output_model=DetailsOutput, allowed_roles=frozenset({AgentRole.executor}),
            handler=handler,
        ))
    gateway = ScriptedGateway([
        {"action": "call_tool", "tool_name": "fetch-a", "tool_input": {"url": "https://jobs.example/1"}},
        {"action": "call_tool", "tool_name": "fetch-b", "tool_input": {"url": "https://jobs.example/2"}},
        {"action": "call_tool", "tool_name": "fetch-a", "tool_input": {"url": "https://jobs.example/1"}},
        {"action": "complete", "summary": "已抓取两个页面"},
    ])
    task = AgentTaskRequest(goal="抓取 JD", allowed_skills=["job-discovery"])
    plan = ExecutionPlan(
        task=task, created_by=AgentRole.planner, complexity=ComplexityLevel.L2,
        success_criteria=["两个 JD"],
        steps=[PlanStep(step_id="discover", objective="抓取", allowed_skills=["job-discovery"])],
    )

    result = ExecutorAgent(gateway=gateway, tools=registry).run(
        task=task, plan=plan, step=plan.steps[0],
        context=ToolContext(user_id="user-a", run_id="run-a"),
        # Only the two real calls are budgeted; the interleaved repeat is deduped.
        tool_budget=ToolCallBudget(2),
    )

    assert result.status == "succeeded"
    assert invocations == {"a": 1, "b": 1}
    assert [obs.error_code for obs in result.observations] == [None, None, "duplicate_tool_call"]


def test_executor_retries_a_failed_call_after_other_calls_succeeded() -> None:
    """A failed call never pollutes succeeded_calls, so a later retry stays legal."""
    attempts = {"count": 0}

    def flaky(_context, _payload):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("transient failure")
        return {"title": "AI Agent 开发工程师"}

    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="fetch-page", skill_name="job-discovery", input_model=FetchInput,
        output_model=DetailsOutput, allowed_roles=frozenset({AgentRole.executor}),
        handler=flaky,
    ))
    gateway = ScriptedGateway([
        {"action": "call_tool", "tool_name": "fetch-page", "tool_input": {"url": "https://jobs.example/1"}},
        {"action": "call_tool", "tool_name": "fetch-page", "tool_input": {"url": "https://jobs.example/2"}},
        {"action": "call_tool", "tool_name": "fetch-page", "tool_input": {"url": "https://jobs.example/1"}},
        {"action": "complete", "summary": "重试后抓取成功"},
    ])
    task = AgentTaskRequest(goal="抓取 JD", allowed_skills=["job-discovery"])
    plan = ExecutionPlan(
        task=task, created_by=AgentRole.planner, complexity=ComplexityLevel.L2,
        success_criteria=["JD"],
        steps=[PlanStep(step_id="discover", objective="抓取", allowed_skills=["job-discovery"])],
    )

    result = ExecutorAgent(gateway=gateway, tools=registry).run(
        task=task, plan=plan, step=plan.steps[0],
        context=ToolContext(user_id="user-a", run_id="run-a"),
        tool_budget=ToolCallBudget(3),
    )

    assert result.status == "succeeded"
    # All three calls ran: the failed call never entered succeeded_calls, so its
    # later retry was allowed despite the intervening successful call.
    assert attempts["count"] == 3
    assert [obs.error_code for obs in result.observations] == [
        "tool_execution_failed", None, None,
    ]


def test_executor_hands_a_stalled_duplicate_loop_to_the_user() -> None:
    """Repeated identical re-calls burn turns without evidence; the harness stops."""
    invocations = {"a": 0}

    def handler_a(_context, _payload):
        invocations["a"] += 1
        return {"title": "岗位一"}

    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="fetch-a", skill_name="job-discovery", input_model=FetchInput,
        output_model=DetailsOutput, allowed_roles=frozenset({AgentRole.executor}),
        handler=handler_a,
    ))
    gateway = ScriptedGateway([
        {"action": "call_tool", "tool_name": "fetch-a", "tool_input": {"url": "https://jobs.example/1"}},
        {"action": "call_tool", "tool_name": "fetch-a", "tool_input": {"url": "https://jobs.example/1"}},
        {"action": "call_tool", "tool_name": "fetch-a", "tool_input": {"url": "https://jobs.example/1"}},
        {"action": "call_tool", "tool_name": "fetch-a", "tool_input": {"url": "https://jobs.example/1"}},
    ])
    task = AgentTaskRequest(goal="抓取 JD", allowed_skills=["job-discovery"])
    plan = ExecutionPlan(
        task=task, created_by=AgentRole.planner, complexity=ComplexityLevel.L2,
        success_criteria=["JD"],
        steps=[PlanStep(step_id="discover", objective="抓取", allowed_skills=["job-discovery"])],
    )

    result = ExecutorAgent(gateway=gateway, tools=registry).run(
        task=task, plan=plan, step=plan.steps[0],
        context=ToolContext(user_id="user-a", run_id="run-a"),
        tool_budget=ToolCallBudget(2),
    )

    assert result.status == "needs_user"
    assert invocations == {"a": 1}
    assert "无法继续自动完成" in result.user_question
    assert [obs.error_code for obs in result.observations] == [None, "duplicate_tool_call", "duplicate_tool_call"]


def test_executor_resets_stall_counter_on_real_progress() -> None:
    """A genuine tool execution clears prior duplicates, so no stall triggers."""
    invocations = {"a": 0, "b": 0}

    def handler_a(_context, _payload):
        invocations["a"] += 1
        return {"title": "岗位一"}

    def handler_b(_context, _payload):
        invocations["b"] += 1
        return {"title": "岗位二"}

    registry = ToolRegistry()
    for name, handler in (("fetch-a", handler_a), ("fetch-b", handler_b)):
        registry.register(ToolDefinition(
            name=name, skill_name="job-discovery", input_model=FetchInput,
            output_model=DetailsOutput, allowed_roles=frozenset({AgentRole.executor}),
            handler=handler,
        ))
    gateway = ScriptedGateway([
        {"action": "call_tool", "tool_name": "fetch-a", "tool_input": {"url": "https://jobs.example/1"}},
        {"action": "call_tool", "tool_name": "fetch-a", "tool_input": {"url": "https://jobs.example/1"}},
        {"action": "call_tool", "tool_name": "fetch-b", "tool_input": {"url": "https://jobs.example/2"}},
        {"action": "call_tool", "tool_name": "fetch-a", "tool_input": {"url": "https://jobs.example/1"}},
        {"action": "complete", "summary": "两个页面均处理完成"},
    ])
    task = AgentTaskRequest(goal="抓取 JD", allowed_skills=["job-discovery"])
    plan = ExecutionPlan(
        task=task, created_by=AgentRole.planner, complexity=ComplexityLevel.L2,
        success_criteria=["两个 JD"],
        steps=[PlanStep(step_id="discover", objective="抓取", allowed_skills=["job-discovery"])],
    )

    result = ExecutorAgent(gateway=gateway, tools=registry).run(
        task=task, plan=plan, step=plan.steps[0],
        context=ToolContext(user_id="user-a", run_id="run-a"),
        tool_budget=ToolCallBudget(3),
    )

    assert result.status == "succeeded"
    assert invocations == {"a": 1, "b": 1}
    assert [obs.error_code for obs in result.observations] == [None, "duplicate_tool_call", None, "duplicate_tool_call"]


def test_executor_hands_a_blocked_search_stall_to_the_user() -> None:
    """Repeated blocked public-search decisions are a stall: ask the human."""
    registry = ToolRegistry()
    task = AgentTaskRequest(
        goal="抓取公开岗位信息",
        allowed_skills=["job-discovery"],
        context={"candidate_urls": ["https://www.liepin.com/zpjava/"]},
    )
    plan = ExecutionPlan(
        task=task, created_by=AgentRole.planner, complexity=ComplexityLevel.L2,
        success_criteria=["排名"],
        steps=[PlanStep(step_id="discover", objective="捕获", allowed_skills=["job-discovery"])],
    )
    gateway = ScriptedGateway([
        {"action": "call_tool", "tool_name": "search-public-job-pages", "tool_input": {"query": "Java 岗位"}},
        {"action": "call_tool", "tool_name": "search-public-job-pages", "tool_input": {"query": "Java 岗位"}},
        {"action": "call_tool", "tool_name": "search-public-job-pages", "tool_input": {"query": "Java 岗位"}},
    ])

    result = ExecutorAgent(gateway=gateway, tools=registry).run(
        task=task, plan=plan, step=plan.steps[0],
        context=ToolContext(user_id="user-a", run_id="run-a"),
    )

    assert result.status == "needs_user"
    assert "无效工具调用" in result.user_question
    assert [obs.error_code for obs in result.observations] == [
        "candidate_urls_already_supplied", "candidate_urls_already_supplied",
    ]


def test_executor_hands_to_user_on_interspersed_duplicate_waste() -> None:
    """R004 regression guard: interspersed duplicates trip the TOTAL cap, not consecutive.

    Pattern: success(a) -> dup(a) -> success(b) -> dup(a) -> success(c) -> dup(a).
    The consecutive counter resets on each success, so the old consecutive-only
    cap never fires. The total-wasted counter is NOT reset by interspersed
    success and reaches 3 at the third duplicate, handing the step to the user.
    """
    invocations = {"a": 0, "b": 0, "c": 0}

    def handler_a(_context, _payload):
        invocations["a"] += 1
        return {"title": "岗位一"}

    def handler_b(_context, _payload):
        invocations["b"] += 1
        return {"title": "岗位二"}

    def handler_c(_context, _payload):
        invocations["c"] += 1
        return {"title": "岗位三"}

    registry = ToolRegistry()
    for name, handler in (
        ("fetch-a", handler_a), ("fetch-b", handler_b), ("fetch-c", handler_c),
    ):
        registry.register(ToolDefinition(
            name=name, skill_name="job-discovery", input_model=FetchInput,
            output_model=DetailsOutput, allowed_roles=frozenset({AgentRole.executor}),
            handler=handler,
        ))
    gateway = ScriptedGateway([
        {"action": "call_tool", "tool_name": "fetch-a", "tool_input": {"url": "https://jobs.example/1"}},
        {"action": "call_tool", "tool_name": "fetch-a", "tool_input": {"url": "https://jobs.example/1"}},
        {"action": "call_tool", "tool_name": "fetch-b", "tool_input": {"url": "https://jobs.example/2"}},
        {"action": "call_tool", "tool_name": "fetch-a", "tool_input": {"url": "https://jobs.example/1"}},
        {"action": "call_tool", "tool_name": "fetch-c", "tool_input": {"url": "https://jobs.example/3"}},
        {"action": "call_tool", "tool_name": "fetch-a", "tool_input": {"url": "https://jobs.example/1"}},
    ])
    task = AgentTaskRequest(goal="抓取 JD", allowed_skills=["job-discovery"])
    plan = ExecutionPlan(
        task=task, created_by=AgentRole.planner, complexity=ComplexityLevel.L2,
        success_criteria=["三个 JD"],
        steps=[PlanStep(step_id="discover", objective="抓取", allowed_skills=["job-discovery"])],
    )

    result = ExecutorAgent(gateway=gateway, tools=registry).run(
        task=task, plan=plan, step=plan.steps[0],
        context=ToolContext(user_id="user-a", run_id="run-a"),
        # Enough budget for the real calls; the duplicates are deduped.
        tool_budget=ToolCallBudget(3),
    )

    assert result.status == "needs_user"
    # Each unique tool was invoked exactly once; the 3 duplicates were deduped.
    assert invocations == {"a": 1, "b": 1, "c": 1}
    # Five observations recorded (3 successes + 2 deduped dups); the 6th dup
    # tripped the total cap and was not recorded.
    assert [obs.error_code for obs in result.observations] == [
        None, "duplicate_tool_call", None, "duplicate_tool_call", None,
    ]
    assert "累计" in result.user_question


def test_executor_hands_to_user_on_alternating_no_progress_waste() -> None:
    """Q057 pattern guard: alternating failed calls trip the TOTAL cap.

    Pattern: fail(search) -> fail(fetch) -> fail(search). The consecutive
    counter resets on each real tool execution (even a failing one), so the
    consecutive-only cap never fires. The total-wasted counter counts each
    turn that produced no new succeeded observation and reaches 3 at the
    third failure, handing the step to the user.
    """
    search_attempts = {"count": 0}
    fetch_attempts = {"count": 0}

    def failing_search(_context, _payload):
        search_attempts["count"] += 1
        raise RuntimeError("search provider error")

    def failing_fetch(_context, _payload):
        fetch_attempts["count"] += 1
        raise RuntimeError("page not found")

    registry = ToolRegistry()
    for name, handler in (("search-jobs", failing_search), ("fetch-page", failing_fetch)):
        registry.register(ToolDefinition(
            name=name, skill_name="job-discovery", input_model=FetchInput,
            output_model=DetailsOutput, allowed_roles=frozenset({AgentRole.executor}),
            handler=handler,
        ))
    gateway = ScriptedGateway([
        {"action": "call_tool", "tool_name": "search-jobs", "tool_input": {"url": "https://jobs.example/s1"}},
        {"action": "call_tool", "tool_name": "fetch-page", "tool_input": {"url": "https://jobs.example/f1"}},
        {"action": "call_tool", "tool_name": "search-jobs", "tool_input": {"url": "https://jobs.example/s2"}},
    ])
    task = AgentTaskRequest(goal="抓取 JD", allowed_skills=["job-discovery"])
    plan = ExecutionPlan(
        task=task, created_by=AgentRole.planner, complexity=ComplexityLevel.L2,
        success_criteria=["JD"],
        steps=[PlanStep(step_id="discover", objective="抓取", allowed_skills=["job-discovery"])],
    )

    result = ExecutorAgent(gateway=gateway, tools=registry).run(
        task=task, plan=plan, step=plan.steps[0],
        context=ToolContext(user_id="user-a", run_id="run-a"),
        tool_budget=ToolCallBudget(3),
    )

    assert result.status == "needs_user"
    assert search_attempts["count"] == 2
    assert fetch_attempts["count"] == 1
    # All three failed tool calls were invoked and recorded (real failures are
    # auditable observations); the third failure tripped the total cap AFTER
    # its observation was recorded, unlike synthetic dedup/blocked
    # observations which are not recorded when they trip the cap.
    assert [obs.error_code for obs in result.observations] == [
        "tool_execution_failed", "tool_execution_failed", "tool_execution_failed",
    ]
    assert "累计" in result.user_question


def test_executor_does_not_trip_total_waste_cap_on_retry_after_failure() -> None:
    """A legitimate retry after a failed call must NOT trip the total-waste cap.

    Pattern: fail(a) -> succeed(a, retry) -> complete. The failed call
    increments total_wasted to 1, but the successful retry does not increment
    it (succeeded_calls grows). total_wasted stays at 1, well under the cap.
    """
    attempts = {"count": 0}

    def flaky(_context, _payload):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("transient failure")
        return {"title": "AI Agent 开发工程师"}

    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="fetch-page", skill_name="job-discovery", input_model=FetchInput,
        output_model=DetailsOutput, allowed_roles=frozenset({AgentRole.executor}),
        handler=flaky,
    ))
    gateway = ScriptedGateway([
        {"action": "call_tool", "tool_name": "fetch-page", "tool_input": {"url": "https://jobs.example/1"}},
        {"action": "call_tool", "tool_name": "fetch-page", "tool_input": {"url": "https://jobs.example/1"}},
        {"action": "complete", "summary": "重试后抓取成功"},
    ])
    task = AgentTaskRequest(goal="抓取 JD", allowed_skills=["job-discovery"])
    plan = ExecutionPlan(
        task=task, created_by=AgentRole.planner, complexity=ComplexityLevel.L2,
        success_criteria=["JD"],
        steps=[PlanStep(step_id="discover", objective="抓取", allowed_skills=["job-discovery"])],
    )

    result = ExecutorAgent(gateway=gateway, tools=registry).run(
        task=task, plan=plan, step=plan.steps[0],
        context=ToolContext(user_id="user-a", run_id="run-a"),
        tool_budget=ToolCallBudget(2),
    )

    assert result.status == "succeeded"
    assert attempts["count"] == 2
    assert [obs.error_code for obs in result.observations] == ["tool_execution_failed", None]


def test_executor_allows_two_exploratory_failures_without_tripping_total_cap() -> None:
    """1-2 exploratory empty failures are legitimate exploration, not waste.

    Pattern: fail(search, query A) -> fail(search, query B) -> succeed(fetch) -> complete.
    Two failed searches bring total_wasted to 2 (under the cap of 3); the
    subsequent successful fetch and complete are not blocked.
    """
    search_attempts = {"count": 0}

    def failing_search(_context, _payload):
        search_attempts["count"] += 1
        raise RuntimeError("no results")

    def successful_fetch(_context, _payload):
        return {"title": "AI Agent 开发工程师"}

    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="search-jobs", skill_name="job-discovery", input_model=FetchInput,
        output_model=DetailsOutput, allowed_roles=frozenset({AgentRole.executor}),
        handler=failing_search,
    ))
    registry.register(ToolDefinition(
        name="fetch-page", skill_name="job-discovery", input_model=FetchInput,
        output_model=DetailsOutput, allowed_roles=frozenset({AgentRole.executor}),
        handler=successful_fetch,
    ))
    gateway = ScriptedGateway([
        {"action": "call_tool", "tool_name": "search-jobs", "tool_input": {"url": "https://jobs.example/s1"}},
        {"action": "call_tool", "tool_name": "search-jobs", "tool_input": {"url": "https://jobs.example/s2"}},
        {"action": "call_tool", "tool_name": "fetch-page", "tool_input": {"url": "https://jobs.example/1"}},
        {"action": "complete", "summary": "已抓取 JD"},
    ])
    task = AgentTaskRequest(goal="抓取 JD", allowed_skills=["job-discovery"])
    plan = ExecutionPlan(
        task=task, created_by=AgentRole.planner, complexity=ComplexityLevel.L2,
        success_criteria=["JD"],
        steps=[PlanStep(step_id="discover", objective="抓取", allowed_skills=["job-discovery"])],
    )

    result = ExecutorAgent(gateway=gateway, tools=registry).run(
        task=task, plan=plan, step=plan.steps[0],
        context=ToolContext(user_id="user-a", run_id="run-a"),
        tool_budget=ToolCallBudget(3),
    )

    assert result.status == "succeeded"
    assert search_attempts["count"] == 2
    assert [obs.error_code for obs in result.observations] == [
        "tool_execution_failed", "tool_execution_failed", None,
    ]


def test_executor_projects_already_succeeded_calls_in_decision_state() -> None:
    """The decision state carries a compact already_succeeded_calls list.

    After a successful tool call, the next decision's state must include an
    ``already_succeeded_calls`` entry with ``tool`` and ``input_summary``
    keys, so the model can recognise "I already called this" and avoid a
    duplicate. The projection is a separate state field, so it survives
    observation summarization (it is never truncated away).
    """
    invocations = {"count": 0}

    def handler(_context, _payload):
        invocations["count"] += 1
        return {"title": "AI Agent 开发工程师"}

    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="fetch-page", skill_name="job-discovery", input_model=FetchInput,
        output_model=DetailsOutput, allowed_roles=frozenset({AgentRole.executor}),
        handler=handler,
    ))
    gateway = ScriptedGateway([
        {"action": "call_tool", "tool_name": "fetch-page", "tool_input": {"url": "https://jobs.example/1"}},
        {"action": "complete", "summary": "已抓取 JD"},
    ])
    task = AgentTaskRequest(goal="抓取 JD", allowed_skills=["job-discovery"])
    plan = ExecutionPlan(
        task=task, created_by=AgentRole.planner, complexity=ComplexityLevel.L2,
        success_criteria=["JD"],
        steps=[PlanStep(step_id="discover", objective="抓取", allowed_skills=["job-discovery"])],
    )

    result = ExecutorAgent(gateway=gateway, tools=registry).run(
        task=task, plan=plan, step=plan.steps[0],
        context=ToolContext(user_id="user-a", run_id="run-a"),
    )

    assert result.status == "succeeded"
    # The second decision's state must carry the already-succeeded call.
    second_state = gateway.states[1]
    assert "already_succeeded_calls" in second_state
    succeeded_projection = second_state["already_succeeded_calls"]
    assert len(succeeded_projection) == 1
    assert succeeded_projection[0]["tool"] == "fetch-page"
    # input_summary is a compact JSON repr of the input, not the full payload.
    assert isinstance(succeeded_projection[0]["input_summary"], str)
    assert "https://jobs.example/1" in succeeded_projection[0]["input_summary"]
    # The first decision's state has an empty list (no succeeded calls yet).
    assert gateway.states[0]["already_succeeded_calls"] == []


def test_executor_truncates_already_succeeded_calls_input_summary() -> None:
    """Large tool inputs are truncated to keep the decision state bounded."""
    long_url = "https://jobs.example/" + "x" * 300
    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="fetch-page", skill_name="job-discovery", input_model=FetchInput,
        output_model=DetailsOutput, allowed_roles=frozenset({AgentRole.executor}),
        handler=lambda _context, _payload: {"title": "岗位"},
    ))
    gateway = ScriptedGateway([
        {"action": "call_tool", "tool_name": "fetch-page", "tool_input": {"url": long_url}},
        {"action": "complete", "summary": "done"},
    ])
    task = AgentTaskRequest(goal="抓取 JD", allowed_skills=["job-discovery"])
    plan = ExecutionPlan(
        task=task, created_by=AgentRole.planner, complexity=ComplexityLevel.L2,
        success_criteria=["JD"],
        steps=[PlanStep(step_id="discover", objective="抓取", allowed_skills=["job-discovery"])],
    )

    result = ExecutorAgent(gateway=gateway, tools=registry).run(
        task=task, plan=plan, step=plan.steps[0],
        context=ToolContext(user_id="user-a", run_id="run-a"),
    )

    assert result.status == "succeeded"
    input_summary = gateway.states[1]["already_succeeded_calls"][0]["input_summary"]
    # Truncated repr ends with "..." and stays within the compact budget.
    assert input_summary.endswith("...")
    assert len(input_summary) <= 200


def test_executor_total_waste_cap_fires_in_candidate_urls_branch_after_prior_failure() -> None:
    """Total-waste cap fires in candidate_urls branch after a prior failed call.

    Pattern: fail(fetch) -> candidate_urls #1 -> candidate_urls #2.
    The failed call brings total_wasted to 1 (consecutive stays 0, reset by
    the real execution). The two candidate_urls calls bring total to 3 while
    consecutive is only 2, so the TOTAL cap fires instead of the consecutive
    cap. This covers the total-waste path in the candidate_urls branch.
    """
    fetch_attempts = {"count": 0}

    def failing_fetch(_context, _payload):
        fetch_attempts["count"] += 1
        raise RuntimeError("page not found")

    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="fetch-page", skill_name="job-discovery", input_model=FetchInput,
        output_model=DetailsOutput, allowed_roles=frozenset({AgentRole.executor}),
        handler=failing_fetch,
    ))
    task = AgentTaskRequest(
        goal="处理候选 JD",
        allowed_skills=["job-discovery"],
        context={"candidate_urls": ["https://jobs.example/agent"]},
    )
    plan = ExecutionPlan(
        task=task, created_by=AgentRole.planner, complexity=ComplexityLevel.L2,
        success_criteria=["处理候选 JD"],
        steps=[PlanStep(step_id="discover", objective="处理候选", allowed_skills=["job-discovery"])],
    )
    gateway = ScriptedGateway([
        {"action": "call_tool", "tool_name": "fetch-page", "tool_input": {"url": "https://jobs.example/1"}},
        {"action": "call_tool", "tool_name": "search-public-job-pages", "tool_input": {"query": "Java"}},
        {"action": "call_tool", "tool_name": "search-public-job-pages", "tool_input": {"query": "Java"}},
    ])

    result = ExecutorAgent(gateway=gateway, tools=registry).run(
        task=task, plan=plan, step=plan.steps[0],
        context=ToolContext(user_id="user-a", run_id="run-a"),
    )

    assert result.status == "needs_user"
    assert fetch_attempts["count"] == 1
    # The failed fetch was recorded; the first candidate_urls block was
    # recorded; the second candidate_urls block tripped the total cap
    # (total=3, consecutive=2) and was NOT recorded.
    assert [obs.error_code for obs in result.observations] == [
        "tool_execution_failed", "candidate_urls_already_supplied",
    ]
    # Total-waste message, not the consecutive candidate_urls message.
    assert "累计" in result.user_question


# ---------------------------------------------------------------------------
# B5 - cross-invocation state carried across verifier RETRY re-invocations
# ---------------------------------------------------------------------------


def _discovery_task(**updates) -> AgentTaskRequest:
    return AgentTaskRequest(
        goal="抓取 JD", allowed_skills=["job-discovery"], **updates
    )


def _single_step_plan(task: AgentTaskRequest) -> ExecutionPlan:
    return ExecutionPlan(
        task=task, created_by=AgentRole.planner, complexity=ComplexityLevel.L2,
        success_criteria=["JD"],
        steps=[PlanStep(step_id="discover", objective="抓取", allowed_skills=["job-discovery"])],
    )


def test_executor_dedups_an_identical_call_from_a_prior_invocation() -> None:
    """A call that succeeded in a prior invocation is deduped on re-invocation.

    The runtime carries the executor's execution_state on a verifier RETRY, so
    the succeeded-call set (by canonical input hash) survives the re-run. The
    re-issued identical call becomes duplicate_tool_call WITHOUT invoking the
    handler, and the carried consecutive-stall counter trips the stall cap.
    """
    invocations = {"count": 0}

    def handler(_context, _payload):
        invocations["count"] += 1
        return {"title": "AI Agent 开发工程师"}

    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="fetch-page", skill_name="job-discovery", input_model=FetchInput,
        output_model=DetailsOutput, allowed_roles=frozenset({AgentRole.executor}),
        handler=handler,
    ))
    # State as the runtime would carry it after the first invocation: one
    # succeeded call recorded and the consecutive-stall counter at 2.
    prior_state = _snapshot_execution_state(
        succeeded_calls=[("fetch-page", {"url": "https://jobs.example/1"})],
        prior_succeeded_calls=[],
        consecutive_stalls=2,
        total_wasted_turns=0,
    )
    task = _discovery_task(execution_state=prior_state)
    gateway = ScriptedGateway([
        {"action": "call_tool", "tool_name": "fetch-page", "tool_input": {"url": "https://jobs.example/1"}},
    ])

    result = ExecutorAgent(gateway=gateway, tools=registry).run(
        task=task, plan=_single_step_plan(task), step=_single_step_plan(task).steps[0],
        context=ToolContext(user_id="user-a", run_id="run-a"),
        tool_budget=ToolCallBudget(4),
    )

    assert result.status == "needs_user"
    assert invocations["count"] == 0
    # The prior succeeded call is projected into the decision state (no hash,
    # matching the model-facing projection) so the model can recognise it.
    assert gateway.states[0]["already_succeeded_calls"] == [
        {"tool": "fetch-page", "input_summary": '{"url":"https://jobs.example/1"}'}
    ]
    # Carried counter (2) + the deduped call = 3: the consecutive-stall cap
    # fires before the observation is recorded, so the list is empty.
    assert "连续重复调用" in result.user_question
    assert result.observations == []
    # The failure snapshot preserves the full dedup set for the next retry.
    expected_hash = _input_hash({"url": "https://jobs.example/1"})
    assert result.execution_state["succeeded_calls"][0]["hash"] == expected_hash
    assert result.execution_state["consecutive_stalls"] == 3
    assert result.execution_state["total_wasted_turns"] == 1


def test_executor_carries_total_waste_counter_across_invocations() -> None:
    """Wasted turns from a prior invocation are NOT reset by a RETRY (C005).

    Two wasted turns were carried; the first failing call of this invocation
    brings the total to 3 and hands the step to the user immediately.
    """
    invocations = {"count": 0}

    def failing(_context, _payload):
        invocations["count"] += 1
        raise RuntimeError("provider down")

    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="fetch-page", skill_name="job-discovery", input_model=FetchInput,
        output_model=DetailsOutput, allowed_roles=frozenset({AgentRole.executor}),
        handler=failing,
    ))
    prior_state = _snapshot_execution_state(
        succeeded_calls=[], prior_succeeded_calls=[], consecutive_stalls=0,
        total_wasted_turns=2,
    )
    task = _discovery_task(execution_state=prior_state)
    gateway = ScriptedGateway([
        {"action": "call_tool", "tool_name": "fetch-page", "tool_input": {"url": "https://jobs.example/1"}},
    ])

    result = ExecutorAgent(gateway=gateway, tools=registry).run(
        task=task, plan=_single_step_plan(task), step=_single_step_plan(task).steps[0],
        context=ToolContext(user_id="user-a", run_id="run-a"),
        tool_budget=ToolCallBudget(4),
    )

    assert result.status == "needs_user"
    assert invocations["count"] == 1
    assert "累计" in result.user_question
    assert [obs.error_code for obs in result.observations] == ["tool_execution_failed"]


def test_snapshot_execution_state_caps_persisted_entries_keeping_most_recent() -> None:
    calls = [
        ("fetch-page", {"url": f"https://jobs.example/{i}"}) for i in range(45)
    ]
    snapshot = _snapshot_execution_state(
        succeeded_calls=calls, prior_succeeded_calls=[], consecutive_stalls=1,
        total_wasted_turns=2,
    )
    entries = snapshot["succeeded_calls"]
    assert len(entries) == 40
    assert entries[0]["tool"] == "fetch-page"
    assert entries[-1]["tool"] == "fetch-page"
    assert entries[0]["input_summary"] == '{"url":"https://jobs.example/5"}'
    assert entries[-1]["input_summary"] == '{"url":"https://jobs.example/44"}'
    assert all("hash" in entry for entry in entries)
    assert snapshot["consecutive_stalls"] == 1
    assert snapshot["total_wasted_turns"] == 2


def test_snapshot_execution_state_merges_prior_entries_before_current() -> None:
    prior = [{"tool": "fetch-old", "hash": "a" * 64, "input_summary": "old"}]
    snapshot = _snapshot_execution_state(
        succeeded_calls=[("fetch-page", {"url": "https://jobs.example/1"})],
        prior_succeeded_calls=prior, consecutive_stalls=0, total_wasted_turns=0,
    )
    entries = snapshot["succeeded_calls"]
    assert len(entries) == 2
    assert entries[0]["tool"] == "fetch-old"
    assert entries[0]["hash"] == "a" * 64
    assert entries[1]["tool"] == "fetch-page"
    assert entries[1]["hash"] == _input_hash({"url": "https://jobs.example/1"})


def test_load_execution_state_drops_malformed_entries_and_counter_garbage() -> None:
    task = _discovery_task(execution_state={
        "succeeded_calls": [
            "junk",
            {"tool": "ok", "hash": "h" * 64, "input_summary": "s"},
            {"tool": 123, "hash": "h" * 64},
            {"tool": "no-hash"},
            {"hash": "h" * 64},
            {"tool": "empty-hash", "hash": ""},
            {"tool": "no-summary", "hash": "z" * 64},
        ],
        "consecutive_stalls": True,
        "total_wasted_turns": "3",
    })
    prior, stalls, waste = _load_execution_state(task)
    assert prior == [
        {"tool": "ok", "hash": "h" * 64, "input_summary": "s"},
        {"tool": "no-summary", "hash": "z" * 64, "input_summary": ""},
    ]
    assert stalls == 0
    assert waste == 0


def test_carried_counter_rejects_non_counter_values() -> None:
    assert _carried_counter(3) == 3
    assert _carried_counter(2, default=7) == 2
    assert _carried_counter(True) == 0
    assert _carried_counter(-1) == 0
    assert _carried_counter("3") == 0
    assert _carried_counter(None) == 0
    assert _carried_counter(None, default=7) == 7

