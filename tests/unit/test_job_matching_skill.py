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
