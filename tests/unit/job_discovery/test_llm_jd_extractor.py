"""Unit tests for the LLM JD-body extractor (PATH C quality port).

Covers the lenient JSON parser, flag gating, graceful degradation, field
coercion, and the integration fork in ``_extract_and_verify_candidates_from_evidence``
(flag ON -> LLM body candidates; flag OFF -> deterministic title-only, LLM never
called). All LLM calls are mocked - no network, no DeepSeek key required.
"""
# ruff: noqa: E402  (sys.path bootstrap must precede project imports)

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from backend.app.services.job_discovery.deepagents_runner import (  # noqa: E402
    _extract_and_verify_candidates_from_evidence,
)
from backend.app.services.job_discovery.extraction.llm_jd_extractor import (  # noqa: E402
    _extract_json_array,
    extract_jd_candidates_llm,
)


class _FakeResp:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeLLM:
    """Mock ChatOpenAI: returns scripted responses (or raises)."""

    def __init__(self, responses: list) -> None:
        # each item is a str (content) or an Exception (raised on invoke)
        self._responses = list(responses)
        self.calls = 0

    def invoke(self, messages) -> _FakeResp:  # noqa: ANN001
        self.calls += 1
        if not self._responses:
            return _FakeResp("[]")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return _FakeResp(item)


def _settings(flag: bool = True) -> SimpleNamespace:
    """Duck-typed Settings double carrying only the two fields the extractor reads."""
    return SimpleNamespace(
        job_discovery_llm_extraction_enabled=flag,
        job_discovery_model="deepseek-v4-flash",
    )


_URL = "https://careers.example.com/jobs"
_REF = {"url": _URL, "content_hash": "sha256_test", "evidence_type": "page_text"}

_BODY_JSON = (
    '[{"title":"算法工程师","company_name":"理想汽车",'
    '"responsibilities":"1. 负责感知\\n2. 参与VLA",'
    '"requirements":"硕士及以上\\n熟悉Python",'
    '"locations":["北京","上海"],"recruitment_types":["校园招聘"],'
    '"confidence":0.9}]'
)


# --- flag gating / early returns -------------------------------------------


def test_flag_off_returns_empty_and_never_invokes_llm() -> None:
    llm = _FakeLLM([_BODY_JSON])
    out = extract_jd_candidates_llm("some text", _URL, settings=_settings(False), model=llm)
    assert out == []
    assert llm.calls == 0


def test_empty_page_text_returns_empty() -> None:
    llm = _FakeLLM([_BODY_JSON])
    assert extract_jd_candidates_llm("   \n  ", _URL, settings=_settings(), model=llm) == []
    assert llm.calls == 0


def test_no_model_and_no_credentials_degrades_to_empty(monkeypatch) -> None:
    # _build_extractor_llm imports src.utils; force it to fail so it returns None.
    import backend.app.services.job_discovery.extraction.llm_jd_extractor as mod

    monkeypatch.setattr(
        mod, "_build_extractor_llm", lambda settings: None
    )
    out = extract_jd_candidates_llm("some text", _URL, settings=_settings(), model=None)
    assert out == []


# --- happy path -------------------------------------------------------------


def test_parses_json_array_into_candidates() -> None:
    llm = _FakeLLM([_BODY_JSON])
    out = extract_jd_candidates_llm("page text", _URL, settings=_settings(), model=llm, ref=_REF)
    assert llm.calls == 1
    assert len(out) == 1
    c = out[0]
    assert c.title == "算法工程师"
    assert c.company_name == "理想汽车"
    assert "负责感知" in c.responsibilities
    assert "硕士及以上" in c.requirements
    assert c.locations == ["北京", "上海"]
    assert c.recruitment_types == ["校园招聘"]
    assert c.confidence == 0.9
    assert c.evidence_refs == [_REF]


# --- lenient parser (ported skill recipe) ----------------------------------


def test_extract_json_array_handles_fence() -> None:
    assert _extract_json_array("```json\n[{\"a\":1}]\n```") == [{"a": 1}]


def test_extract_json_array_handles_single_object() -> None:
    assert _extract_json_array('{"a":1}') == [{"a": 1}]


def test_extract_json_array_handles_prose_wrapped() -> None:
    assert _extract_json_array('Here are jobs:\n[{"a":1}]\nDone.') == [{"a": 1}]


def test_extract_json_array_empty_on_garbage() -> None:
    assert _extract_json_array("no json here") == []
    assert _extract_json_array("") == []


def test_recovers_from_code_fence() -> None:
    llm = _FakeLLM(["```json\n" + _BODY_JSON + "\n```"])
    out = extract_jd_candidates_llm("t", _URL, settings=_settings(), model=llm, ref=_REF)
    assert len(out) == 1
    assert out[0].title == "算法工程师"


