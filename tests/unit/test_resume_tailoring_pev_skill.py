"""Evidence and confirmed-fact tests for PEV resume tailoring."""

import pytest
from pydantic import ValidationError

from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.career_skills.resume_tailoring import (
    BuildResumeTailoringBriefInput,
    ResumeTailoringError,
    _find_fact_ref_for_keyword,
    _find_target,
    _flatten_text,
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
    assert [item.model_dump() for item in result.proposed_diffs] == [
        {
            "op": "highlight",
            "section": "skills",
            "fact_ref": "skills",
            "target_evidence_ref": "artifact-agent",
            "change_summary": "将已确认的 Python 事实前置到技能部分，并保留原有可核验表述。",
        },
        {
            "op": "highlight",
            "section": "skills",
            "fact_ref": "skills",
            "target_evidence_ref": "artifact-agent",
            "change_summary": "将已确认的 RAG 事实前置到技能部分，并保留原有可核验表述。",
        },
    ]


def test_resume_brief_accepts_the_observed_page_artifact_identifier() -> None:
    """A FetchPublicJobPageOutput identifier must be usable by this Skill."""
    artifact_id = f"observed:{'a' * 64}"
    context = ToolContext(
        user_id="user-a",
        run_id="run-a",
        metadata={
            "observed_public_evidence": [{
                "artifact_id": artifact_id,
                "source_url": "https://jobs.example/agent",
                "content_hash": "a" * 64,
                "visible_text": "岗位要求 Python。",
            }],
            "confirmed_profile_facts": {"skills": ["Python"]},
        },
    )

    result = build_resume_tailoring_brief(
        context,
        BuildResumeTailoringBriefInput(
            target_artifact_id=artifact_id,
            target_keywords=["Python"],
        ),
    )

    assert result.target_artifact_id == artifact_id


def test_resume_brief_rejects_missing_or_incomplete_evidence_and_empty_keywords() -> None:
    with pytest.raises(ValidationError):
        BuildResumeTailoringBriefInput(target_artifact_id="jd", target_keywords=[" "])
    no_target = ToolContext(user_id="user-a", run_id="run-a", metadata={"observed_public_evidence": []})
    with pytest.raises(ResumeTailoringError, match="target_evidence_not_found"):
        build_resume_tailoring_brief(
            no_target,
            BuildResumeTailoringBriefInput(target_artifact_id="jd", target_keywords=["Python"]),
        )
    incomplete = ToolContext(
        user_id="user-a", run_id="run-a",
        metadata={"observed_public_evidence": [{"artifact_id": "jd", "source_url": "https://jobs.example"}]},
    )
    with pytest.raises(ResumeTailoringError, match="target_evidence_incomplete"):
        build_resume_tailoring_brief(
            incomplete,
            BuildResumeTailoringBriefInput(target_artifact_id="jd", target_keywords=["Python"]),
        )


def test_resume_brief_maps_project_and_general_facts_to_reviewable_sections() -> None:
    context = ToolContext(
        user_id="user-a", run_id="run-a",
        metadata={
            "observed_public_evidence": [{
                "artifact_id": "jd", "source_url": "https://jobs.example",
                "title": 9, "visible_text": "需要 Docker、沟通能力。",
            }],
            "confirmed_profile_facts": {"projects": ["Docker 部署"], "basics": "沟通能力"},
        },
    )

    result = build_resume_tailoring_brief(
        context,
        BuildResumeTailoringBriefInput(
            target_artifact_id="jd", target_keywords=[" Docker ", "docker", "沟通"],
        ),
    )

    assert result.target_title is None
    assert result.supported_keywords == ["docker", "沟通"]
    assert [(diff.fact_ref, diff.section) for diff in result.proposed_diffs] == [
        ("projects", "projects"), ("basics", "summary"),
    ]


def test_resume_tailoring_fact_helpers_only_use_dict_backed_confirmed_facts() -> None:
    assert _find_target("not-a-list", "jd") is None
    assert _flatten_text(("Python", {"nested": [1, None, "RAG"]})) == "Python\n\n\nRAG"
    assert _find_fact_ref_for_keyword(["Python"], "python") is None
    assert _find_fact_ref_for_keyword({"skills": ["Python"]}, "rag") is None
    # A non-matching dict evidence item is skipped before the match is found.
    other = {"artifact_id": "other", "source_url": "https://jobs.example/other"}
    target = {"artifact_id": "jd", "source_url": "https://jobs.example"}
    assert _find_target([other, target], "jd") is target


def test_resume_brief_emits_only_missing_gap_when_no_keyword_is_supported_by_facts() -> None:
    """Every JD keyword absent from confirmed facts yields a gap, no highlight."""
    context = ToolContext(
        user_id="user-a",
        run_id="run-a",
        metadata={
            "observed_public_evidence": [{
                "artifact_id": "jd",
                "source_url": "https://jobs.example",
                "content_hash": "a" * 64,
                "visible_text": "岗位要求 Python 与 RAG 开发经验。",
            }],
            "confirmed_profile_facts": {},
        },
    )

    result = build_resume_tailoring_brief(
        context,
        BuildResumeTailoringBriefInput(
            target_artifact_id="jd", target_keywords=["Python", "RAG"],
        ),
    )

    assert result.supported_keywords == []
    assert result.missing_keywords == ["python", "rag"]
    assert result.proposed_diffs == []
    assert result.safe_actions == [
        "Python、RAG 尚无已确认事实：仅在能补充项目证据时添加，不得虚构。",
    ]


def test_resume_brief_accepts_aigc_synonyms_and_preserves_liepin_attribution() -> None:
    """A named-source mirror stays valid only with its exact captured attribution marker."""
    context = ToolContext(
        user_id="user-a",
        run_id="run-a",
        metadata={
            "task_goal": "在猎聘网找北京的 AIGC 产品经理（应届生）岗位，并定制简历。",
            "observed_public_evidence": [{
                "artifact_id": "linkedin-liepin-jd",
                "source_url": "https://cn.linkedin.com/jobs/view/ai-product-manager-123",
                "content_hash": "f" * 64,
                "title": "AI产品经理实习生",
                "visible_text": (
                    "AI产品经理实习生 北京市 该职位来源于猎聘 "
                    "参与大模型、Agent、Prompt 和 RAG 产品设计；本科生可申请。"
                ),
            }],
            "confirmed_profile_facts": {"skills": ["Prompt", "RAG"]},
        },
    )

    result = build_resume_tailoring_brief(
        context,
        BuildResumeTailoringBriefInput(
            target_artifact_id="linkedin-liepin-jd",
            target_keywords=["Prompt", "RAG"],
        ),
    )

    assert result.target_title == "AI产品经理实习生"
    assert result.source_url == "https://cn.linkedin.com/jobs/view/ai-product-manager-123"
    assert result.source_attribution == "猎聘"
    assert result.supported_keywords == ["prompt", "rag"]


def test_resume_brief_rejects_named_source_mirror_without_attribution_marker() -> None:
    context = ToolContext(
        user_id="user-a",
        run_id="run-a",
        metadata={
            "task_goal": "在猎聘网找北京的 AIGC 产品经理实习岗位，并定制简历。",
            "observed_public_evidence": [{
                "artifact_id": "linkedin-unattributed",
                "source_url": "https://cn.linkedin.com/jobs/view/ai-product-manager-456",
                "content_hash": "e" * 64,
                "title": "AI产品经理实习生",
                "visible_text": "北京 大模型 Agent 产品设计，实习生可申请。",
            }],
            "confirmed_profile_facts": {"skills": ["Agent"]},
        },
    )

    with pytest.raises(ResumeTailoringError, match="target_source_mismatch"):
        build_resume_tailoring_brief(
            context,
            BuildResumeTailoringBriefInput(
                target_artifact_id="linkedin-unattributed",
                target_keywords=["Agent"],
            ),
        )
