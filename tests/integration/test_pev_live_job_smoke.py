"""Opt-in live smoke coverage for public AI-application / Agent job evidence."""

from __future__ import annotations

import os

import pytest

from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.career_skills.job_discovery import (
    ExtractObservedJobDetailsInput,
    FetchPublicJobPageInput,
    extract_observed_job_details,
    fetch_public_job_page,
)


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_PEV_SMOKE") != "1",
    reason="set RUN_LIVE_PEV_SMOKE=1 to run public-network job smoke coverage",
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


@pytest.mark.parametrize("url", OFFICIAL_AI_AGENT_JOB_URLS)
def test_public_official_ai_agent_job_has_traceable_structured_details(url: str) -> None:
    """Each supplied official URL must yield a JD, not a model-invented record."""
    context = ToolContext(user_id="live-smoke", run_id="live-smoke")
    page = fetch_public_job_page(context, FetchPublicJobPageInput(url=url))
    details = extract_observed_job_details(
        ToolContext(
            user_id="live-smoke",
            run_id="live-smoke",
            metadata={"observed_public_evidence": [page.model_dump()]},
        ),
        ExtractObservedJobDetailsInput(artifact_id=page.artifact_id),
    )

    assert len(details.candidates) == 1
    candidate = details.candidates[0]
    assert candidate.title
    assert candidate.responsibilities
    assert candidate.requirements
    assert candidate.evidence_refs == [{
        "artifact_id": page.artifact_id,
        "source_url": url,
        "content_hash": page.content_hash,
    }]
