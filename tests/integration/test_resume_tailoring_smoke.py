"""Live-LLM smoke test for the Resume Tailoring skill.

Opt-in: set ``RUN_RESUME_TAILORING_SMOKE=1`` and have a ``DEEPSEEK_API_KEY``
available. Exercises the real ``LLMDraftGenerator`` against the DeepSeek API to
confirm the skill produces well-formed, fact-referencing diff operations - the
"does it actually land" check that unit tests (which mock the LLM) cannot cover.

Skipped otherwise so the default suite needs no network or credentials.
"""

from __future__ import annotations

import os

import pytest

from backend.app.services.resume_tailoring.generator import LLMDraftGenerator
from backend.app.services.resume_tailoring.llm_factory import build_draft_generator_llm
from tests.conftest import settings_override

_RUN = os.environ.get("RUN_RESUME_TAILORING_SMOKE") == "1"
_HAS_KEY = bool(
    os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENAI_API_KEY")
)

pytestmark = pytest.mark.skipif(
    not (_RUN and _HAS_KEY),
    reason="set RUN_RESUME_TAILORING_SMOKE=1 and DEEPSEEK_API_KEY to run this live smoke",
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


def test_generator_produces_valid_diffs_against_real_llm():
    """The real DeepSeek LLM returns a non-empty, well-formed diffs list."""
    settings = settings_override(resume_tailoring_model="deepseek-v4-flash")
    llm = build_draft_generator_llm(settings)
    generator = LLMDraftGenerator(llm, settings)

    result = generator.generate_diffs(
        job_snapshot=_JOB,
        profile_facts=_FACTS,
        preferences=_PREFS,
        match_analysis=_MATCH,
    )

    diffs = result["diffs"]
    assert isinstance(diffs, list)
    assert result["agent_version"] == settings.resume_tailoring_agent_version
    # A useful tailoring should propose at least one operation.
    assert len(diffs) >= 1, f"expected >=1 diff, got {diffs!r}"

    for diff in diffs:
        assert diff.get("op") in _VALID_OPS, f"bad op: {diff!r}"
        assert diff.get("section"), f"empty section: {diff!r}"
        assert diff.get("fact_ref") in _FACTS, f"unknown fact_ref: {diff!r}"
