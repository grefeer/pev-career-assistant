"""Round 5 regression tests for evidence-backed completion honesty."""

from backend.app.services.agent_runtime.schemas import PlanStep, ToolObservation
from backend.app.services.career_skills.manifest import build_career_skill_registry
from backend.app.services.career_skills.registry import build_career_tool_registry


_REGISTRY = build_career_skill_registry(build_career_tool_registry())


def test_completion_gate_rejects_a_job_discovery_summary_without_evidence() -> None:
    step = PlanStep(
        step_id="discover",
        objective="抓取一个可核验的公开岗位页面",
        allowed_skills=["job-discovery"],
    )

    assert not _REGISTRY.completion_evidence_gate(
        step,
        [
            ToolObservation(
                tool_name="fetch-public-job-pages",
                status="failed",
                error_code="public_page_content_insufficient",
            )
        ],
        summary="未找到可核验岗位页面，请提供链接。",
    )


def test_completion_gate_rejects_a_search_index_without_a_job_page() -> None:
    step = PlanStep(
        step_id="discover",
        objective="查询公开岗位",
        allowed_skills=["job-discovery"],
    )

    assert not _REGISTRY.completion_evidence_gate(
        step,
        [
            ToolObservation(
                tool_name="search-public-job-pages",
                status="succeeded",
                output={"query": "不存在的岗位", "results": []},
            )
        ],
        summary="公开检索已执行，未返回匹配岗位。",
    )

