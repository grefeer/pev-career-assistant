"""Live-LLM smoke test for the resume-tailoring SKILL (script-based runtime).

Distinct from ``test_resume_tailoring_smoke`` (which exercises the backend
``LLMDraftGenerator`` directly): this runs the full ``ResumeTailoringRuntime``
path - the per-task cloned skill, the allowlisted ``run_skill_script`` tool, the
real ``generate.py`` subprocess calling the DeepSeek LLM, and the ``validate.py``
grounding step. It is the "does the skill actually land end-to-end" check that
the mocked unit tests cannot cover.

Opt-in: set ``RUN_RESUME_TAILORING_SKILL_SMOKE=1`` and have a
``DEEPSEEK_API_KEY`` available (env or Windows User scope). Skipped otherwise so
the default suite needs no network or credentials.
"""

from __future__ import annotations

import os

import pytest

from backend.app.services.resume_tailoring.runtime import ResumeTailoringRuntime
from tests.conftest import settings_override

_RUN = os.environ.get("RUN_RESUME_TAILORING_SKILL_SMOKE") == "1"
_HAS_KEY = bool(
    os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENAI_API_KEY")
)

pytestmark = pytest.mark.skipif(
    not (_RUN and _HAS_KEY),
    reason="set RUN_RESUME_TAILORING_SKILL_SMOKE=1 and DEEPSEEK_API_KEY to run this live skill smoke",
)

_VALID_OPS = {"reorder", "rephrase", "summarize", "omit", "highlight"}

_JOB = {
    "title": "Backend Engineer (Go)",
    "company": "Acme Corp",
    "location": "Beijing",
    "requirements": ["3+ years Go", "microservices", "Kubernetes"],
    "responsibilities": ["design scalable APIs", "own service reliability"],
}
_FACTS = {
    "exp_api": {"role": "Backend Engineer", "summary": "Built Python/FastAPI microservices"},
    "exp_platform": {"role": "Platform Engineer", "summary": "Operated Kubernetes clusters and CI/CD"},
    "skills": {"languages": ["Python", "Go (learning)"], "tools": ["Kubernetes", "Docker"]},
}
_PREFS = {"desired_roles": ["Backend Engineer"], "target_cities": ["Beijing"]}
_MATCH = {
    "strengths": [{"area": "Python microservices"}],
    "gaps": [{"area": "production Go experience"}],
}
# Map each fact key to example evidence ids so validate.py can ground evidence_ids.
_EVIDENCE = {
    "exp_api": ["ev_api_1"],
    "exp_platform": ["ev_platform_1"],
    "skills": ["ev_skills_1"],
}


def test_skill_runtime_produces_well_formed_diffs_against_real_llm(tmp_path):
    """The runtime's generate+validate path produces non-empty, fact-grounded diffs."""
    settings = settings_override(resume_tailoring_model="deepseek-v4-flash")
    runtime = ResumeTailoringRuntime(settings, artifact_root=tmp_path)

    result = runtime.run(
        report_id="smoke-1",
        job_snapshot=_JOB,
        profile_facts=_FACTS,
        preferences=_PREFS,
        match_analysis=_MATCH,
        evidence_refs=_EVIDENCE,
        validate=True,
    )

    # A generation crash is a real failure; a grounding miss is tolerable
    # (the human can fix the diffs in place) but the diffs must still be present.
    assert result.status in {"succeeded", "needs_manual_review"}, (
        f"expected succeeded/needs_manual_review, got {result.status}: {result.last_error}"
    )
    assert result.diffs, f"expected >=1 diff, got {result.diffs!r}"
    assert result.agent_version

    for diff in result.diffs:
        assert diff.get("op") in _VALID_OPS, f"bad op: {diff!r}"
        assert diff.get("section"), f"empty section: {diff!r}"
        assert diff.get("fact_ref") in _FACTS, f"unknown fact_ref: {diff!r}"

    # The generated draft must be published as auditable evidence.
    assert result.evidence_refs
    assert result.evidence_refs[0]["evidence_type"] == "resume_draft_diffs"
