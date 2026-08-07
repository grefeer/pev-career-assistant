from __future__ import annotations

from langchain_core.messages import AIMessage

from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.career_skills.job_discovery import (
    ExtractObservedJobDetailsInput,
)
from backend.app.services.deepagents_runtime.tools.llm_extractor import (
    LLMJobExtractor,
    _EXTRACTION_PROMPT,
    build_llm_extractor,
)
from tests.conftest import settings_override
from tests.unit.deepagents_testkit import ScriptedModel

_SOURCE_URL = "https://example.com/jobs"


def _ctx(
    artifact_id: str = "page_01",
    source_url: str = _SOURCE_URL,
    text: str = "岗位：后端工程师\n岗位职责：负责后端服务开发\n任职要求：精通 Python",
) -> ToolContext:
    return ToolContext(
        user_id="u",
        run_id="r",
        metadata={
            "observed_public_evidence": [
                {
                    "artifact_id": artifact_id,
                    "source_url": source_url,
                    "content_hash": artifact_id,
                    "visible_text": text,
                }
            ]
        },
    )


def _extractor_with(model) -> LLMJobExtractor:
    extractor = LLMJobExtractor(
        settings_override(deepagents_llm_extraction_enabled=True)
    )
    extractor._model = model
    return extractor


class _RecordingModel:
    """Replays one response and records the messages it received."""

    def __init__(self, response: str) -> None:
        self.response = response
        self.messages: list | None = None

    def invoke(self, messages, **kwargs) -> AIMessage:  # noqa: ANN001
        self.messages = list(messages)
        return AIMessage(content=self.response)


class _RaisingModel:
    """Never answers: any invocation explodes (model drift / API failure)."""

    def invoke(self, messages, **kwargs) -> AIMessage:  # noqa: ANN001
        raise RuntimeError("model exploded")


def test_build_llm_extractor_respects_flag() -> None:
    off = settings_override(deepagents_llm_extraction_enabled=False)
    assert build_llm_extractor(off) is None
    on = settings_override(deepagents_llm_extraction_enabled=True)
    extractor = build_llm_extractor(on)
    assert extractor is not None
    assert extractor._model is not None


def test_extractor_folds_parse_failure_to_empty_candidates() -> None:
    # ScriptedModel returns prose without any JSON -> extractor must NOT raise;
    # it folds to an empty candidate list (verifier sees honest "no candidates").
    # The evidence IS present here, so the fold happens at the lenient-parse
    # stage (the ValueError arm of the parse), not at the evidence lookup.
    model = ScriptedModel(responses=["这个页面没有可解析的职位内容。"])
    extractor = _extractor_with(model)
    output = extractor(_ctx(), ExtractObservedJobDetailsInput(artifact_id="page_01"))
    assert output.candidates == []
    assert output.content_hash == "page_01"


def test_extractor_lenient_json_fence_strip() -> None:
    # ```json fence around the payload parses cleanly
    model = ScriptedModel(
        responses=[
            '```json [{"title": "后端工程师", "company_name": "示例公司", '
            '"locations": ["上海"], "responsibilities": "负责后端", '
            '"requirements": "精通Python", "confidence": 0.9}] ```'
        ]
    )
    output = _extractor_with(model)(_ctx(), ExtractObservedJobDetailsInput(artifact_id="page_01"))
    assert len(output.candidates) == 1
    assert output.candidates[0].title == "后端工程师"
    assert output.candidates[0].company_name == "示例公司"
    assert output.candidates[0].locations == ["上海"]
    assert output.candidates[0].confidence == 0.9


def test_extractor_uses_extraction_guide_prompt() -> None:
    # the system prompt transcribes the extraction-guide.md contract: the
    # job-title / company / body fields the model must emit
    for field in (
        "title",
        "company_name",
        "locations",
        "responsibilities",
        "requirements",
        "recruitment_types",
        "apply_url",
        "deadline_text",
        "confidence",
    ):
        assert field in _EXTRACTION_PROMPT
    model = _RecordingModel(
        '{"title": "后端工程师", "company_name": "示例公司"}'
    )
    output = _extractor_with(model)(
        _ctx(), ExtractObservedJobDetailsInput(artifact_id="page_01")
    )
    # the page text is passed as the human message, verbatim from the
    # registered observed evidence (never a model-proposed URI)
    assert model.messages is not None
    assert model.messages[0].content == _EXTRACTION_PROMPT
    assert model.messages[1].content == _ctx().metadata["observed_public_evidence"][0][
        "visible_text"
    ]
    assert output.candidates[0].title == "后端工程师"


