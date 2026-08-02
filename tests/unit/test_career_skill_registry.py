"""Default ToolRegistry wiring for real PEV business Skills."""

from __future__ import annotations

from backend.app.domain.agent_runtime import AgentRole
from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.career_skills.registry import build_career_tool_registry


def test_registry_exposes_public_job_evidence_tool_to_executor(monkeypatch) -> None:
    """Job discovery is an actual registered tool, not merely a Skill label."""
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery.fetch_public_job_page",
        lambda context, payload: {
            "artifact_id": "observed:" + "a" * 64,
            "source_url": payload.url,
            "title": "AI 应用开发工程师",
            "visible_text": "岗位职责和要求",
            "content_hash": "a" * 64,
        },
    )
    registry = build_career_tool_registry()

    result = registry.invoke(
        role=AgentRole.executor,
        name="fetch-public-job-page",
        context=ToolContext(user_id="user-a", run_id="run-a"),
        payload={"url": "https://jobs.example/1"},
        allowed_skills=frozenset({"job-discovery"}),
    )

    assert result.status == "succeeded"
    assert result.output is not None
    assert result.output["title"] == "AI 应用开发工程师"


def test_registry_exposes_public_web_search_as_part_of_job_discovery_skill() -> None:
    """Executor can discover candidate URLs without promoting search into a fourth Agent."""
    registry = build_career_tool_registry()

    names = {
        tool["name"]
        for tool in registry.tool_catalog(
            role=AgentRole.executor,
            allowed_skills=frozenset({"job-discovery"}),
        )
    }

    assert "search-public-job-pages" in names
    catalog = registry.tool_catalog(
        role=AgentRole.executor,
        allowed_skills=frozenset({"job-discovery", "job-matching"}),
    )
    matching = next(tool for tool in catalog if tool["name"] == "match-observed-jobs")
    assert "推荐任务必须调用" in matching["description"]
    assert "matches" in matching["output_schema"]["properties"]
