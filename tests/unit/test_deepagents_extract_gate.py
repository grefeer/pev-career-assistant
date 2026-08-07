from __future__ import annotations

from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.career_skills.job_discovery import (
    ExtractObservedJobDetailsInput,
    ExtractObservedJobDetailsOutput,
)
from backend.app.services.deepagents_runtime.tools import extract_gate
from backend.app.services.deepagents_runtime.tools.extract_gate import extract_with_gate

_CONTEXT = ToolContext(user_id="u1", run_id="r1")


def _candidate(**overrides) -> dict:
    candidate = {
        "title": "后端工程师",
        "company_name": "示例公司",
        "locations": ["上海"],
        "responsibilities": "职责",
        "requirements": "要求",
        "recruitment_types": ["校招"],
        "apply_url": None,
        "deadline_text": None,
        "confidence": 0.9,
        "evidence_refs": [{"artifact_id": "a1"}],
        "normalization_warnings": [],
    }
    candidate.update(overrides)
    return candidate


def _regex_output(*candidates: dict) -> ExtractObservedJobDetailsOutput:
    return ExtractObservedJobDetailsOutput(
        source_artifact_id="a1",
        source_url="https://example.com/jobs",
        content_hash="hash-regex",
        candidates=list(candidates),
    )


def _llm_extractor(context, payload) -> ExtractObservedJobDetailsOutput:
    return ExtractObservedJobDetailsOutput(
        source_artifact_id=payload.artifact_id,
        source_url="https://example.com/jobs",
        content_hash="hash-llm",
        candidates=[_candidate(title="后端工程师(LLM)")],
    )


def _patch_regex(monkeypatch, output: ExtractObservedJobDetailsOutput) -> None:
    monkeypatch.setattr(
        extract_gate,
        "extract_observed_job_details",
        lambda context, payload: output,
    )


def test_gate_disabled_never_calls_llm(monkeypatch) -> None:
    called = []

    def llm(context, payload):
        called.append(1)
        return _llm_extractor(context, payload)

    _patch_regex(monkeypatch, _regex_output())
    payload = ExtractObservedJobDetailsInput(artifact_id="a1")
    result = extract_with_gate(_CONTEXT, payload, enabled=False, llm_extractor=llm)
    assert called == []
    assert result.candidates == []


def test_gate_without_llm_extractor_returns_regex(monkeypatch) -> None:
    _patch_regex(monkeypatch, _regex_output(_candidate()))
    payload = ExtractObservedJobDetailsInput(artifact_id="a1")
    result = extract_with_gate(_CONTEXT, payload, enabled=True, llm_extractor=None)
    assert [c.title for c in result.candidates] == ["后端工程师"]


def test_gate_skips_llm_when_regex_confident(monkeypatch) -> None:
    called = []

    def llm(context, payload):
        called.append(1)
        return _llm_extractor(context, payload)

    _patch_regex(monkeypatch, _regex_output(_candidate(confidence=0.9)))
    payload = ExtractObservedJobDetailsInput(artifact_id="a1")
    result = extract_with_gate(_CONTEXT, payload, enabled=True, llm_extractor=llm)
    assert called == []
    assert [c.title for c in result.candidates] == ["后端工程师"]


def test_gate_calls_llm_on_empty_regex(monkeypatch) -> None:
    _patch_regex(monkeypatch, _regex_output())
    payload = ExtractObservedJobDetailsInput(artifact_id="a1")
    result = extract_with_gate(
        _CONTEXT, payload, enabled=True, llm_extractor=_llm_extractor
    )
    assert [c.title for c in result.candidates] == ["后端工程师(LLM)"]


def test_gate_calls_llm_on_low_confidence(monkeypatch) -> None:
    # ExtractedJobDetails.confidence is a non-nullable float, so "missing
    # confidence" cannot exist in the model; low confidence is the gate
    # trigger (threshold _LOW_CONFIDENCE_BELOW = 0.6).
    _patch_regex(
        monkeypatch,
        _regex_output(_candidate(confidence=0.4), _candidate(title="低置信", confidence=0.4)),
    )
    payload = ExtractObservedJobDetailsInput(artifact_id="a1")
    result = extract_with_gate(
        _CONTEXT, payload, enabled=True, llm_extractor=_llm_extractor
    )
    titles = [c.title for c in result.candidates]
    assert "后端工程师" in titles  # regex candidate preserved verbatim
    assert "低置信" in titles
    assert "后端工程师(LLM)" in titles  # new identity appended (pareto union)


def test_merge_skips_duplicate_identity(monkeypatch) -> None:
    _patch_regex(monkeypatch, _regex_output(_candidate(confidence=0.4)))

    def same_identity_llm(context, payload):
        return ExtractObservedJobDetailsOutput(
            source_artifact_id=payload.artifact_id,
            source_url="https://example.com/jobs",
            content_hash="hash-llm",
            candidates=[
                _candidate(confidence=0.9, title="后端工程师", responsibilities="LLM 改写")
            ],
        )

    payload = ExtractObservedJobDetailsInput(artifact_id="a1")
    result = extract_with_gate(
        _CONTEXT, payload, enabled=True, llm_extractor=same_identity_llm
    )
    assert len(result.candidates) == 1
    assert result.candidates[0].responsibilities == "职责"  # regex version kept


def test_merge_identity_treats_none_title_or_company_as_empty(monkeypatch) -> None:
    # (title, company_name) may be None individually; the identity must then
    # fall back to the empty string so a None-named LLM candidate still
    # dedups against a None-named regex candidate.
    _patch_regex(
        monkeypatch,
        _regex_output(
            _candidate(title=None, confidence=0.4),
            _candidate(company_name=None, confidence=0.4),
        ),
    )

    def none_named_llm(context, payload):
        return ExtractObservedJobDetailsOutput(
            source_artifact_id=payload.artifact_id,
            source_url="https://example.com/jobs",
            content_hash="hash-llm",
            candidates=[
                _candidate(title=None, responsibilities="LLM 改写"),
                _candidate(company_name=None, responsibilities="LLM 改写"),
            ],
        )

    payload = ExtractObservedJobDetailsInput(artifact_id="a1")
    result = extract_with_gate(
        _CONTEXT, payload, enabled=True, llm_extractor=none_named_llm
    )
    assert len(result.candidates) == 2  # both None-named identities deduped
    assert {c.responsibilities for c in result.candidates} == {"职责"}
