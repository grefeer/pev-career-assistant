"""JD-grounded preparation plan behavior for the PEV career-planning Skill."""

from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.career_skills.career_planning import (
    BuildPreparationPlanInput,
    build_preparation_plan,
)


def test_preparation_plan_uses_only_topics_present_in_the_selected_jd() -> None:
    """A topic removed from public JD evidence cannot remain in the plan."""
    context = ToolContext(
        user_id="user-a",
        run_id="run-a",
        metadata={"observed_public_evidence": [{
            "artifact_id": "artifact-agent",
            "source_url": "https://jobs.example/agent",
            "content_hash": "a" * 64,
            "title": "AI Agent 开发工程师",
            "visible_text": "岗位要求 Python、RAG、Agent 工程化和 LLM 应用开发能力。",
        }]},
    )

    result = build_preparation_plan(
        context,
        BuildPreparationPlanInput(
            target_artifact_id="artifact-agent",
            focus_keywords=["Python", "RAG", "Agent", "Kubernetes"],
        ),
    )

    assert result.jd_topics == ["python", "rag", "agent"]
    assert result.actions == [
        "为 Python、RAG、Agent 各准备一个可量化的项目案例，并标明你的具体贡献。",
        "围绕 JD 中的 Python、RAG、Agent 做一次 30 分钟技术讲解演练，准备架构取舍与故障排查追问。",
    ]
    assert [item.model_dump() for item in result.plan_items] == [
        {
            "topic": "python",
            "priority": "P0",
            "time_budget_hours": 2,
            "completion_criteria": "准备一个 Python 相关项目案例，说明你的具体贡献和可核验结果。",
            "review_checkpoint": "完成后用 JD 的 Python 要求复盘：案例是否覆盖职责、取舍和追问。",
        },
        {
            "topic": "rag",
            "priority": "P1",
            "time_budget_hours": 2,
            "completion_criteria": "准备一个 RAG 相关项目案例，说明你的具体贡献和可核验结果。",
            "review_checkpoint": "完成后用 JD 的 RAG 要求复盘：案例是否覆盖职责、取舍和追问。",
        },
        {
            "topic": "agent",
            "priority": "P1",
            "time_budget_hours": 2,
            "completion_criteria": "准备一个 Agent 相关项目案例，说明你的具体贡献和可核验结果。",
            "review_checkpoint": "完成后用 JD 的 Agent 要求复盘：案例是否覆盖职责、取舍和追问。",
        },
    ]


def test_preparation_plan_accepts_the_observed_page_artifact_identifier() -> None:
    """A FetchPublicJobPageOutput identifier must be usable by this Skill."""
    artifact_id = f"observed:{'a' * 64}"
    context = ToolContext(
        user_id="user-a",
        run_id="run-a",
        metadata={"observed_public_evidence": [{
            "artifact_id": artifact_id,
            "source_url": "https://jobs.example/agent",
            "content_hash": "a" * 64,
            "visible_text": "岗位要求 Python。",
        }]},
    )

    result = build_preparation_plan(
        context,
        BuildPreparationPlanInput(
            target_artifact_id=artifact_id,
            focus_keywords=["Python"],
        ),
    )

    assert result.target_artifact_id == artifact_id