def test_extractor_never_raises_on_model_error() -> None:
    # a live-model failure (timeout / drift / API error) folds to an honest
    # empty output instead of crashing the run
    output = _extractor_with(_RaisingModel())(
        _ctx(), ExtractObservedJobDetailsInput(artifact_id="page_01")
    )
    assert output.candidates == []
    assert output.source_url == ""
    assert output.content_hash == "page_01"


def test_extractor_folds_when_evidence_missing() -> None:
    # no registered observed evidence for the artifact: fold WITHOUT invoking
    # the model (a raising model proves the extractor never calls it).  The
    # unrelated evidence item carries a content_hash whose observed: form
    # does NOT match the payload, so the lookup must fall through it.
    output = _extractor_with(_RaisingModel())(
        ToolContext(
            user_id="u",
            run_id="r",
            metadata={
                "observed_public_evidence": [
                    {"artifact_id": "other", "content_hash": "abc"}
                ]
            },
        ),
        ExtractObservedJobDetailsInput(artifact_id="missing"),
    )
    assert output.candidates == []
    assert output.content_hash == "missing"


def test_extractor_folds_on_empty_page_text() -> None:
    # evidence with no visible_text folds BEFORE the model is invoked (the
    # raising model proves the call never happens)
    output = _extractor_with(_RaisingModel())(
        _ctx(text=""), ExtractObservedJobDetailsInput(artifact_id="page_01")
    )
    assert output.candidates == []
    assert output.content_hash == "page_01"


def test_extractor_drops_invalid_items() -> None:
    # responsibilities as an int is not a valid JD body -> that item is
    # dropped, never raised; the valid sibling survives
    model = ScriptedModel(
        responses=[
            '[{"title": "后端工程师", "company_name": "示例公司"}, '
            '{"title": "坏数据", "company_name": "示例公司", "responsibilities": 123}]'
        ]
    )
    output = _extractor_with(model)(
        _ctx(), ExtractObservedJobDetailsInput(artifact_id="page_01")
    )
    assert [c.title for c in output.candidates] == ["后端工程师"]


def test_extractor_evidence_refs_bound_to_artifact() -> None:
    # the model never supplies evidence_refs: every candidate's refs are
    # tool-bound to the payload artifact (model-proposed URIs never trusted)
    model = ScriptedModel(
        responses=[
            '{"title": "后端工程师", "company_name": "示例公司", '
            '"evidence_refs": [{"source_url": "https://evil.example.com"}]}'
        ]
    )
    output = _extractor_with(model)(
        _ctx(), ExtractObservedJobDetailsInput(artifact_id="page_01")
    )
    assert output.source_url == _SOURCE_URL
    refs = output.candidates[0].evidence_refs
    assert refs == [
        {
            "artifact_id": "page_01",
            "source_url": _SOURCE_URL,
            "content_hash": "page_01",
        }
    ]


def test_extractor_parses_clean_candidates_wrapper() -> None:
    # a clean {"candidates": [...]} payload parses via the whole-document
    # load, the wrapper unwraps to its candidate list, and non-dict items
    # inside the wrapper are dropped (not raised)
    model = ScriptedModel(
        responses=['{"candidates": [{"title": "前端工程师"}, "junk"]}']
    )
    output = _extractor_with(model)(
        _ctx(), ExtractObservedJobDetailsInput(artifact_id="page_01")
    )
    assert [c.title for c in output.candidates] == ["前端工程师"]


def test_extractor_parses_candidates_wrapper_with_trailing_prose() -> None:
    # prose around a {"candidates": [...]} object: the balanced object is
    # found and the wrapper unwrapped
    model = ScriptedModel(
        responses=[
            "根据页面内容，以下是职位信息："
            '{"candidates": [{"title": "前端工程师", "company_name": "示例公司"}]} '
            "以上就是全部职位。"
        ]
    )
    output = _extractor_with(model)(
        _ctx(), ExtractObservedJobDetailsInput(artifact_id="page_01")
    )
    assert [c.title for c in output.candidates] == ["前端工程师"]


