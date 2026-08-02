"""Live-LLM smoke test for the interview-prep SKILL (script-based runtime).

Distinct from ``test_interview_prep_smoke`` (which exercises the backend
``LLMInterviewPrepGenerator`` directly): this runs the full
``InterviewPrepRuntime`` path - the per-task cloned skill, the allowlisted
``run_skill_script`` tool, the real ``generate.py`` subprocess calling the
DeepSeek LLM. It is the "does the skill actually land end-to-end" check that the
mocked unit tests cannot cover.

Opt-in: set ``RUN_INTERVIEW_PREP_SKILL_SMOKE=1`` and have a ``DEEPSEEK_API_KEY``
available (env or Windows User scope). Skipped otherwise so the default suite
needs no network or credentials.
"""

from __future__ import annotations

import os

import pytest

from backend.app.services.interview_prep.runtime import InterviewPrepRuntime
from tests.conftest import settings_override

_RUN = os.environ.get("RUN_INTERVIEW_PREP_SKILL_SMOKE") == "1"
_HAS_KEY = bool(
    os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENAI_API_KEY")
)

pytestmark = pytest.mark.skipif(
    not (_RUN and _HAS_KEY),
    reason="set RUN_INTERVIEW_PREP_SKILL_SMOKE=1 and DEEPSEEK_API_KEY to run this live skill smoke",
)

_CONTENT_KEYS = (
    "technical_questions",
    "behavioral_questions",
    "talking_points",
    "topics_to_review",
    "questions_to_ask",
)

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


def test_skill_runtime_produces_well_formed_prep_kit_against_real_llm(tmp_path):
    """The runtime's generate path produces a non-empty, well-formed prep kit."""
    settings = settings_override(interview_prep_model="deepseek-v4-flash")
    runtime = InterviewPrepRuntime(settings, artifact_root=tmp_path)

    result = runtime.run(
        report_id="smoke-1",
        job_snapshot=_JOB,
        profile_facts=_FACTS,
        preferences=_PREFS,
        match_analysis=_MATCH,
    )

    # Generation success is the only success path; a generation crash is a real
    # failure. An artifact-publish failure would surface as needs_manual_review
    # with the content still preserved - also acceptable for the smoke check.
    assert result.status in {"succeeded", "needs_manual_review"}, (
        f"expected succeeded/needs_manual_review, got {result.status}: {result.last_error}"
    )
    assert result.agent_version
    assert result.content, "expected a non-empty prep kit"
    assert set(result.content.keys()) == set(_CONTENT_KEYS)

    # At least one section must carry real study material.
    assert any(result.content.values()), f"all sections empty: {result.content!r}"
    for key in _CONTENT_KEYS:
        assert isinstance(result.content[key], list)
        assert all(isinstance(item, str) for item in result.content[key]), (
            f"non-string item in {key}: {result.content[key]!r}"
        )

    # The generated kit must be published as auditable evidence.
    assert result.evidence_refs
    assert result.evidence_refs[0]["evidence_type"] == "interview_prep_kit"
