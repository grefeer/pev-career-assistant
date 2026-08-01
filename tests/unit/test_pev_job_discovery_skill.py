"""Real public-page evidence tool used by the PEV job-discovery Skill."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.app.services.career_skills.job_discovery import (
    ExtractObservedJobDetailsInput,
    FetchPublicJobPageInput,
    PublicJobFetchError,
    extract_observed_job_details,
    fetch_public_job_page,
)
from backend.app.services.agent_runtime.tool_context import ToolContext


def test_fetch_public_job_page_returns_hashable_visible_evidence(monkeypatch) -> None:
    """Executor receives source-backed text, not a model-generated JD claim."""
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery._assert_public_url",
        lambda url: None,
    )
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery.requests.get",
        lambda *args, **kwargs: SimpleNamespace(
            text="<html><title>AI Agent 开发工程师</title><body><h1>AI Agent 开发工程师</h1><p>职责：构建智能体。</p></body></html>",
            encoding="utf-8",
            apparent_encoding="utf-8",
            raise_for_status=lambda: None,
        ),
    )

    result = fetch_public_job_page(
        ToolContext(user_id="user-a", run_id="run-a"),
        FetchPublicJobPageInput(url="https://jobs.example/ai-agent"),
    )

    assert result.source_url == "https://jobs.example/ai-agent"
    assert result.title == "AI Agent 开发工程师"
    assert "职责：构建智能体。" in result.visible_text
    assert len(result.content_hash) == 64


def test_fetch_public_job_page_rejects_loopback_before_network_access() -> None:
    """An Agent cannot use a public-web tool to probe private infrastructure."""
    with pytest.raises(PublicJobFetchError, match="unsafe_public_url"):
        fetch_public_job_page(
            ToolContext(user_id="user-a", run_id="run-a"),
            FetchPublicJobPageInput(url="http://127.0.0.1:8000/private"),
        )


def test_extract_observed_job_details_returns_structured_fields_only_from_captured_evidence() -> None:
    """Detailed JD output must be derived from the selected immutable page evidence."""
    context = ToolContext(
        user_id="user-a",
        run_id="run-a",
        metadata={"observed_public_evidence": [{
            "artifact_id": "artifact-ai-agent",
            "source_url": "https://jobs.example/ai-agent",
            "content_hash": "a" * 64,
            "title": "招聘详情",
            "visible_text": """
岗位名称：AI Agent 开发工程师
公司：示例科技
岗位职责：负责 RAG、Agent 平台和工具调用能力开发。
任职要求：熟悉 Python、LLM 和工程化部署。
工作地点：北京
""",
        }]},
    )

    result = extract_observed_job_details(
        context,
        ExtractObservedJobDetailsInput(artifact_id="artifact-ai-agent"),
    )

    assert result.source_artifact_id == "artifact-ai-agent"
    assert [candidate.model_dump() for candidate in result.candidates] == [{
        "title": "AI Agent 开发工程师",
        "company_name": "示例科技",
        "locations": ["北京"],
        "responsibilities": "负责 RAG、Agent 平台和工具调用能力开发。",
        "requirements": "熟悉 Python、LLM 和工程化部署。",
        "recruitment_types": [],
        "apply_url": "https://jobs.example/ai-agent",
        "deadline_text": None,
        "confidence": 1.0,
        "evidence_refs": [{
            "artifact_id": "artifact-ai-agent",
            "source_url": "https://jobs.example/ai-agent",
            "content_hash": "a" * 64,
        }],
        "normalization_warnings": [],
    }]


def test_extract_observed_job_details_handles_official_page_without_labeled_title() -> None:
    """A navigation button must not replace the true title on common official career pages."""
    context = ToolContext(
        user_id="user-a",
        run_id="run-a",
        metadata={"observed_public_evidence": [{
            "artifact_id": "artifact-official",
            "source_url": "https://jobs.example/official",
            "content_hash": "b" * 64,
            "title": "官方招聘",
            "visible_text": """
首页
职位
2027AIDU-智能体算法工程师(J99969)
北京市
技术
工作职责：
负责 AI Agent 的设计与研发。
职责要求：
熟悉 Python、RAG 和 Agent 开发框架。
申请职位
""",
        }]},
    )

    result = extract_observed_job_details(
        context,
        ExtractObservedJobDetailsInput(artifact_id="artifact-official"),
    )

    candidate = result.candidates[0]
    assert candidate.title == "2027AIDU-智能体算法工程师(J99969)"
    assert candidate.locations == ["北京市"]
    assert candidate.responsibilities == "负责 AI Agent 的设计与研发。"
    assert candidate.requirements == "熟悉 Python、RAG 和 Agent 开发框架。"


def test_extract_observed_job_details_derives_social_type_and_clears_resolved_location_warning() -> None:
    """Source path and recovered location must correct, not contradict, legacy heuristics."""
    context = ToolContext(
        user_id="user-a",
        run_id="run-a",
        metadata={"observed_public_evidence": [{
            "artifact_id": "artifact-social",
            "source_url": "https://talent.example/jobs/detail/SOCIAL/abc",
            "content_hash": "c" * 64,
            "title": "官方招聘",
            "visible_text": """
Agent研发工程师
北京市
工作职责：负责 Agent 系统研发。
职责要求：熟悉 Python。
申请职位
""",
        }]},
    )

    result = extract_observed_job_details(
        context,
        ExtractObservedJobDetailsInput(artifact_id="artifact-social"),
    )

    candidate = result.candidates[0]
    assert candidate.recruitment_types == ["social"]
    assert "No location information found" not in candidate.normalization_warnings