def test_extractor_parses_array_with_prose_prefix() -> None:
    model = ScriptedModel(
        responses=[
            "Here are the candidates: "
            '[{"title": "算法工程师", "company_name": "示例公司"}]'
        ]
    )
    output = _extractor_with(model)(
        _ctx(), ExtractObservedJobDetailsInput(artifact_id="page_01")
    )
    assert [c.title for c in output.candidates] == ["算法工程师"]


def test_extractor_recovers_first_object_from_truncated_array() -> None:
    # the model hit max_tokens mid-array: the complete first object survives
    model = ScriptedModel(
        responses=[
            '[{"title": "后端工程师", "company_name": "示例公司"}, '
            '{"title": "未完成'
        ]
    )
    output = _extractor_with(model)(
        _ctx(), ExtractObservedJobDetailsInput(artifact_id="page_01")
    )
    assert [c.title for c in output.candidates] == ["后端工程师"]


def test_extractor_handles_escaped_quotes_and_braces_in_strings() -> None:
    # brace/quote state inside JSON strings must not confuse the balanced scan
    model = ScriptedModel(
        responses=[
            '[{"title": "前端工程师 \\"资深\\"", "company_name": "示例公司", '
            '"requirements": "熟悉 {JSON} 与 Vue"}]'
        ]
    )
    output = _extractor_with(model)(
        _ctx(), ExtractObservedJobDetailsInput(artifact_id="page_01")
    )
    assert len(output.candidates) == 1
    assert output.candidates[0].title == '前端工程师 "资深"'
    assert output.candidates[0].requirements == "熟悉 {JSON} 与 Vue"


def test_extractor_handles_scalar_model_output() -> None:
    # a model that answers a bare scalar (not a JD payload) yields no
    # candidates, never a crash
    output = _extractor_with(ScriptedModel(responses=["42"]))(
        _ctx(), ExtractObservedJobDetailsInput(artifact_id="page_01")
    )
    assert output.candidates == []


def test_extractor_drops_non_dict_list_items() -> None:
    model = ScriptedModel(
        responses=[
            '[{"title": "后端工程师", "company_name": "示例公司"}, "junk"]'
        ]
    )
    output = _extractor_with(model)(
        _ctx(), ExtractObservedJobDetailsInput(artifact_id="page_01")
    )
    assert [c.title for c in output.candidates] == ["后端工程师"]


def test_extractor_missing_confidence_defaults_low() -> None:
    # a candidate without an explicit confidence is flagged for manual
    # review (0.5), never fabricated as high-confidence
    model = ScriptedModel(responses=['{"title": "后端工程师", "company_name": "示例公司"}'])
    output = _extractor_with(model)(
        _ctx(), ExtractObservedJobDetailsInput(artifact_id="page_01")
    )
    assert output.candidates[0].confidence == 0.5


def test_extractor_resolves_observed_prefixed_payload() -> None:
    # legacy prefixed payloads ("observed:<hash>") resolve evidence whose
    # content_hash matches, mirroring career_skills _find_observed_evidence
    ctx = _ctx(artifact_id="abc123")
    model = ScriptedModel(responses=['{"title": "后端工程师", "company_name": "示例公司"}'])
    output = _extractor_with(model)(
        ctx, ExtractObservedJobDetailsInput(artifact_id="observed:abc123")
    )
    assert output.candidates[0].title == "后端工程师"
    assert output.candidates[0].evidence_refs[0]["artifact_id"] == "observed:abc123"


def test_extractor_skips_non_dict_evidence_items() -> None:
    # a non-dict entry in the evidence list is skipped; the valid one resolves
    ctx = ToolContext(
        user_id="u",
        run_id="r",
        metadata={
            "observed_public_evidence": [
                "junk",
                {
                    "artifact_id": "page_01",
                    "source_url": _SOURCE_URL,
                    "content_hash": "page_01",
                    "visible_text": "岗位：后端工程师",
                },
            ]
        },
    )
    output = _extractor_with(ScriptedModel(responses=['{"title": "后端工程师"}']))(
        ctx, ExtractObservedJobDetailsInput(artifact_id="page_01")
    )
    assert output.candidates[0].title == "后端工程师"
