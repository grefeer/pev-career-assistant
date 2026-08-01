"""Real public-page evidence tool used by the PEV job-discovery Skill."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.app.services.career_skills.job_discovery import (
    FetchPublicJobPageInput,
    PublicJobFetchError,
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
