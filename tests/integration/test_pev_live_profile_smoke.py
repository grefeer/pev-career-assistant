"""Opt-in, local-only smoke coverage for grounded profile tailoring."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.career_skills.career_planning import (
    BuildPreparationPlanInput,
    build_preparation_plan,
)
from backend.app.services.career_skills.job_discovery import (
    FetchPublicJobPageInput,
    fetch_public_job_page,
)
from backend.app.services.career_skills.resume_tailoring import (
    BuildResumeTailoringBriefInput,
    build_resume_tailoring_brief,
)
from backend.app.services.profile_parser import (
    extract_evidence_candidates,
    extract_resume_document,
)


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_PEV_SMOKE") != "1" or not os.environ.get("LIVE_RESUME_PDF"),
    reason="set RUN_LIVE_PEV_SMOKE=1 and LIVE_RESUME_PDF to run local-profile smoke coverage",
)


OFFICIAL_AI_AGENT_JOB_URLS = (
    "https://talent.baidu.com/jobs/detail/GRADUATE/4f1cbc80-8332-4a92-b8fa-c0132b17d47e",
    "https://talent.baidu.com/jobs/detail/GRADUATE/74d83772-1bd0-42b9-8cc5-69eb45696b62",
    "https://talent.baidu.com/jobs/detail/GRADUATE/6f9c3a86-6557-409d-8fa7-e6f4c68d6765",
)


def test_local_resume_has_only_fact_grounded_tailoring_for_three_real_jds() -> None:
    """The local PDF is read in-memory and never copied into the repository."""
    resume_path = Path(os.environ["LIVE_RESUME_PDF"])
    parsed = extract_resume_document(resume_path.name, resume_path.read_bytes())
    assert parsed.needs_manual_entry is False
    candidates = extract_evidence_candidates(parsed.text)
    confirmed_facts = {
        candidate.field_path: candidate.candidate_value for candidate in candidates
    }
    assert confirmed_facts

    pages = [
        fetch_public_job_page(
            ToolContext(user_id="live-smoke", run_id="live-profile-smoke"),
            FetchPublicJobPageInput(url=url),
        )
        for url in OFFICIAL_AI_AGENT_JOB_URLS
    ]
    observed_evidence = [page.model_dump() for page in pages]
    context = ToolContext(
        user_id="live-smoke",
        run_id="live-profile-smoke",
        metadata={
            "confirmed_profile_facts": confirmed_facts,
            "observed_public_evidence": observed_evidence,
        },
    )

    for page in pages:
        tailoring = build_resume_tailoring_brief(
            context,
            BuildResumeTailoringBriefInput(
                target_artifact_id=page.artifact_id,
                    target_keywords=["Agent", "Python", "LLM", "RAG", "FastAPI"],
            ),
        )
        plan = build_preparation_plan(
            context,
            BuildPreparationPlanInput(
                target_artifact_id=page.artifact_id,
                    focus_keywords=["Agent", "Python", "LLM", "RAG", "FastAPI"],
                time_budget_hours=6,
            ),
        )

        assert tailoring.source_url == page.source_url
        assert all(diff.fact_ref in confirmed_facts for diff in tailoring.proposed_diffs)
        assert all(diff.target_evidence_ref == page.artifact_id for diff in tailoring.proposed_diffs)
        if tailoring.missing_keywords:
            assert any("不得虚构" in action for action in tailoring.safe_actions)
        assert plan.source_url == page.source_url
        assert plan.plan_items
        assert sum(item.time_budget_hours for item in plan.plan_items) == 6
        assert all(item.review_checkpoint for item in plan.plan_items)
