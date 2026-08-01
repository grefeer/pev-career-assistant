"""Evidence and confirmed-fact tests for PEV resume tailoring."""

from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.career_skills.resume_tailoring import (
    BuildResumeTailoringBriefInput,
    build_resume_tailoring_brief,
)


def test_resume_brief_never_recommends_claiming_a_jd_keyword_absent_from_confirmed_facts() -> None:
    """Removing a confirmed skill must turn it into a gap, not a fabricated rewrite."""
    context = ToolContext(
        user_id="user-a",
        run_id="run-a",
        metadata={
            "observed_public_evidence": [{
                "artifact_id": "artifact-agent",
                "source_url": "https://jobs.example/agent",
                "content_hash": "a" * 64,
                "title": "AI Agent 开发工程师",
                "visible_text": "要求 Python、RAG、LangGraph 和 Agent 开发经验。",
            }],
            "confirmed_profile_facts": {
                "skills": ["Python", "RAG"],
                "project": "基于 RAG 的智能问答系统",
            },
        },
    )

    result = build_resume_tailoring_brief(
        context,
        BuildResumeTailoringBriefInput(
            target_artifact_id="artifact-agent",
            target_keywords=["Python", "RAG", "LangGraph", "Agent"],
        ),
    )

    assert result.target_artifact_id == "artifact-agent"
    assert result.supported_keywords == ["python", "rag"]
    assert result.missing_keywords == ["langgraph", "agent"]
    assert result.safe_actions == [
        "在项目经历中优先展示已确认的 Python、RAG 事实，并量化可核验结果。",
        "LangGraph、Agent 尚无已确认事实：仅在能补充项目证据时添加，不得虚构。",
    ]