def test_recovers_single_object() -> None:
    obj = _BODY_JSON.strip("[]")
    llm = _FakeLLM([obj])
    out = extract_jd_candidates_llm("t", _URL, settings=_settings(), model=llm, ref=_REF)
    assert len(out) == 1
    assert out[0].title == "算法工程师"


# --- graceful degradation / retry ------------------------------------------


def test_garbage_then_empty_returns_empty_after_retry() -> None:
    llm = _FakeLLM(["totally not json", "still not json"])
    out = extract_jd_candidates_llm("t", _URL, settings=_settings(), model=llm, ref=_REF)
    assert out == []
    assert llm.calls == 2  # verify-retry: one retry round


def test_first_garbage_second_valid_returns_valid() -> None:
    llm = _FakeLLM(["garbage", _BODY_JSON])
    out = extract_jd_candidates_llm("t", _URL, settings=_settings(), model=llm, ref=_REF)
    assert len(out) == 1
    assert out[0].title == "算法工程师"
    assert llm.calls == 2


def test_invoke_exception_returns_empty() -> None:
    llm = _FakeLLM([RuntimeError("boom"), RuntimeError("boom")])
    out = extract_jd_candidates_llm("t", _URL, settings=_settings(), model=llm, ref=_REF)
    assert out == []
    assert llm.calls == 2


def test_invoke_exception_then_success_returns_valid() -> None:
    llm = _FakeLLM([RuntimeError("transient"), _BODY_JSON])
    out = extract_jd_candidates_llm("t", _URL, settings=_settings(), model=llm, ref=_REF)
    assert len(out) == 1
    assert llm.calls == 2


# --- field coercion / title-only -------------------------------------------


def test_title_only_output_kept_with_warning() -> None:
    llm = _FakeLLM(['[{"title":"前端工程师","confidence":0.4}]'])
    out = extract_jd_candidates_llm("t", _URL, settings=_settings(), model=llm, ref=_REF)
    assert len(out) == 1
    c = out[0]
    assert c.responsibilities == ""
    assert c.requirements == ""
    assert any("Title-only" in w for w in c.normalization_warnings)


def test_field_coercion_string_locations_and_clamped_confidence() -> None:
    llm = _FakeLLM([
        '[{"title":"工程师","locations":"北京、上海","confidence":1.5,'
        '"apply_url":""}]'
    ])
    out = extract_jd_candidates_llm("t", _URL, settings=_settings(), model=llm, ref=_REF)
    assert len(out) == 1
    c = out[0]
    assert c.locations == ["北京", "上海"]
    assert c.confidence == 1.0
    assert c.apply_url == _URL  # empty apply_url falls back to the page URL


def test_entry_without_title_dropped() -> None:
    llm = _FakeLLM(['[{"title":"","responsibilities":"x"},{"title":"工程师"}]'])
    out = extract_jd_candidates_llm("t", _URL, settings=_settings(), model=llm, ref=_REF)
    assert len(out) == 1
    assert out[0].title == "工程师"


# --- integration: the supervisor fork -------------------------------------


def _page_text_evidence(text: str, url: str = _URL) -> list[dict]:
    """A page_text evidence page with no regex-matchable JD-detail structure."""
    return [{
        "evidence_type": "page_text",
        "url": url,
        "content_hash": "sha256_test",
        "text_excerpt": text,
    }]


def test_extract_and_verify_uses_llm_body_when_flag_on() -> None:
    text = "在招职位\n算法工程师\n前端开发工程师\n"
    llm = _FakeLLM([_BODY_JSON])
    cands, _ = _extract_and_verify_candidates_from_evidence(
        _page_text_evidence(text), _URL, settings=_settings(True), model=llm
    )
    assert llm.calls == 1
    titles = [c["title"] for c in cands]
    assert "算法工程师" in titles
    # LLM body candidate survives with its responsibilities/requirements body.
    body = [c for c in cands if c["title"] == "算法工程师"][0]
    assert body["responsibilities"]
    assert body["requirements"]


def test_extract_and_verify_falls_back_to_title_only_when_flag_off() -> None:
    text = "在招职位\n算法工程师\n前端开发工程师\n"
    llm = _FakeLLM([_BODY_JSON])  # would return body, but flag is off
    cands, _ = _extract_and_verify_candidates_from_evidence(
        _page_text_evidence(text), _URL, settings=_settings(False), model=llm
    )
    # Flag off -> LLM never invoked; deterministic title-only path runs unchanged.
    assert llm.calls == 0
    titles = {c["title"] for c in cands}
    # The deterministic loose title extractor surfaces at least the 工程师 titles.
    assert any("工程师" in t for t in titles)
    # And no LLM body leaks through.
    assert all(not (c["responsibilities"] or c["requirements"]) for c in cands)
