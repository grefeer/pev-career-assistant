"""Opt-in real-model evaluation of the natural-language personal-job workflow."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import select

from backend.app.db.models import AgentArtifact, AgentTurn, User, UserRole
from backend.app.domain.agent_runtime import AgentRole, RunStatus
from backend.app.services.agent_runtime.executor_agent import ExecutorAgent
from backend.app.services.agent_runtime.model_gateway import build_agent_model_gateway
from backend.app.services.agent_runtime.planner_agent import PlannerAgent
from backend.app.services.agent_runtime.runtime import AgentRuntime
from backend.app.services.agent_runtime.schemas import AgentBudget, AgentTaskRequest
from backend.app.services.career_skills.manifest import build_career_skill_registry
from backend.app.services.career_skills.registry import build_career_tool_registry
from backend.app.services.profile_parser import (
    extract_evidence_candidates,
    extract_resume_document,
)
from tests.conftest import settings_override


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_PEV_E2E") != "1" or not os.environ.get("LIVE_RESUME_PDF"),
    reason="set RUN_LIVE_PEV_E2E=1 and LIVE_RESUME_PDF for the real-model PEV evaluation",
)


OFFICIAL_AI_AGENT_JOB_URLS = (
    "https://talent.baidu.com/jobs/detail/GRADUATE/4f1cbc80-8332-4a92-b8fa-c0132b17d47e",
    "https://talent.baidu.com/jobs/detail/GRADUATE/74d83772-1bd0-42b9-8cc5-69eb45696b62",
    "https://talent.baidu.com/jobs/detail/SOCIAL/75d3af47-7f79-4d71-862b-6fbca577bb19",
    "https://talent.baidu.com/jobs/detail/GRADUATE/3287bb6a-8c27-4648-a3c2-b3cac16c3d36",
    "https://talent.baidu.com/jobs/detail/GRADUATE/6f9c3a86-6557-409d-8fa7-e6f4c68d6765",
    "https://talent.baidu.com/jobs/detail/SOCIAL/5bb42582-10ab-4f49-94a6-7ee296885d8f",
    "https://talent.baidu.com/jobs/detail/INTERN/cd423c1c-7a35-4672-b0a7-2857308efe43",
)


def test_natural_language_agent_workflow_uses_real_model_and_real_public_jds(db_session) -> None:
    """Evaluate a full request without asserting unavailable date/salary/company facts."""
    resume_path = Path(os.environ["LIVE_RESUME_PDF"])
    parsed_resume = extract_resume_document(resume_path.name, resume_path.read_bytes())
    assert parsed_resume.needs_manual_entry is False
    confirmed_facts = {
        candidate.field_path: candidate.candidate_value
        for candidate in extract_evidence_candidates(parsed_resume.text)
    }
    assert confirmed_facts
    user = User(
        id="live-e2e-user",
        account="live-e2e@example.test",
        nickname="live-e2e",
        password_hash="not-a-real-password-hash",
        role=UserRole.STUDENT,
    )
    db_session.add(user)
    db_session.commit()
    settings = settings_override(agent_harness_enabled=True)
    gateway = build_agent_model_gateway(settings)
    tools = build_career_tool_registry()
    runtime = AgentRuntime(
        planner=PlannerAgent(gateway=gateway, tools=tools),
        executor=ExecutorAgent(gateway=gateway, tools=tools),

        agent_version="pev-live-e2e",
        skills=build_career_skill_registry(tools),
    )
    task = AgentTaskRequest(
        goal=(
            "请基于近三天可公开验证的国企或民营 AI 应用开发、Agent 开发岗位，"
            "给我完整 JD、最优薪资推荐、针对该岗位的简历修改建议。"
            "必须只使用公开证据；页面没有发布日期、公司类型或薪资时明确标注未验证，"
            "不得推断。优先检查候选官方 JD。"
        ),
        allowed_skills=[
            "job-discovery",
            "job-matching",
            "resume-tailoring",
        ],
        context={"candidate_urls": list(OFFICIAL_AI_AGENT_JOB_URLS)},
        private_context={"confirmed_profile_facts": confirmed_facts},
        budget=AgentBudget(
            max_agent_turns=36,
            max_tool_calls=24,
            max_replans=1,
            max_wall_clock_seconds=240,
        ),
    )

    result = runtime.run(db_session, user_id=user.id, task=task)

    turns = list(db_session.scalars(select(AgentTurn).where(AgentTurn.run_id == result.run_id)))
    assert result.status is RunStatus.succeeded, [
        (turn.role.value, turn.decision_json) for turn in turns
    ]
    assert {turn.role for turn in turns} >= {
        AgentRole.planner,
        AgentRole.executor,
        AgentRole.verifier,
    }
    artifacts = list(
        db_session.scalars(select(AgentArtifact).where(AgentArtifact.run_id == result.run_id))
    )
    artifact_types = {artifact.artifact_type for artifact in artifacts}
    assert "public_job_page" in artifact_types
    assert "job_matching_report" in artifact_types
    assert "resume_tailoring_brief" in artifact_types
