"""Unit tests for the resume-tailoring LLMDraftGenerator.

Covers prompt building, LLM invocation, and the JSON-extraction state machine
(fenced blocks, preamble/postamble, bare arrays, missing-JSON, missing-diffs,
non-dict entry filtering) plus every branch of the response-coercion helpers.
The LLM is always a fake object - no network is used here.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from backend.app.config import Settings
from backend.app.services.resume_tailoring.generator import (
    DraftGenerationError,
    LLMDraftGenerator,
    _block_text,
    _coerce_diffs,
    _extract_content,
    _extract_json,
    _parse_diffs,
    _slice_between,
    _try_parse_json,
)
from backend.app.services.resume_tailoring.llm_factory import (
    DraftGeneratorConfigError,
    build_draft_generator_llm,
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


# ---------------------------------------------------------------------------
# generate_diffs - happy paths
# ---------------------------------------------------------------------------


def _generator(response: Any, settings: Settings | None = None) -> tuple[LLMDraftGenerator, _FakeLLM]:
    fake = _FakeLLM(response)
    return LLMDraftGenerator(fake, settings), fake


def test_generate_diffs_clean_json_object():
    diffs = [{"op": "highlight", "section": "work_experience", "fact_ref": "senior_engineer"}]
    gen, fake = _generator(_msg(json.dumps({"diffs": diffs})))
    result = gen.generate_diffs(
        job_snapshot=_JOB, profile_facts=_FACTS, preferences=_PREFS, match_analysis=_MATCH
    )
    assert result["diffs"] == diffs
    assert result["agent_version"] == "1.0.0"  # settings=None -> default
    # Prompt carried all inputs and the valid fact refs.
    human_payload = json.loads(fake.calls[0][1].content)
    assert human_payload["job_snapshot"] == _JOB
    assert human_payload["profile_facts"] == _FACTS
    assert human_payload["valid_fact_refs"] == ["senior_engineer"]
    assert human_payload["preferences"] == _PREFS
    assert human_payload["match_analysis"] == _MATCH
    assert fake.calls[0][0].__class__.__name__ == "SystemMessage"


def test_generate_diffs_fenced_block():
    diffs = [{"op": "omit", "section": "summary", "fact_ref": "senior_engineer"}]
    content = f"Here is the plan:\n```json\n{json.dumps({'diffs': diffs})}\n```\nDone."
    gen, _ = _generator(_msg(content))
    assert gen.generate_diffs(job_snapshot=_JOB, profile_facts=_FACTS)["diffs"] == diffs


def test_generate_diffs_preamble_postamble():
    diffs = [{"op": "rephrase", "section": "skills", "fact_ref": "senior_engineer"}]
    content = f"Sure! {json.dumps({'diffs': diffs})} hope that helps"
    gen, _ = _generator(_msg(content))
    assert gen.generate_diffs(job_snapshot=_JOB, profile_facts=_FACTS)["diffs"] == diffs


def test_generate_diffs_bare_array_response():
    diffs = [{"op": "highlight", "section": "work_experience", "fact_ref": "senior_engineer"}]
    gen, _ = _generator(_msg(json.dumps(diffs)))
    assert gen.generate_diffs(job_snapshot=_JOB, profile_facts=_FACTS)["diffs"] == diffs


def test_generate_diffs_filters_non_dict_entries():
    content = json.dumps({"diffs": [{"op": "omit", "section": "s", "fact_ref": "senior_engineer"}, "junk", 7]})
    gen, _ = _generator(_msg(content))
    diffs = gen.generate_diffs(job_snapshot=_JOB, profile_facts=_FACTS)["diffs"]
    assert len(diffs) == 1
    assert diffs[0]["op"] == "omit"


def test_generate_diffs_defaults_preferences_and_match_to_empty():
    diffs = [{"op": "summarize", "section": "summary", "fact_ref": "senior_engineer"}]
    gen, fake = _generator(_msg(json.dumps({"diffs": diffs})))
    gen.generate_diffs(job_snapshot=_JOB, profile_facts=_FACTS)  # no prefs/match
    payload = json.loads(fake.calls[0][1].content)
    assert payload["preferences"] == {}
    assert payload["match_analysis"] == {}


def test_generate_diffs_stamps_agent_version_from_settings():
    settings = settings_override(resume_tailoring_agent_version="2.3.0")
    gen, _ = _generator(_msg(json.dumps({"diffs": []})), settings=settings)
    assert gen.agent_version == "2.3.0"
    assert gen.generate_diffs(job_snapshot=_JOB, profile_facts=_FACTS)["agent_version"] == "2.3.0"


def test_generate_diffs_valid_fact_refs_empty_when_facts_not_dict():
    gen, fake = _generator(_msg(json.dumps({"diffs": []})))
    gen.generate_diffs(job_snapshot=_JOB, profile_facts=None)  # type: ignore[arg-type]
    payload = json.loads(fake.calls[0][1].content)
    assert payload["valid_fact_refs"] == []


# ---------------------------------------------------------------------------
# generate_diffs - parse errors propagate as DraftGenerationError
# ---------------------------------------------------------------------------


def test_generate_diffs_no_json_raises():
    gen, _ = _generator(_msg("the resume looks fine, no changes needed"))
    with pytest.raises(DraftGenerationError, match="parseable JSON") as exc:
        gen.generate_diffs(job_snapshot=_JOB, profile_facts=_FACTS)
    assert exc.value.code == "draft_generation_parse_error"


def test_generate_diffs_json_without_diffs_raises():
    gen, _ = _generator(_msg(json.dumps({"summary": "no diffs key"})))
    with pytest.raises(DraftGenerationError, match="diffs") as exc:
        gen.generate_diffs(job_snapshot=_JOB, profile_facts=_FACTS)
    assert exc.value.code == "draft_generation_parse_error"


def test_generate_diffs_diffs_not_a_list_raises():
    gen, _ = _generator(_msg(json.dumps({"diffs": "not-a-list"})))
    with pytest.raises(DraftGenerationError):
        gen.generate_diffs(job_snapshot=_JOB, profile_facts=_FACTS)


def test_generate_diffs_llm_exception_propagates():
    gen, _ = _generator(ConnectionError("boom"))
    with pytest.raises(ConnectionError):
        gen.generate_diffs(job_snapshot=_JOB, profile_facts=_FACTS)


def test_generate_diffs_empty_content_raises():
    gen, _ = _generator(_msg(""))
    with pytest.raises(DraftGenerationError):
        gen.generate_diffs(job_snapshot=_JOB, profile_facts=_FACTS)


# ---------------------------------------------------------------------------
# _extract_content - response shape variants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("response,expected", [
    (None, ""),
    (_msg("plain text"), "plain text"),
    ("raw string", "raw string"),                 # no .content attr -> itself
    (_msg(42), "42"),                              # non-str scalar content
    (_msg(["a", {"text": "b"}, 3]), "ab3"),        # list content blocks
])
def test_extract_content_variants(response, expected):
    assert _extract_content(response) == expected


def test_block_text_variants():
    assert _block_text("x") == "x"
    assert _block_text({"text": "y"}) == "y"
    assert _block_text({"other": "z"}) == ""       # dict without "text"
    assert _block_text(5) == "5"                   # other -> str()


# ---------------------------------------------------------------------------
# JSON extraction helpers - explicit branch coverage
# ---------------------------------------------------------------------------


def test_extract_json_returns_none_when_unparseable():
    assert _extract_json("totally not json at all") is None


def test_try_parse_json_empty_returns_none():
    assert _try_parse_json("   ") is None


def test_try_parse_json_broken_object_slice_returns_none():
    # A ``{...}`` span is found but is not valid JSON -> falls through to the
    # array branch (absent) and finally returns None.
    assert _try_parse_json("noise { broken object } more") is None


def test_try_parse_json_broken_array_slice_returns_none():
    # No object span; a ``[...]`` span is found but is not valid JSON.
    assert _try_parse_json("noise [ broken array ] more") is None


def test_slice_between_missing_open_returns_none():
    assert _slice_between("no brackets", "{", "}") is None


def test_slice_between_unterminated_returns_none():
    # close bracket precedes/equals open -> out of order
    assert _slice_between("} {", "{", "}") is None


def test_slice_between_missing_close_after_open_returns_none():
    # open present, but no close at all -> rfind returns -1 (<= start)
    assert _slice_between("{unterminated", "{", "}") is None


def test_coerce_diffs_variants():
    bare = [{"op": "omit"}, "x", 1]
    assert _coerce_diffs(bare) == [{"op": "omit"}]
    assert _coerce_diffs({"diffs": [{"op": "rephrase"}, None]}) == [{"op": "rephrase"}]
    assert _coerce_diffs({"diffs": "no"}) is None
    assert _coerce_diffs({"other": 1}) is None
    assert _coerce_diffs("scalar") is None
    assert _coerce_diffs(None) is None


def test_parse_diffs_returns_empty_list_when_valid():
    assert _parse_diffs(json.dumps({"diffs": []})) == []


def test_parse_diffs_array_input():
    payload = [{"op": "highlight", "section": "s", "fact_ref": "f"}]
    assert _parse_diffs(json.dumps(payload)) == payload


# ---------------------------------------------------------------------------
# llm_factory
# ---------------------------------------------------------------------------


def test_build_draft_generator_llm_raises_without_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # The Windows registry fallback can still surface a key in CI; patch it out.
    import src.utils as utils_mod

    monkeypatch.setattr(utils_mod, "_get_windows_user_env", lambda _name: None)
    settings = settings_override(resume_tailoring_model="deepseek-v4-flash")
    with pytest.raises(DraftGeneratorConfigError) as exc:
        build_draft_generator_llm(settings)
    assert exc.value.code == "missing_api_key"


def test_build_draft_generator_llm_builds_with_key(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    import src.utils as utils_mod

    monkeypatch.setattr(utils_mod, "_get_windows_user_env", lambda _name: None)
    settings = settings_override(resume_tailoring_model="deepseek-v4-flash")
    llm = build_draft_generator_llm(settings)
    # ChatOpenAI exposes the configured model name and base url.
    assert llm.model_name == "deepseek-v4-flash"
    assert "deepseek" in llm.openai_api_base


def test_build_draft_generator_llm_disables_v4_thinking(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    import src.utils as utils_mod

    monkeypatch.setattr(utils_mod, "_get_windows_user_env", lambda _name: None)
    settings = settings_override(resume_tailoring_model="deepseek-v4-flash")
    llm = build_draft_generator_llm(settings)
    assert llm.extra_body == {"thinking": {"type": "disabled"}}


def test_build_draft_generator_llm_skips_thinking_flag_for_non_v4(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    import src.utils as utils_mod

    monkeypatch.setattr(utils_mod, "_get_windows_user_env", lambda _name: None)
    settings = settings_override(resume_tailoring_model="deepseek-v3-0324")
    llm = build_draft_generator_llm(settings)
    # Non-v4 model: the thinking-disable guard does not apply.
    assert getattr(llm, "extra_body", None) in (None, {})
