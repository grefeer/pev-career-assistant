"""Behavior tests for evidence-bound PEV job matching."""

from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.career_skills.job_matching import (
    MatchObservedJobsInput,
    match_observed_jobs,
)


def test_match_observed_jobs_ranks_only_context_evidence_by_confirmed_keywords() -> None:
    """Removing evidence or keyword hits must change the user-visible ranking."""
    context = ToolContext(
        user_id="user-a",
        run_id="run-a",
        metadata={
            "observed_public_evidence": [
                {
                    "artifact_id": "artifact-agent",
                    "source_url": "https://jobs.example/agent",
                    "content_hash": "a" * 64,
                    "title": "AI Agent 开发工程师",
                    "visible_text": "负责 RAG、Agent 平台和 Python 服务开发。",
                },
                {
                    "artifact_id": "artifact-sales",
                    "source_url": "https://jobs.example/sales",
                    "content_hash": "b" * 64,
                    "title": "销售运营专员",
                    "visible_text": "负责销售数据和客户运营。",
                },
            ]
        },
    )

    result = match_observed_jobs(
        context,
        MatchObservedJobsInput(profile_keywords=["Python", "RAG", "Agent"]),
    )

    assert [(match.artifact_id, match.score, match.matched_keywords) for match in result.matches] == [
        ("artifact-agent", 100, ["python", "rag", "agent"]),
        ("artifact-sales", 0, []),
    ]
    assert all(match.artifact_id.startswith("artifact-") for match in result.matches)


def test_match_reports_missing_salary_and_company_type_as_unverified_not_inferred() -> None:
    context = ToolContext(
        user_id="user-a",
        run_id="run-a",
        metadata={
            "observed_public_evidence": [{
                "artifact_id": "artifact-agent",
                "source_url": "https://jobs.example/agent",
                "content_hash": "a" * 64,
                "title": "AI Agent 开发工程师",
                "visible_text": "北京岗位，负责 Agent 平台和 Python 服务开发。",
            }]
        },
    )

    result = match_observed_jobs(
        context,
        MatchObservedJobsInput(
            profile_keywords=["Python", "Agent"],
            preferred_locations=["北京"],
            ranking_criteria=["skills", "location", "salary", "company_type"],
        ),
    )

    assert result.matches[0].matched_locations == ["北京"]
    assert result.matches[0].compensation_text is None
    assert result.matches[0].unverified_ranking_criteria == ["salary", "company_type"]
    assert result.unresolved_ranking_criteria == ["salary", "company_type"]


def test_match_uses_only_explicit_salary_and_company_type_evidence() -> None:
    context = ToolContext(
        user_id="user-a",
        run_id="run-a",
        metadata={
            "observed_public_evidence": [
                None,
                {"artifact_id": "missing-fields"},
                {
                    "artifact_id": "agent", "source_url": "https://jobs.example/agent",
                    "visible_text": "上海民营企业，月薪 25k-35k，负责 Python Agent。",
                },
            ]
        },
    )

    result = match_observed_jobs(
        context,
        MatchObservedJobsInput(
            profile_keywords=[" Python ", "python"], preferred_locations=[" 上海 ", "上海"],
            ranking_criteria=["salary", "company_type", "recency", "salary"], limit=1,
        ),
    )

    assert result.matches[0].compensation_text == "25k-35k"
    assert result.matches[0].observed_company_types == ["民营"]
    assert result.matches[0].matched_locations == ["上海"]
    assert result.matches[0].unverified_ranking_criteria == ["recency"]
    assert result.unresolved_ranking_criteria == ["recency"]


def test_match_rejects_non_list_evidence_without_inventing_results() -> None:
    result = match_observed_jobs(
        ToolContext(user_id="user-a", run_id="run-a", metadata={"observed_public_evidence": "bad"}),
        MatchObservedJobsInput(ranking_criteria=["recency"]),
    )

    assert result.matches == []
    assert result.unresolved_ranking_criteria == []


def test_match_marks_an_unmatched_requested_location_as_unverified() -> None:
    context = ToolContext(
        user_id="user-a", run_id="run-a",
        metadata={"observed_public_evidence": [{
            "artifact_id": "agent", "source_url": "https://jobs.example/agent",
            "visible_text": "上海岗位，负责 Agent 开发。",
        }]},
    )

    result = match_observed_jobs(
        context,
        MatchObservedJobsInput(preferred_locations=["北京"], ranking_criteria=["location"]),
    )

    assert result.matches[0].unverified_ranking_criteria == ["location"]
