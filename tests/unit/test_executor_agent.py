"""Executor Agent behavior on the production Deep path.

Stage 1.2: the legacy non-Deep loop was removed, so every Executor test here
drives the DeepAgents loop through a scripted chat model
(tests.unit.deepagents_testkit). The legacy decide-style scripts are
converted mechanically by scripted_executor_model; assertions that only
made sense for the old loop's decision state (gateway.states) were
replaced with observation/status assertions or dropped where the Deep path
has its own coverage in test_deep_executor.py.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from backend.app.domain.agent_runtime import AgentRole, ComplexityLevel
from backend.app.services.agent_runtime.executor.execution_policy import (
    normalized_step_tool_input,
)
from backend.app.services.agent_runtime.executor.execution_state import (
    carried_counter,
    input_hash,
    load_execution_state,
    snapshot_execution_state,
)
from backend.app.services.agent_runtime.executor_agent import ExecutorAgent
from backend.app.services.agent_runtime.observation_projection import (
    observation_for_decision,
)
from backend.app.services.agent_runtime.schemas import (
    AgentTaskRequest,
    ExecutionPlan,
    PlanStep,
    ToolObservation,
)
from backend.app.services.agent_runtime.skill_definition import SkillRegistry
from backend.app.services.agent_runtime.tool_budget import ToolCallBudget
from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.agent_runtime.tool_registry import ToolDefinition, ToolRegistry
from backend.app.services.agent_runtime.turn_budget import AgentTurnBudget
from tests.unit.deepagents_testkit import (
    DeepGateway,
    scripted_executor_model,
)


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


class SearchInput(BaseModel):
    query: str


class SheetQueryInput(BaseModel):
    company_keywords: list[str] = []
    role_keywords: list[str] = []
    location_keywords: list[str] = []
    recent_days: int | None = None


class SheetQueryOutput(BaseModel):
    records: list[dict[str, Any]]
    source_url: str
    content_hash: str


def _agent(
    gateway: DeepGateway,
    registry: ToolRegistry,
    *,
    skills: SkillRegistry | None = None,
) -> ExecutorAgent:
    return ExecutorAgent(
        gateway=gateway,
        tools=registry,
        skills=skills or SkillRegistry(),
    )


def _task(goal: str, allowed_skills: list[str]) -> AgentTaskRequest:
    return AgentTaskRequest(goal=goal, allowed_skills=allowed_skills)


def _context(user_id: str = "user-a", run_id: str = "run-a") -> ToolContext:
    return ToolContext(user_id=user_id, run_id=run_id)


def _single_step_plan(
    task: AgentTaskRequest, objective: str
) -> ExecutionPlan:
    return ExecutionPlan(
        task=task,
        created_by=AgentRole.planner,
        complexity=ComplexityLevel.L2,
        success_criteria=["完成"],
        steps=[
            PlanStep(
                step_id="discover",
                objective=objective,
                allowed_skills=task.allowed_skills,
            )
        ],
    )


def test_executor_completes_recent_company_routing_step_after_sheet_result() -> None:
    """A routing step closes deterministically once its sheet list observation lands."""
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
    model = scripted_executor_model(
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
    task = _task(
        "先列出最近1天更新的公司清单，再逐公司核实 AIGC 产品经理岗位。",
        ["job-discovery"],
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

    result = _agent(DeepGateway(model), registry).run(
        task=task,
        plan=plan,
        step=plan.steps[0],
        context=_context(),
    )

    assert result.status == "succeeded"
    # The routing contract completes the step even though the script carries
    # no terminal decision: the deterministic routing completion ends the run.
    assert captured[0].recent_days == 1
    assert [observation.tool_name for observation in result.observations] == [
        "query-career-sheet-records"
    ]

def test_executor_normalizes_tailoring_target_to_goal_constrained_source() -> None:
    """A model-selected unrelated artifact cannot consume the tailoring call."""
    task = _task(
        "在猎聘网找北京的 AIGC 产品经理（应届生）岗位，并定制简历。",
        ["resume-tailoring"],
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

    normalized = normalized_step_tool_input(
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
    task = _task(
        "基于上一环节找到的岗位和我的简历，为最匹配的岗位生成修改建议。",
        ["resume-tailoring"],
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

    normalized = normalized_step_tool_input(
        plan,
        step,
        "build-resume-tailoring-brief",
        {"target_artifact_id": "baiont", "target_keywords": ["Agent", "RAG"]},
        task=task,
        context=context,
    )

    assert normalized["target_artifact_id"] == "baiont:candidate:1"
    assert normalized["target_keywords"] == ["Agent", "RAG", "AI"]


def test_executor_projects_batch_details_to_identifiers_and_titles_only() -> None:
    projected = observation_for_decision(
        ToolObservation(
            tool_name="extract-observed-job-details-batch",
            status="succeeded",
            output={
                "details": [
                    {
                        "source_artifact_id": "observed:a",
                        "source_url": "https://jobs.example/a",
                        "content_hash": "a" * 64,
                        "candidates": [{"title": "岗位 A", "responsibilities": "x" * 5_000}],
                    }
                ]
            },
        )
    )

    assert projected["output"]["details"] == [
        {
            "source_artifact_id": "observed:a",
            "source_url": "https://jobs.example/a",
            "content_hash": "a" * 64,
            "candidate_titles": ["岗位 A"],
        }
    ]


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
    gateway = DeepGateway(
        scripted_executor_model(
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
                },
            ]
        )
    )
    task = _task("找岗位", ["job-discovery"])
    plan = _single_step_plan(task, "提取公开 JD")

    result = _agent(gateway, registry).run(
        task=task,
        plan=plan,
        step=plan.steps[0],
        context=_context(),
    )

    assert result.status == "succeeded"
    assert [item.error_code for item in result.observations] == [
        "tool_execution_failed",
        None,
    ]
    assert result.observations[1].output == {
        "source_url": "https://jobs.example/1",
        "title": "AI 应用开发工程师",
    }

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
    gateway = DeepGateway(scripted_executor_model([
        {"action": "call_tool", "tool_name": "fetch-page", "tool_input": {"url": "https://jobs.example/1"}},
        {"action": "call_tool", "tool_name": "extract-page", "tool_input": {"url": "https://jobs.example/1"}},
        {"action": "complete", "summary": "已提取完整 JD"},
    ]))
    task = _task("提取 JD", ["job-discovery"])
    plan = _single_step_plan(task, "抓取并提取")

    result = _agent(gateway, registry).run(
        task=task, plan=plan, step=plan.steps[0],
        context=_context(),
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
    gateway = DeepGateway(scripted_executor_model([
        {"action": "call_tool", "tool_name": "fetch-pages", "tool_input": {"url": "unused"}},
        {"action": "call_tool", "tool_name": "inspect-pages", "tool_input": {"url": "unused"}},
        {"action": "complete", "summary": "已检查批量 JD"},
    ]))
    task = _task("提取 JD", ["job-discovery"])
    plan = _single_step_plan(task, "批量抓取")

    result = _agent(gateway, registry).run(
        task=task, plan=plan, step=plan.steps[0],
        context=_context(),
    )

    # Every distinct page is exposed to the next tool call (deduplicated by id).
    assert result.observations[1].output == {"title": "岗位 A,岗位 B"}


def test_executor_keeps_agent_in_control_after_blocking_redundant_search() -> None:
    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="search-public-job-pages", skill_name="job-discovery", input_model=SearchInput,
        output_model=DetailsOutput, allowed_roles=frozenset({AgentRole.executor}),
        handler=lambda _context, _payload: {"title": "unused"},
    ))
    gateway = DeepGateway(scripted_executor_model([
        {"action": "call_tool", "tool_name": "search-public-job-pages", "tool_input": {"query": "unused"}},
        {"action": "complete", "summary": "改用已提供的候选页面"},
    ]))
    task = _task(
        "处理候选 JD",
        ["job-discovery"],
    )
    task = task.model_copy(update={"context": {"candidate_urls": ["https://jobs.example/agent"]}})
    plan = _single_step_plan(task, "处理候选")

    result = _agent(gateway, registry).run(
        task=task, plan=plan, step=plan.steps[0],
        context=_context(),
    )

    assert result.status == "succeeded"
    assert result.observations[0].error_code == "candidate_urls_already_supplied"


def test_executor_surfaces_prior_observations_and_verifier_feedback_on_retry() -> None:
    """On a Verifier retry the Executor sees the model input already carrying prior evidence."""
    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="match-jobs", skill_name="job-matching", input_model=FetchInput,
        output_model=DetailsOutput, allowed_roles=frozenset({AgentRole.executor}),
        handler=lambda _context, _payload: {"title": "AI 应用开发工程师"},
    ))
    gateway = DeepGateway(scripted_executor_model([
        {"action": "call_tool", "tool_name": "match-jobs", "tool_input": {"url": "unused"}},
        {"action": "complete", "summary": "已生成匹配报告"},
    ]))
    task = _task("推荐岗位", ["job-matching"])
    task = task.model_copy(
        update={"context": {"verifier_feedback": ["missing match-observed-jobs"]}}
    )
    plan = _single_step_plan(task, "匹配 JD")
    prior_fetch = ToolObservation(
        tool_name="fetch-public-job-pages", status="succeeded",
        output={"artifact_id": "observed:a", "source_url": "https://jobs.example/a",
                "title": "AI Agent 开发", "visible_text": "JD 正文", "content_hash": "a" * 64},
    )

    result = _agent(gateway, registry).run(
        task=task, plan=plan, step=plan.steps[0],
        context=_context(),
        prior_observations=[prior_fetch],
    )

    assert result.status == "succeeded"
    # The Executor called the named missing tool, not a repeat discovery fetch.
    assert result.observations[0].tool_name == "match-jobs"


def test_executor_returns_need_user_and_honors_hard_budgets() -> None:
    task = _task("提取 JD", ["job-discovery"])
    plan = _single_step_plan(task, "抓取")
    context = _context()
    need_user = _agent(
        DeepGateway(scripted_executor_model([
            {"action": "need_user", "user_question": "请给 URL"},
        ])),
        ToolRegistry(),
    ).run(task=task, plan=plan, step=plan.steps[0], context=context)
    assert need_user.status == "needs_user"

    budget_registry = ToolRegistry()
    budget_registry.register(ToolDefinition(
        name="fetch-page", skill_name="job-discovery", input_model=FetchInput,
        output_model=DetailsOutput, allowed_roles=frozenset({AgentRole.executor}),
        handler=lambda _context, _payload: {"title": "x"},
    ))
    tool_limited = _agent(
        DeepGateway(scripted_executor_model([
            {"action": "call_tool", "tool_name": "fetch-page", "tool_input": {"url": "https://jobs.example/1"}},
        ])),
        budget_registry,
    ).run(task=task, plan=plan, step=plan.steps[0], context=context, tool_budget=ToolCallBudget(1, used=1))
    assert tool_limited.error_code == "tool_budget_exhausted"

    turn_limited = _agent(DeepGateway(scripted_executor_model([])), ToolRegistry()).run(
        task=task, plan=plan, step=plan.steps[0], context=context, turn_budget=AgentTurnBudget(1, used=1),
    )
    assert turn_limited.error_code == "agent_turn_budget_exhausted"

    exhausted = _agent(DeepGateway(scripted_executor_model([])), ToolRegistry()).run(
        task=task.model_copy(update={"budget": task.budget.model_copy(update={"max_agent_turns": 1})}),
        plan=plan, step=plan.steps[0], context=context,
    )
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
    gateway = DeepGateway(scripted_executor_model([
        {"action": "call_tool", "tool_name": "extract-page", "tool_input": {"url": "https://jobs.example/1"}},
        {"action": "call_tool", "tool_name": "extract-page", "tool_input": {"url": "https://jobs.example/1"}},
        {"action": "complete", "summary": "已提取 JD"},
    ]))
    task = _task("提取 JD", ["job-discovery"])
    plan = _single_step_plan(task, "提取")

    result = _agent(gateway, registry).run(
        task=task, plan=plan, step=plan.steps[0],
        context=_context(),
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
    gateway = DeepGateway(scripted_executor_model([
        {"action": "call_tool", "tool_name": "fetch-page", "tool_input": {"url": "https://jobs.example/1"}},
        {"action": "call_tool", "tool_name": "fetch-page", "tool_input": {"url": "https://jobs.example/2"}},
        {"action": "complete", "summary": "已抓取两个页面"},
    ]))
    task = _task("抓取 JD", ["job-discovery"])
    plan = _single_step_plan(task, "抓取")

    result = _agent(gateway, registry).run(
        task=task, plan=plan, step=plan.steps[0],
        context=_context(),
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
    gateway = DeepGateway(scripted_executor_model([
        {"action": "call_tool", "tool_name": "fetch-page", "tool_input": {"url": "https://jobs.example/1"}},
        {"action": "call_tool", "tool_name": "fetch-page", "tool_input": {"url": "https://jobs.example/1"}},
        {"action": "complete", "summary": "重试后抓取成功"},
    ]))
    task = _task("抓取 JD", ["job-discovery"])
    plan = _single_step_plan(task, "抓取")

    result = _agent(gateway, registry).run(
        task=task, plan=plan, step=plan.steps[0],
        context=_context(),
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
    gateway = DeepGateway(scripted_executor_model([
        {"action": "call_tool", "tool_name": "fetch-a", "tool_input": {"url": "https://jobs.example/1"}},
        {"action": "call_tool", "tool_name": "fetch-b", "tool_input": {"url": "https://jobs.example/2"}},
        {"action": "call_tool", "tool_name": "fetch-a", "tool_input": {"url": "https://jobs.example/1"}},
        {"action": "complete", "summary": "已抓取两个页面"},
    ]))
    task = _task("抓取 JD", ["job-discovery"])
    plan = _single_step_plan(task, "抓取")

    result = _agent(gateway, registry).run(
        task=task, plan=plan, step=plan.steps[0],
        context=_context(),
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
    gateway = DeepGateway(scripted_executor_model([
        {"action": "call_tool", "tool_name": "fetch-page", "tool_input": {"url": "https://jobs.example/1"}},
        {"action": "call_tool", "tool_name": "fetch-page", "tool_input": {"url": "https://jobs.example/2"}},
        {"action": "call_tool", "tool_name": "fetch-page", "tool_input": {"url": "https://jobs.example/1"}},
        {"action": "complete", "summary": "重试后抓取成功"},
    ]))
    task = _task("抓取 JD", ["job-discovery"])
    plan = _single_step_plan(task, "抓取")

    result = _agent(gateway, registry).run(
        task=task, plan=plan, step=plan.steps[0],
        context=_context(),
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
    gateway = DeepGateway(scripted_executor_model([
        {"action": "call_tool", "tool_name": "fetch-a", "tool_input": {"url": "https://jobs.example/1"}},
        {"action": "call_tool", "tool_name": "fetch-a", "tool_input": {"url": "https://jobs.example/1"}},
        {"action": "call_tool", "tool_name": "fetch-a", "tool_input": {"url": "https://jobs.example/1"}},
        {"action": "call_tool", "tool_name": "fetch-a", "tool_input": {"url": "https://jobs.example/1"}},
    ]))
    task = _task("抓取 JD", ["job-discovery"])
    plan = _single_step_plan(task, "抓取")

    result = _agent(gateway, registry).run(
        task=task, plan=plan, step=plan.steps[0],
        context=_context(),
        tool_budget=ToolCallBudget(2),
    )

    assert result.status == "needs_user"
    assert result.error_code == "executor_stalled"
    assert invocations == {"a": 1}
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
    gateway = DeepGateway(scripted_executor_model([
        {"action": "call_tool", "tool_name": "fetch-a", "tool_input": {"url": "https://jobs.example/1"}},
        {"action": "call_tool", "tool_name": "fetch-a", "tool_input": {"url": "https://jobs.example/1"}},
        {"action": "call_tool", "tool_name": "fetch-b", "tool_input": {"url": "https://jobs.example/2"}},
        {"action": "call_tool", "tool_name": "fetch-a", "tool_input": {"url": "https://jobs.example/1"}},
        {"action": "complete", "summary": "两个页面均处理完成"},
    ]))
    task = _task("抓取 JD", ["job-discovery"])
    plan = _single_step_plan(task, "抓取")

    result = _agent(gateway, registry).run(
        task=task, plan=plan, step=plan.steps[0],
        context=_context(),
        tool_budget=ToolCallBudget(3),
    )

    assert result.status == "succeeded"
    assert invocations == {"a": 1, "b": 1}
    assert [obs.error_code for obs in result.observations] == [None, "duplicate_tool_call", None, "duplicate_tool_call"]


def test_executor_hands_a_blocked_search_stall_to_the_user() -> None:
    """Repeated blocked public-search decisions are a stall: ask the human."""
    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="search-public-job-pages", skill_name="job-discovery", input_model=SearchInput,
        output_model=DetailsOutput, allowed_roles=frozenset({AgentRole.executor}),
        handler=lambda _context, _payload: {"title": "unused"},
    ))
    task = _task("抓取公开岗位信息", ["job-discovery"])
    task = task.model_copy(
        update={"context": {"candidate_urls": ["https://www.liepin.com/zpjava/"]}}
    )
    plan = _single_step_plan(task, "捕获")
    gateway = DeepGateway(scripted_executor_model([
        {"action": "call_tool", "tool_name": "search-public-job-pages", "tool_input": {"query": "Java 岗位"}},
        {"action": "call_tool", "tool_name": "search-public-job-pages", "tool_input": {"query": "Java 岗位"}},
        {"action": "call_tool", "tool_name": "search-public-job-pages", "tool_input": {"query": "Java 岗位"}},
    ]))

    result = _agent(gateway, registry).run(
        task=task, plan=plan, step=plan.steps[0],
        context=_context(),
    )

    assert result.status == "needs_user"
    assert result.error_code == "executor_stalled"
    assert [obs.error_code for obs in result.observations] == [
        "candidate_urls_already_supplied", "candidate_urls_already_supplied",
    ]


def test_executor_hands_to_user_on_interspersed_duplicate_waste() -> None:
    """Interspersed duplicates trip the TOTAL cap (3) even when consecutive resets."""
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
    gateway = DeepGateway(scripted_executor_model([
        {"action": "call_tool", "tool_name": "fetch-a", "tool_input": {"url": "https://jobs.example/1"}},
        {"action": "call_tool", "tool_name": "fetch-a", "tool_input": {"url": "https://jobs.example/1"}},
        {"action": "call_tool", "tool_name": "fetch-b", "tool_input": {"url": "https://jobs.example/2"}},
        {"action": "call_tool", "tool_name": "fetch-a", "tool_input": {"url": "https://jobs.example/1"}},
        {"action": "call_tool", "tool_name": "fetch-c", "tool_input": {"url": "https://jobs.example/3"}},
        {"action": "call_tool", "tool_name": "fetch-a", "tool_input": {"url": "https://jobs.example/1"}},
    ]))
    task = _task("抓取 JD", ["job-discovery"])
    plan = _single_step_plan(task, "抓取")

    result = _agent(gateway, registry).run(
        task=task, plan=plan, step=plan.steps[0],
        context=_context(),
    )

    assert result.status == "needs_user"
    assert result.error_code == "executor_stalled"
    assert invocations == {"a": 1, "b": 1, "c": 1}
    # The third duplicate raises the stall before its observation is appended.
    assert [obs.error_code for obs in result.observations] == [
        None, "duplicate_tool_call", None, "duplicate_tool_call", None,
    ]


def test_executor_hands_to_user_on_alternating_no_progress_waste() -> None:
    """Alternating duplicate pairs also trip the total-waste cap."""
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
    gateway = DeepGateway(scripted_executor_model([
        {"action": "call_tool", "tool_name": "fetch-a", "tool_input": {"url": "https://jobs.example/1"}},
        {"action": "call_tool", "tool_name": "fetch-a", "tool_input": {"url": "https://jobs.example/1"}},
        {"action": "call_tool", "tool_name": "fetch-b", "tool_input": {"url": "https://jobs.example/2"}},
        {"action": "call_tool", "tool_name": "fetch-b", "tool_input": {"url": "https://jobs.example/2"}},
        {"action": "call_tool", "tool_name": "fetch-a", "tool_input": {"url": "https://jobs.example/1"}},
        {"action": "call_tool", "tool_name": "fetch-a", "tool_input": {"url": "https://jobs.example/1"}},
    ]))
    task = _task("抓取 JD", ["job-discovery"])
    plan = _single_step_plan(task, "抓取")

    result = _agent(gateway, registry).run(
        task=task, plan=plan, step=plan.steps[0],
        context=_context(),
    )

    assert result.status == "needs_user"
    assert result.error_code == "executor_stalled"
    assert [obs.error_code for obs in result.observations] == [
        None, "duplicate_tool_call", None, "duplicate_tool_call",
    ]


def test_executor_does_not_trip_total_waste_cap_on_retry_after_failure() -> None:
    """A failed call followed by a successful retry never counts as waste."""
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
    gateway = DeepGateway(scripted_executor_model([
        {"action": "call_tool", "tool_name": "fetch-page", "tool_input": {"url": "https://jobs.example/1"}},
        {"action": "call_tool", "tool_name": "fetch-page", "tool_input": {"url": "https://jobs.example/1"}},
        {"action": "complete", "summary": "重试后抓取成功"},
    ]))
    task = _task("抓取 JD", ["job-discovery"])
    plan = _single_step_plan(task, "抓取")

    result = _agent(gateway, registry).run(
        task=task, plan=plan, step=plan.steps[0],
        context=_context(),
    )

    assert result.status == "succeeded"
    assert attempts["count"] == 2


def test_executor_allows_two_exploratory_failures_without_tripping_total_cap() -> None:
    """Exploratory tool failures are not waste; the third real call may succeed."""
    calls: list[str] = []

    def fail_a(_context, _payload):
        calls.append("a")
        raise RuntimeError("down")

    def fail_b(_context, _payload):
        calls.append("b")
        raise RuntimeError("down")

    def ok_c(_context, _payload):
        calls.append("c")
        return {"title": "AI Agent 开发工程师"}

    registry = ToolRegistry()
    for name, handler in (("fetch-a", fail_a), ("fetch-b", fail_b), ("fetch-c", ok_c)):
        registry.register(ToolDefinition(
            name=name, skill_name="job-discovery", input_model=FetchInput,
            output_model=DetailsOutput, allowed_roles=frozenset({AgentRole.executor}),
            handler=handler,
        ))
    gateway = DeepGateway(scripted_executor_model([
        {"action": "call_tool", "tool_name": "fetch-a", "tool_input": {"url": "https://jobs.example/1"}},
        {"action": "call_tool", "tool_name": "fetch-b", "tool_input": {"url": "https://jobs.example/2"}},
        {"action": "call_tool", "tool_name": "fetch-c", "tool_input": {"url": "https://jobs.example/3"}},
        {"action": "complete", "summary": "第三个工具成功"},
    ]))
    task = _task("抓取 JD", ["job-discovery"])
    plan = _single_step_plan(task, "抓取")

    result = _agent(gateway, registry).run(
        task=task, plan=plan, step=plan.steps[0],
        context=_context(),
    )

    assert result.status == "succeeded"
    assert calls == ["a", "b", "c"]


def test_executor_total_waste_cap_fires_in_candidate_urls_branch_after_prior_failure() -> None:
    """Candidate-gate rejections count as waste even after an exploratory failure."""
    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="fetch-page", skill_name="job-discovery", input_model=FetchInput,
        output_model=DetailsOutput, allowed_roles=frozenset({AgentRole.executor}),
        handler=lambda _context, _payload: (_ for _ in ()).throw(RuntimeError("down")),
    ))
    registry.register(ToolDefinition(
        name="search-public-job-pages", skill_name="job-discovery", input_model=SearchInput,
        output_model=DetailsOutput, allowed_roles=frozenset({AgentRole.executor}),
        handler=lambda _context, _payload: {"title": "unused"},
    ))
    task = _task("抓取 JD", ["job-discovery"])
    task = task.model_copy(
        update={"context": {"candidate_urls": ["https://jobs.example/a"]}}
    )
    plan = _single_step_plan(task, "抓取")
    gateway = DeepGateway(scripted_executor_model([
        {"action": "call_tool", "tool_name": "fetch-page", "tool_input": {"url": "https://jobs.example/a"}},
        {"action": "call_tool", "tool_name": "search-public-job-pages", "tool_input": {"query": "q"}},
        {"action": "call_tool", "tool_name": "search-public-job-pages", "tool_input": {"query": "q"}},
        {"action": "call_tool", "tool_name": "search-public-job-pages", "tool_input": {"query": "q"}},
    ]))

    result = _agent(gateway, registry).run(
        task=task, plan=plan, step=plan.steps[0],
        context=_context(),
    )

    assert result.status == "needs_user"
    assert result.error_code == "executor_stalled"


def test_executor_dedups_an_identical_call_from_a_prior_invocation() -> None:
    """A call that already succeeded in a prior invocation is deduped immediately."""
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
    prior_state = snapshot_execution_state(
        succeeded_calls=[("extract-page", {"url": "https://jobs.example/1"})],
        prior_succeeded_calls=[],
        consecutive_stalls=0,
        total_wasted_turns=0,
    )
    task = _task("提取 JD", ["job-discovery"])
    task = task.model_copy(update={"execution_state": prior_state})
    plan = _single_step_plan(task, "提取")
    gateway = DeepGateway(scripted_executor_model([
        {"action": "call_tool", "tool_name": "extract-page", "tool_input": {"url": "https://jobs.example/1"}},
        {"action": "complete", "summary": "已提取 JD"},
    ]))

    result = _agent(gateway, registry).run(
        task=task, plan=plan, step=plan.steps[0],
        context=_context(),
    )

    assert result.status == "succeeded"
    assert invocations["count"] == 0
    assert [obs.error_code for obs in result.observations] == ["duplicate_tool_call"]


def test_executor_carries_total_waste_counter_across_invocations() -> None:
    """Carried waste counts toward the stall cap in the next invocation."""
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
    prior_state = snapshot_execution_state(
        succeeded_calls=[],
        prior_succeeded_calls=[],
        consecutive_stalls=0,
        total_wasted_turns=2,
    )
    task = _task("提取 JD", ["job-discovery"])
    task = task.model_copy(update={"execution_state": prior_state})
    plan = _single_step_plan(task, "提取")
    gateway = DeepGateway(scripted_executor_model([
        {"action": "call_tool", "tool_name": "extract-page", "tool_input": {"url": "https://jobs.example/1"}},
        {"action": "call_tool", "tool_name": "extract-page", "tool_input": {"url": "https://jobs.example/1"}},
    ]))

    result = _agent(gateway, registry).run(
        task=task, plan=plan, step=plan.steps[0],
        context=_context(),
    )

    assert result.status == "needs_user"
    assert result.error_code == "executor_stalled"
    assert invocations["count"] == 1


def test_snapshot_execution_state_caps_persisted_entries_keeping_most_recent() -> None:
    snapshot = snapshot_execution_state(
        succeeded_calls=[("fetch-page", {"url": f"https://jobs.example/{index}"}) for index in range(60)],
        prior_succeeded_calls=[],
        consecutive_stalls=0,
        total_wasted_turns=0,
    )
    entries = snapshot["succeeded_calls"]
    assert len(entries) == 40
    assert entries[-1]["hash"] == input_hash({"url": "https://jobs.example/59"})
    assert entries[0]["hash"] == input_hash({"url": "https://jobs.example/20"})


def test_snapshot_execution_state_merges_prior_entries_before_current() -> None:
    snapshot = snapshot_execution_state(
        succeeded_calls=[("fetch-page", {"url": "https://jobs.example/current"})],
        prior_succeeded_calls=[{"tool": "fetch-page", "hash": input_hash({"url": "https://jobs.example/prior"}), "input_summary": ""}],
        consecutive_stalls=0,
        total_wasted_turns=0,
    )
    entries = snapshot["succeeded_calls"]
    assert len(entries) == 2
    assert entries[0]["hash"] == input_hash({"url": "https://jobs.example/prior"})
    assert entries[1]["hash"] == input_hash({"url": "https://jobs.example/current"})


def test_load_execution_state_drops_malformed_entries_and_counter_garbage() -> None:
    task = _task("提取 JD", ["job-discovery"])
    task = task.model_copy(
        update={
            "execution_state": {
                "succeeded_calls": [
                    "junk",
                    {"tool": "ok", "hash": "h" * 64},
                    {"tool": 123, "hash": "h" * 64},
                    {"tool": "no-hash"},
                ],
                "consecutive_stalls": "garbage",
                "total_wasted_turns": None,
            }
        }
    )
    calls, stalls, wasted = load_execution_state(task)
    assert calls == [{"tool": "ok", "hash": "h" * 64, "input_summary": ""}]
    assert stalls == 0
    assert wasted == 0


def test_carried_counter_rejects_non_counter_values() -> None:
    assert carried_counter(None) == 0
    assert carried_counter("garbage") == 0
    assert carried_counter(-3) == 0
    assert carried_counter(7) == 7
