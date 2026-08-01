"""Live-LLM smoke test for the Interview Prep skill.

Opt-in: set ``RUN_INTERVIEW_PREP_SMOKE=1`` and have a ``DEEPSEEK_API_KEY``
available. Exercises the real ``LLMInterviewPrepGenerator`` against the
DeepSeek API to confirm the skill produces a well-formed, non-empty interview
kit - the "does it actually land" check that unit tests (which mock the LLM)
cannot cover.

Skipped otherwise so the default suite needs no network or credentials.
"""

from __future__ import annotations

import os

import pytest

from backend.app.services.interview_prep.generator import CONTENT_KEYS, LLMInterviewPrepGenerator
from backend.app.services.interview_prep.llm_factory import build_interview_prep_llm
from tests.conftest import settings_override

_RUN = os.environ.get("RUN_INTERVIEW_PREP_SMOKE") == "1"
_HAS_KEY = bool(
    os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENAI_API_KEY")
)

pytestmark = pytest.mark.skipif(
    not (_RUN and _HAS_KEY),
    reason="set RUN_INTERVIEW_PREP_SMOKE=1 and DEEPSEEK_API_KEY to run this live smoke",
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


def test_generator_produces_valid_kit_against_real_llm():
    """The real DeepSeek LLM returns a non-empty, well-formed content dict."""
    settings = settings_override(interview_prep_model="deepseek-v4-flash")
    llm = build_interview_prep_llm(settings)
    generator = LLMInterviewPrepGenerator(llm, settings)

    result = generator.generate_prep(
        job_snapshot=_JOB,
        profile_facts=_FACTS,
        preferences=_PREFS,
        match_analysis=_MATCH,
    )

    content = result["content"]
    assert isinstance(content, dict)
    assert result["agent_version"] == settings.interview_prep_agent_version
    # The generator guarantees all five sections exist as lists.
    for key in CONTENT_KEYS:
        assert key in content, f"missing section: {key}"
        assert isinstance(content[key], list), f"{key} is not a list: {content[key]!r}"

    # A useful kit proposes at least one technical + one behavioral question.
    assert len(content["technical_questions"]) >= 1, content["technical_questions"]
    assert len(content["behavioral_questions"]) >= 1, content["behavioral_questions"]
    # And at least one question to ask the interviewer.
    assert len(content["questions_to_ask"]) >= 1, content["questions_to_ask"]
    for item in content["technical_questions"] + content["behavioral_questions"]:
        assert isinstance(item, str) and item.strip(), f"empty item: {item!r}"
