"""Round-4 candidate B: match-observed-jobs input tolerance + invalid_tool_input dedup.

Mechanism (leader_decision_round4.md RC-1 / D2, engineer_architecture_round4.md
N1, engineer_tool_round4.md W4): step 2 (job-matching) called
``match-observed-jobs`` three times with model-facing shapes that failed pydantic
validation (Chinese ``ranking_criteria`` values, string ``profile_keywords``).
Each ``invalid_tool_input`` wasted one turn; three wastes tripped
``_MAX_TOTAL_WASTED_TURNS`` and ended the run as waiting_user. Candidate B makes
the schema tolerant (data-driven alias mapping onto the existing Literal domain
+ string-keyword coercion, recorded in ``normalization_warnings``) and treats
``invalid_tool_input`` as a stable failure so an identical doomed re-issue is
deduped instead of burning another wasted turn. Unknown criteria keep today's
ValidationError behavior; canonical payloads are unchanged.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from backend.app.domain.agent_runtime import AgentRole, ComplexityLevel
from backend.app.services.agent_runtime.executor_agent import ExecutorAgent
from backend.app.services.agent_runtime.schemas import (
    AgentTaskRequest,
    ExecutionPlan,
    PlanStep,
)
from backend.app.services.agent_runtime.tool_budget import ToolCallBudget
from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.agent_runtime.tool_registry import ToolDefinition, ToolRegistry
from backend.app.services.career_skills.job_matching import (
    MatchObservedJobsInput,
    match_observed_jobs,
)


class MatchOutput(BaseModel):
    title: str


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


# --- input tolerance: profile_keywords --------------------------------------


def test_profile_keywords_string_is_split_into_keyword_list() -> None:
    payload = MatchObservedJobsInput(profile_keywords="Python, RAG；Agent")

    assert payload.profile_keywords == ["python", "rag", "agent"]
    assert payload.normalization_warnings == [
        "profile_keywords coerced from string to 3 keyword(s)"
    ]


def test_profile_keywords_single_string_becomes_single_keyword() -> None:
    payload = MatchObservedJobsInput(profile_keywords="machine learning")

    assert payload.profile_keywords == ["machine learning"]
    assert payload.normalization_warnings == [
        "profile_keywords coerced from string to 1 keyword(s)"
    ]


def test_profile_keywords_list_shape_is_unchanged() -> None:
    payload = MatchObservedJobsInput(profile_keywords=[" Python ", "python"])

    assert payload.profile_keywords == ["python"]
    assert payload.normalization_warnings == []


def test_legacy_aliases_and_limit_are_normalized_before_tool_validation() -> None:
    payload = MatchObservedJobsInput.model_validate({
        "keywords": "Python、RAG",
        "locations": "上海, 北京",
        "rankingCriteria": "技能, 地点",
        "limit": 500,
    })

    assert payload.profile_keywords == ["python", "rag"]
    assert payload.preferred_locations == ["上海", "北京"]
    assert payload.ranking_criteria == ["skills", "location"]
    assert payload.limit == 100
    assert "limit normalized to fixed business limit 100" in payload.normalization_warnings


# --- input tolerance: ranking_criteria --------------------------------------


def test_ranking_criteria_chinese_alias_maps_to_canonical_enum() -> None:
    payload = MatchObservedJobsInput(
        ranking_criteria=["技能", "薪资待遇", "公司类型", "时效", "地点"]
    )

    assert payload.ranking_criteria == [
        "skills", "salary", "company_type", "recency", "location",
    ]
    assert payload.normalization_warnings == [
        "ranking_criteria '技能' normalized to 'skills'",
        "ranking_criteria '薪资待遇' normalized to 'salary'",
        "ranking_criteria '公司类型' normalized to 'company_type'",
        "ranking_criteria '时效' normalized to 'recency'",
        "ranking_criteria '地点' normalized to 'location'",
    ]


def test_ranking_criteria_case_is_normalized() -> None:
    payload = MatchObservedJobsInput(ranking_criteria=["Salary", "SKILLS"])

    assert payload.ranking_criteria == ["salary", "skills"]
    assert payload.normalization_warnings == [
        "ranking_criteria 'Salary' normalized to 'salary'",
        "ranking_criteria 'SKILLS' normalized to 'skills'",
    ]


def test_ranking_criteria_canonical_values_are_unchanged() -> None:
    payload = MatchObservedJobsInput(
        ranking_criteria=["skills", "location", "salary", "recency", "company_type"]
    )

    assert payload.ranking_criteria == [
        "skills", "location", "salary", "recency", "company_type",
    ]
    assert payload.normalization_warnings == []


def test_unknown_ranking_criterion_still_raises_validation_error() -> None:
    # "综合评分" has no synonym in the existing Literal domain; mapping it to a
    # non-enum value would bypass validation, so it must keep failing.
    with pytest.raises(ValidationError):
        MatchObservedJobsInput(ranking_criteria=["综合评分"])
    with pytest.raises(ValidationError):
        MatchObservedJobsInput(ranking_criteria=["skills", "综合评分"])
    with pytest.raises(ValidationError):
        MatchObservedJobsInput(ranking_criteria=["技能", 123])


# --- semantics of tolerated input vs canonical input ------------------------


def test_tolerated_payload_ranks_identically_to_canonical_equivalent() -> None:
    context = ToolContext(
        user_id="user-a",
        run_id="run-a",
        metadata={
            "observed_public_evidence": [{
                "artifact_id": "artifact-agent",
                "source_url": "https://jobs.example/agent",
                "content_hash": "a" * 64,
                "title": "AI Agent 开发工程师",
                "visible_text": "上海民营公司，月薪 25k-35k，负责 Agent 平台和 Python 开发。",
            }]
        },
    )
    tolerated = MatchObservedJobsInput(
        profile_keywords="Python, Agent",
        preferred_locations=["上海"],
        ranking_criteria=["技能", "地点", "薪资", "公司类型", "时效"],
    )
    canonical = MatchObservedJobsInput(
        profile_keywords=["Python", "Agent"],
        preferred_locations=["上海"],
        ranking_criteria=["skills", "location", "salary", "company_type", "recency"],
    )

    tolerated_result = match_observed_jobs(context, tolerated)
    canonical_result = match_observed_jobs(context, canonical)

    assert tolerated.normalization_warnings
    assert canonical.normalization_warnings == []
    match = tolerated_result.matches[0]
    assert match.matched_keywords == ["python", "agent"]
    assert match.matched_locations == ["上海"]
    assert match.compensation_text == "25k-35k"
    assert match.observed_company_types == ["民营"]
    assert match.unverified_ranking_criteria == ["recency"]
    assert tolerated_result.unresolved_ranking_criteria == ["recency"]
    assert tolerated_result.matches == canonical_result.matches
    assert tolerated_result.unresolved_ranking_criteria == (
        canonical_result.unresolved_ranking_criteria
    )


# --- executor: invalid_tool_input as a stable failure -----------------------


def _matching_task(**updates: Any) -> AgentTaskRequest:
    return AgentTaskRequest(goal="推荐匹配岗位", allowed_skills=["job-matching"], **updates)


def _single_step_plan(task: AgentTaskRequest) -> ExecutionPlan:
    return ExecutionPlan(
        task=task,
        created_by=AgentRole.planner,
        complexity=ComplexityLevel.L2,
        success_criteria=["匹配结果"],
        steps=[PlanStep(step_id="match", objective="匹配", allowed_skills=["job-matching"])],
    )


def _registry_with_match_tool(
    invocations: dict[str, int],
) -> tuple[ToolRegistry, dict[str, list[Any]]]:
    """A real registry with a real ``match-observed-jobs``-shaped tool.

    The handler counts invocations and exposes the validated payloads so tests
    can prove what reached the tool boundary.
    """
    registry = ToolRegistry()
    captured: dict[str, list[Any]] = {"payloads": []}

    def handler(_context: Any, payload: Any) -> dict[str, str]:
        invocations["count"] += 1
        captured["payloads"].append(payload)
        return {"title": "matched"}

    registry.register(ToolDefinition(
        name="match-jobs",
        skill_name="job-matching",
        input_model=MatchObservedJobsInput,
        output_model=MatchOutput,
        allowed_roles=frozenset({AgentRole.executor}),
        handler=handler,
    ))
    return registry, captured


def test_executor_dedups_identical_invalid_tool_input_reissue() -> None:
    """Re-issuing the SAME bad payload is a doomed repeat: deduped, no extra waste.

    Without the stable-failure treatment each identical re-issue would waste
    one more turn and three wastes would end the run as waiting_user. With
    ``invalid_tool_input`` in ``_STABLE_FAILURE_ERROR_CODES`` the first failure
    counts one wasted turn and identical re-issues become duplicate_tool_call.
    """
    invocations = {"count": 0}
    registry, _captured = _registry_with_match_tool(invocations)
    task = _matching_task()
    gateway = ScriptedGateway([
        {"action": "call_tool", "tool_name": "match-jobs",
         "tool_input": {"ranking_criteria": ["综合评分"]}},
        {"action": "call_tool", "tool_name": "match-jobs",
         "tool_input": {"ranking_criteria": ["综合评分"]}},
        {"action": "call_tool", "tool_name": "match-jobs",
         "tool_input": {"ranking_criteria": ["综合评分"]}},
        {"action": "complete", "summary": "匹配完成"},
    ])

    result = ExecutorAgent(gateway=gateway, tools=registry).run(
        task=task, plan=_single_step_plan(task), step=_single_step_plan(task).steps[0],
        context=ToolContext(user_id="user-a", run_id="run-a"),
        tool_budget=ToolCallBudget(4),
    )

    assert result.status == "succeeded"
    assert invocations["count"] == 0
    assert [obs.error_code for obs in result.observations] == [
        "invalid_tool_input", "duplicate_tool_call", "duplicate_tool_call",
    ]
    # The identical re-issues burned no budget/waste; only the first call did.
    assert result.execution_state["total_wasted_turns"] == 1
    assert result.execution_state["consecutive_stalls"] == 2


def test_executor_distinct_invalid_payloads_each_count_one_wasted_turn() -> None:
    """Genuinely NEW bad shapes still burn one wasted turn each (cap backstop).

    Three different invalid payloads produce three wasted turns; the
    total-waste cap (3) still hands the step to the human as the last line of
    defense when tolerance cannot fix the shape.
    """
    invocations = {"count": 0}
    registry, _captured = _registry_with_match_tool(invocations)
    task = _matching_task()
    gateway = ScriptedGateway([
        {"action": "call_tool", "tool_name": "match-jobs",
         "tool_input": {"ranking_criteria": ["综合评分"]}},
        {"action": "call_tool", "tool_name": "match-jobs",
         "tool_input": {"ranking_criteria": ["不存在的标准"]}},
        {"action": "call_tool", "tool_name": "match-jobs",
         "tool_input": {"profile_keywords": [123]}},
    ])

    result = ExecutorAgent(gateway=gateway, tools=registry).run(
        task=task, plan=_single_step_plan(task), step=_single_step_plan(task).steps[0],
        context=ToolContext(user_id="user-a", run_id="run-a"),
        tool_budget=ToolCallBudget(4),
    )

    assert result.status == "needs_user"
    assert invocations["count"] == 0
    assert [obs.error_code for obs in result.observations] == [
        "invalid_tool_input", "invalid_tool_input", "invalid_tool_input",
    ]
    assert "累计" in result.user_question
    assert result.execution_state["total_wasted_turns"] == 3


def test_executor_tolerated_payload_succeeds_and_records_normalization() -> None:
    """The exact model-facing shape that used to trip the waste cap now works.

    String keywords and Chinese criteria are coerced at the tool boundary, the
    tool runs normally, and no turn is wasted.
    """
    invocations = {"count": 0}
    registry, captured = _registry_with_match_tool(invocations)
    task = _matching_task()
    gateway = ScriptedGateway([
        {"action": "call_tool", "tool_name": "match-jobs",
         "tool_input": {"profile_keywords": "Python, RAG", "ranking_criteria": ["技能", "Salary"]}},
        {"action": "complete", "summary": "匹配完成"},
    ])

    result = ExecutorAgent(gateway=gateway, tools=registry).run(
        task=task, plan=_single_step_plan(task), step=_single_step_plan(task).steps[0],
        context=ToolContext(user_id="user-a", run_id="run-a"),
        tool_budget=ToolCallBudget(4),
    )

    assert result.status == "succeeded"
    assert invocations["count"] == 1
    assert [obs.error_code for obs in result.observations] == [None]
    assert result.execution_state["total_wasted_turns"] == 0
    # The lenient conversion reached the tool boundary: the handler received
    # the coerced canonical payload with the conversion audit attached.
    assert captured["payloads"][0].profile_keywords == ["python", "rag"]
    assert captured["payloads"][0].ranking_criteria == ["skills", "salary"]
    assert captured["payloads"][0].normalization_warnings == [
        "profile_keywords coerced from string to 2 keyword(s)",
        "ranking_criteria '技能' normalized to 'skills'",
        "ranking_criteria 'Salary' normalized to 'salary'",
    ]
