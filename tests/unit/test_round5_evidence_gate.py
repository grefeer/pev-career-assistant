"""Round 5 regression tests for evidence-backed completion honesty."""

from backend.app.services.agent_runtime.evidence_gate import completion_evidence_gate
from backend.app.services.agent_runtime.schemas import PlanStep, ToolObservation


def test_completion_gate_rejects_a_job_discovery_summary_without_evidence() -> None:
    step = PlanStep(
        step_id="discover",
        objective="抓取一个可核验的公开岗位页面",
        allowed_skills=["job-discovery"],
    )

    assert not completion_evidence_gate(
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


def test_completion_gate_accepts_a_verified_empty_search_result() -> None:
    step = PlanStep(
        step_id="discover",
        objective="查询公开岗位",
        allowed_skills=["job-discovery"],
    )

    assert completion_evidence_gate(
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

