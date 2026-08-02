"""Unit tests for the interview-prep LLMInterviewPrepGenerator.

Covers prompt building, LLM invocation, and the content-coercion state machine
(fenced blocks, preamble/postamble, JSON arrays, missing-JSON, empty content,
non-dict filtering, non-string list-item filtering) plus every branch of the
response-coercion helpers and the LLM factory. The LLM is always a fake object
- no network is used here.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from backend.app.services.interview_prep.generator import (
    CONTENT_KEYS,
    InterviewPrepGenerationError,
    LLMInterviewPrepGenerator,
    _coerce_content,
    _parse_content,
)
from backend.app.services.interview_prep.llm_factory import (
    InterviewPrepConfigError,
    build_interview_prep_llm,
)
from tests.conftest import settings_override

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeLLM:
    """Records the messages it receives and returns/raises a canned response."""

    def __init__(self, response: Any):
        # ``response`` is either the object returned by invoke(), or an
        # exception instance that invoke() should raise.
        self._response = response
        self.calls: list[list[Any]] = []

    def invoke(self, messages: list[Any]) -> Any:
        self.calls.append(messages)
        if isinstance(self._response, BaseException):
            raise self._response
        return self._response


def _msg(content: Any) -> SimpleNamespace:
    return SimpleNamespace(content=content)


_JOB = {"title": "Backend Engineer", "company": "Acme", "requirements": ["Python"]}
_FACTS = {"senior_engineer": {"role": "Senior Engineer", "summary": "Led API team"}}
_PREFS = {"desired_roles": ["Backend Engineer"], "target_cities": ["Beijing"]}
_MATCH = {"strengths": [{"area": "Python"}], "gaps": [{"area": "Go"}]}

_CONTENT = {
    "technical_questions": ["Explain Python GIL."],
    "behavioral_questions": ["Tell me about a conflict."],
    "talking_points": ["Led a team of 5."],
    "topics_to_review": ["Concurrency."],
    "questions_to_ask": ["What is the team structure?"],
}


def _generator(response: Any, settings=None) -> tuple[LLMInterviewPrepGenerator, _FakeLLM]:
    fake = _FakeLLM(response)
    return LLMInterviewPrepGenerator(fake, settings), fake


# ---------------------------------------------------------------------------
# generate_prep - happy paths
# ---------------------------------------------------------------------------


def test_generate_prep_clean_json_object():
    gen, fake = _generator(_msg(json.dumps(_CONTENT)))
    result = gen.generate_prep(
        job_snapshot=_JOB, profile_facts=_FACTS, preferences=_PREFS, match_analysis=_MATCH
    )
    assert result["content"] == _CONTENT
    assert result["agent_version"] == "1.0.0"  # settings=None -> default
    # Prompt carried all inputs.
    human_payload = json.loads(fake.calls[0][1].content)
    assert human_payload["job_snapshot"] == _JOB
    assert human_payload["profile_facts"] == _FACTS
    assert human_payload["preferences"] == _PREFS
    assert human_payload["match_analysis"] == _MATCH
    assert fake.calls[0][0].__class__.__name__ == "SystemMessage"


def test_generate_prep_fenced_block():
    content = f"Here is the kit:\n```json\n{json.dumps(_CONTENT)}\n```\nDone."
    gen, _ = _generator(_msg(content))
    assert gen.generate_prep(job_snapshot=_JOB, profile_facts=_FACTS)["content"] == _CONTENT


def test_generate_prep_preamble_postamble():
    content = f"Sure! {json.dumps(_CONTENT)} hope that helps"
    gen, _ = _generator(_msg(content))
    assert gen.generate_prep(job_snapshot=_JOB, profile_facts=_FACTS)["content"] == _CONTENT


def test_generate_prep_defaults_inputs_to_empty():
    gen, fake = _generator(_msg(json.dumps(_CONTENT)))
    gen.generate_prep(job_snapshot=_JOB)  # no facts/prefs/match
    payload = json.loads(fake.calls[0][1].content)
    assert payload["profile_facts"] == {}
    assert payload["preferences"] == {}
    assert payload["match_analysis"] == {}


def test_generate_prep_stamps_agent_version_from_settings():
    settings = settings_override(interview_prep_agent_version="2.3.0")
    gen, _ = _generator(_msg(json.dumps(_CONTENT)), settings=settings)
    assert gen.agent_version == "2.3.0"
    assert gen.generate_prep(job_snapshot=_JOB)["agent_version"] == "2.3.0"


def test_generate_prep_normalizes_non_list_and_non_string_items():
    # Non-list values are dropped to [], non-string list items are filtered,
    # and unknown keys are ignored. technical_questions survives -> non-empty.
    payload = {
        "technical_questions": ["q1", 2, None, "q2"],
        "behavioral_questions": "not-a-list",  # -> []
        "extra": "ignored",
    }
    gen, _ = _generator(_msg(json.dumps(payload)))
    content = gen.generate_prep(job_snapshot=_JOB)["content"]
    assert content["technical_questions"] == ["q1", "q2"]
    assert content["behavioral_questions"] == []
    assert content["talking_points"] == []
    assert content["topics_to_review"] == []
    assert content["questions_to_ask"] == []
    assert "extra" not in content


# ---------------------------------------------------------------------------
# generate_prep - parse errors propagate as InterviewPrepGenerationError
# ---------------------------------------------------------------------------


def test_generate_prep_no_json_raises():
    gen, _ = _generator(_msg("the candidate looks great, no prep needed"))
    with pytest.raises(InterviewPrepGenerationError, match="parseable") as exc:
        gen.generate_prep(job_snapshot=_JOB)
    assert exc.value.code == "interview_prep_parse_error"


def test_generate_prep_json_array_raises():
    # A bare JSON array is parseable but not an object -> parse error.
    gen, _ = _generator(_msg(json.dumps(["a", "b"])))
    with pytest.raises(InterviewPrepGenerationError) as exc:
        gen.generate_prep(job_snapshot=_JOB)
    assert exc.value.code == "interview_prep_parse_error"


def test_generate_prep_empty_content_raises():
    # A dict with no recognized sections -> empty_content.
    gen, _ = _generator(_msg(json.dumps({"summary": "no recognized keys"})))
    with pytest.raises(InterviewPrepGenerationError, match="no recognized") as exc:
        gen.generate_prep(job_snapshot=_JOB)
    assert exc.value.code == "interview_prep_empty_content"


def test_generate_prep_all_empty_lists_raises():
    gen, _ = _generator(
        _msg(json.dumps({k: [] for k in CONTENT_KEYS}))
    )
    with pytest.raises(InterviewPrepGenerationError) as exc:
        gen.generate_prep(job_snapshot=_JOB)
    assert exc.value.code == "interview_prep_empty_content"


def test_generate_prep_llm_exception_propagates():
    gen, _ = _generator(ConnectionError("boom"))
    with pytest.raises(ConnectionError):
        gen.generate_prep(job_snapshot=_JOB)


def test_generate_prep_empty_response_raises():
    gen, _ = _generator(_msg(""))
    with pytest.raises(InterviewPrepGenerationError):
        gen.generate_prep(job_snapshot=_JOB)


# ---------------------------------------------------------------------------
# _coerce_content / _parse_content - explicit branch coverage
# ---------------------------------------------------------------------------


def test_coerce_content_returns_none_for_non_dict():
    assert _coerce_content(["a", "b"]) is None
    assert _coerce_content("scalar") is None
    assert _coerce_content(None) is None
    assert _coerce_content(7) is None


def test_coerce_content_normalizes_dict():
    payload = {
        "technical_questions": ["q1", 1, "q2"],
        "behavioral_questions": "nope",  # non-list -> []
    }
    result = _coerce_content(payload)
    assert result is not None
    assert result["technical_questions"] == ["q1", "q2"]
    assert result["behavioral_questions"] == []
    # Missing keys default to [].
    assert result["talking_points"] == []
    assert result["topics_to_review"] == []
    assert result["questions_to_ask"] == []


def test_parse_content_happy_returns_normalized():
    content = json.dumps(_CONTENT)
    parsed = _parse_content(content)
    assert parsed == _CONTENT


def test_parse_content_no_json_raises_parse_error():
    with pytest.raises(InterviewPrepGenerationError) as exc:
        _parse_content("totally not json")
    assert exc.value.code == "interview_prep_parse_error"


def test_parse_content_non_dict_raises_parse_error():
    with pytest.raises(InterviewPrepGenerationError) as exc:
        _parse_content(json.dumps(["a", "b"]))
    assert exc.value.code == "interview_prep_parse_error"


def test_parse_content_empty_dict_raises_empty_content():
    with pytest.raises(InterviewPrepGenerationError) as exc:
        _parse_content(json.dumps({"unrelated": "x"}))
    assert exc.value.code == "interview_prep_empty_content"


# ---------------------------------------------------------------------------
# llm_factory
# ---------------------------------------------------------------------------


def _clear_api_keys(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    import src.utils as utils_mod

    monkeypatch.setattr(utils_mod, "_get_windows_user_env", lambda _name: None)


def test_build_interview_prep_llm_raises_without_key(monkeypatch):
    _clear_api_keys(monkeypatch)
    settings = settings_override(interview_prep_model="deepseek-v4-flash")
    with pytest.raises(InterviewPrepConfigError) as exc:
        build_interview_prep_llm(settings)
    assert exc.value.code == "missing_api_key"


def test_build_interview_prep_llm_builds_with_key(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    import src.utils as utils_mod

    monkeypatch.setattr(utils_mod, "_get_windows_user_env", lambda _name: None)
    settings = settings_override(interview_prep_model="deepseek-v4-flash")
    llm = build_interview_prep_llm(settings)
    assert llm.model_name == "deepseek-v4-flash"
    assert "deepseek" in llm.openai_api_base


def test_build_interview_prep_llm_disables_v4_thinking(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    import src.utils as utils_mod

    monkeypatch.setattr(utils_mod, "_get_windows_user_env", lambda _name: None)
    settings = settings_override(interview_prep_model="deepseek-v4-flash")
    llm = build_interview_prep_llm(settings)
    assert llm.extra_body == {"thinking": {"type": "disabled"}}


def test_build_interview_prep_llm_skips_thinking_flag_for_non_v4(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    import src.utils as utils_mod

    monkeypatch.setattr(utils_mod, "_get_windows_user_env", lambda _name: None)
    settings = settings_override(interview_prep_model="deepseek-v3-0324")
    llm = build_interview_prep_llm(settings)
    assert getattr(llm, "extra_body", None) in (None, {})
