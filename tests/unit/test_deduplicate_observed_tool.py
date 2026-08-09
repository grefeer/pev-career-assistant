"""PEV run-internal dedup tool (deduplicate-observed-jobs) unit tests."""

from __future__ import annotations

import json

import pytest

from backend.app.services.career_skills.deduplicate_observed import (
    DeduplicateObservedJobsInput,
    DeduplicateObservedJobsOutput,
    DeduplicatedRemoval,
    deduplicate_observed_jobs,
    _detail_identity_keys,
    _evidence_identity_keys,
    _normalize_apply_url,
    _normalize_text,
    _normalize_title,
    _record_identity_keys,
    _title_identity,
)
from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.career_skills.job_discovery import (
    ExtractedJobDetails,
    _parse_adapter_evidence,
)


def _adapter_evidence_text(records: list[dict]) -> str:
    return json.dumps(records, ensure_ascii=False, indent=2)


def _adapter_record(
    title: str = "算法工程师",
    *,
    job_id: str | None = "MK_1",
    apply_url: str | None = "https://jobs.moka.com/job/1",
    location: str = "北京",
    description: str = "岗位职责：负责推荐系统。任职要求：5 年经验。",
) -> dict:
    record: dict = {
        "title": title,
        "description": description,
        "apply_url": apply_url,
    }
    if job_id is not None:
        record["job_id"] = job_id
    if location is not None:
        record["location"] = location
    return record


def _evidence_metadata(artifact_id: str, text: str, **extra: object) -> dict:
    item: dict[str, object] = {
        "artifact_id": artifact_id,
        "source_url": "https://jobs.example/x",
        "content_hash": artifact_id.removeprefix("observed:"),
        "visible_text": text,
        "title": None,
    }
    item.update(extra)
    return {"observed_public_evidence": [item]}


def _context(items: list[dict[str, object]]) -> ToolContext:
    return ToolContext(user_id="u", run_id="r", metadata={"observed_public_evidence": items})


def test_dedup_removes_adapter_job_id_duplicate() -> None:
    first = _adapter_record(title="算法工程师", job_id="MK_7")
    second = _adapter_record(title="算法工程师（社招）", job_id="MK_7", apply_url="https://jobs.moka.com/job/7?v=2")
    context = _context([
        _evidence_metadata("observed:a", _adapter_evidence_text([first]))["observed_public_evidence"][0],
        _evidence_metadata("observed:b", _adapter_evidence_text([second]))["observed_public_evidence"][0],
    ])
    result = deduplicate_observed_jobs(
        context,
        DeduplicateObservedJobsInput(artifact_ids=["observed:a", "observed:b"]),
    )
    assert result.kept == ["observed:a"]
    assert len(result.removed) == 1
    removal = result.removed[0]
    assert removal.artifact_id == "observed:b"
    assert removal.reason == "duplicate_identity"
    assert "observed:a" in removal.detail


def test_dedup_adapter_url_and_title_fallbacks() -> None:
    # Same apply_url, no job_id -> duplicate.
    no_job_id = _adapter_record(job_id=None, apply_url="https://jobs.moka.com/job/9/")
    same_url = _adapter_record(job_id=None, apply_url="https://jobs.moka.com/JOB/9")
    context = _context([
        _evidence_metadata("observed:u1", _adapter_evidence_text([no_job_id]))["observed_public_evidence"][0],
        _evidence_metadata("observed:u2", _adapter_evidence_text([same_url]))["observed_public_evidence"][0],
    ])
    result = deduplicate_observed_jobs(
        context, DeduplicateObservedJobsInput(artifact_ids=["observed:u1", "observed:u2"])
    )
    assert result.kept == ["observed:u1"]
    assert result.removed[0].reason == "duplicate_identity"

    # No job_id, no apply_url, same normalized title -> duplicate (title fallback).
    title_only_a = _adapter_record(job_id=None, apply_url=None, title="算法工程师（校招）")
    title_only_b = _adapter_record(job_id=None, apply_url=None, title="算法工程师(校招)")
    context = _context([
        _evidence_metadata("observed:t1", _adapter_evidence_text([title_only_a]))["observed_public_evidence"][0],
        _evidence_metadata("observed:t2", _adapter_evidence_text([title_only_b]))["observed_public_evidence"][0],
    ])
    result = deduplicate_observed_jobs(
        context, DeduplicateObservedJobsInput(artifact_ids=["observed:t1", "observed:t2"])
    )
    assert result.kept == ["observed:t1"]
    assert result.removed[0].reason == "duplicate_identity"


def test_dedup_keeps_distinct_adapter_records() -> None:
    context = _context([
        _evidence_metadata("observed:a", _adapter_evidence_text([_adapter_record(job_id="MK_1")]))["observed_public_evidence"][0],
        _evidence_metadata("observed:b", _adapter_evidence_text([_adapter_record(job_id="MK_2")]))["observed_public_evidence"][0],
    ])
    result = deduplicate_observed_jobs(
        context, DeduplicateObservedJobsInput(artifact_ids=["observed:a", "observed:b"])
    )
    assert result.kept == ["observed:a", "observed:b"]
    assert result.removed == []


def _page_text(title: str, location: str = "北京") -> str:
    return (
        f"岗位名称：{title}\n"
        "岗位职责：负责智能体系统的设计与开发，参与招聘平台后端架构与数据处理链路建设，"
        "配合产品团队完成需求分析、技术方案评审与上线交付。\n"
        "任职要求：计算机相关专业，5 年以上后端开发经验，熟悉 Python 与分布式系统，"
        "具备良好的沟通能力和团队协作精神。\n"
        f"工作地点：{location}\n"
    )


def test_dedup_text_evidence_same_source_url_is_duplicate() -> None:
    # The extractor keys page candidates by their source URL (apply_url
    # fallback), so two artifacts fetched from the same URL are the same
    # posting even when the page content drifted between fetches.
    first = _evidence_metadata("observed:p1", _page_text("算法工程师"))
    second = _evidence_metadata("observed:p2", _page_text("算法工程师"))
    context = _context([
        first["observed_public_evidence"][0],
        second["observed_public_evidence"][0],
    ])
    result = deduplicate_observed_jobs(
        context, DeduplicateObservedJobsInput(artifact_ids=["observed:p1", "observed:p2"])
    )
    assert result.kept == ["observed:p1"]
    assert result.removed[0].reason == "duplicate_identity"
    assert "observed:p1" in result.removed[0].detail


def test_dedup_text_evidence_same_title_different_urls_kept() -> None:
    # Different source URLs are different apply routes: same normalized title
    # must NOT dedupe them (this is the cross-company collision guard).
    first = _evidence_metadata("observed:p1", _page_text("算法工程师"))
    second = _evidence_metadata(
        "observed:p2", _page_text("算法工程师"), source_url="https://jobs.example/y"
    )
    context = _context([
        first["observed_public_evidence"][0],
        second["observed_public_evidence"][0],
    ])
    result = deduplicate_observed_jobs(
        context, DeduplicateObservedJobsInput(artifact_ids=["observed:p1", "observed:p2"])
    )
    assert result.kept == ["observed:p1", "observed:p2"]
    assert result.removed == []


def test_dedup_text_evidence_keeps_distinct_titles() -> None:
    context = _context([
        _evidence_metadata("observed:p1", _page_text("算法工程师"), source_url="https://jobs.example/x")["observed_public_evidence"][0],
        _evidence_metadata("observed:p2", _page_text("前端开发工程师"), source_url="https://jobs.example/y")["observed_public_evidence"][0],
    ])
    result = deduplicate_observed_jobs(
        context, DeduplicateObservedJobsInput(artifact_ids=["observed:p1", "observed:p2"])
    )
    assert result.kept == ["observed:p1", "observed:p2"]
    assert result.removed == []


def test_dedup_reports_missing_and_incomplete_evidence() -> None:
    context = _context([
        _evidence_metadata("observed:no-text", "")["observed_public_evidence"][0],
    ])
    result = deduplicate_observed_jobs(
        context,
        DeduplicateObservedJobsInput(artifact_ids=["observed:ghost", "observed:no-text"]),
    )
    assert result.kept == []
    assert result.removed[0].artifact_id == "observed:ghost"
    assert result.removed[0].reason == "evidence_not_found"
    assert result.removed[1].artifact_id == "observed:no-text"
    assert result.removed[1].reason == "evidence_incomplete"


def test_dedup_keeps_identity_less_evidence() -> None:
    # A bare "[" fails the strict adapter parse and extracts no candidates.
    context = _context([
        _evidence_metadata("observed:x", "[")["observed_public_evidence"][0],
    ])
    result = deduplicate_observed_jobs(
        context, DeduplicateObservedJobsInput(artifact_ids=["observed:x"])
    )
    assert result.kept == ["observed:x"]
    assert result.removed == []


def test_dedup_extract_error_keeps_artifact() -> None:
    # Missing source_url makes extraction fail; a broken artifact is kept
    # (this tool removes duplicates, never silently drops evidence).
    broken = _evidence_metadata("observed:broken", _page_text("算法工程师"), source_url=None)
    context = _context([broken["observed_public_evidence"][0]])
    result = deduplicate_observed_jobs(
        context, DeduplicateObservedJobsInput(artifact_ids=["observed:broken"])
    )
    assert result.kept == ["observed:broken"]
    assert result.removed == []


def test_normalize_title_strips_bracketed_qualifiers() -> None:
    assert _normalize_title("算法工程师") == "算法工程师"
    assert _normalize_title("算法工程师（校招）") == "算法工程师"
    assert _normalize_title("算法工程师(校招)") == "算法工程师"
    assert _normalize_title(" 前端工程师【北京】 ") == "前端工程师"
    assert _normalize_title("") == ""
    assert _normalize_title(None) == ""
    # Full-width chars NFKC-normalize to half-width before bracket stripping.
    assert _normalize_title("ＡＢＣ（实习）") == "abc"


def test_normalize_apply_url_and_text() -> None:
    assert _normalize_apply_url("https://Jobs.Moka.com/job/1/") == "https://jobs.moka.com/job/1"
    assert _normalize_apply_url("") == ""
    assert _normalize_apply_url(None) == ""
    assert _normalize_text(None) == ""
    assert _normalize_text(" 北京　上海 ") == "北京上海"


def test_record_identity_keys_priority() -> None:
    # job_id present -> it is the ONLY key (title changes must not diverge).
    keys = _record_identity_keys(_adapter_record(job_id="BS_42"))
    assert keys == ("job_id:BS_42",)
    # Non-string job_id is ignored; falls back to url + title.
    keys = _record_identity_keys(_adapter_record(job_id=None))
    assert keys == ("url:https://jobs.moka.com/job/1", "title:算法工程师|北京|")
    # No url and no title -> no identity at all.
    keys = _record_identity_keys({"title": "T", "description": "D", "apply_url": None})
    assert keys == ("title:t||",)
    keys = _record_identity_keys({"title": None, "description": "D", "apply_url": None})
    assert keys == ()


def test_detail_identity_keys_priority() -> None:
    def detail(apply_url: str | None, title: str | None, locations: list[str] | None = None) -> ExtractedJobDetails:
        return ExtractedJobDetails(
            title=title,
            company_name=None,
            locations=locations or [],
            responsibilities="岗位职责：负责推荐系统。",
            requirements="",
            recruitment_types=[],
            apply_url=apply_url,
            deadline_text=None,
            confidence=1.0,
            evidence_refs=[],
            normalization_warnings=[],
        )

    assert _detail_identity_keys(detail("https://X.com/job/9/", "算法工程师")) == (
        "url:https://x.com/job/9",
    )
    assert _detail_identity_keys(detail(None, "算法工程师（校招）", ["北京"])) == (
        "title:算法工程师|北京|",
    )
    assert _detail_identity_keys(detail(None, None)) == ()


def test_title_identity_scopes_by_location_and_recruitment_type() -> None:
    # sorted(set(...)) orders by codepoint: 上海 (U+4E0A) < 北京 (U+5317).
    assert _title_identity("算法工程师", ["北京", "上海"], []) == (
        "title:算法工程师|上海|北京|"
    )
    assert _title_identity("算法工程师", ["北京"], ["campus"]) == (
        "title:算法工程师|北京|campus"
    )
    assert _title_identity("", ["北京"], []) == ""
    assert _title_identity(None, [], ["campus"]) == ""


def test_evidence_identity_keys_deduplicates_within_artifact() -> None:
    records = [
        _adapter_record(job_id=None, apply_url="https://jobs.moka.com/job/1", title="算法工程师"),
        _adapter_record(job_id=None, apply_url="https://jobs.moka.com/job/2", title="算法工程师"),
    ]
    text = _adapter_evidence_text(records)
    parsed = _parse_adapter_evidence(text)
    assert parsed is not None
    evidence = _evidence_metadata("observed:m", text)
    context = _context([evidence["observed_public_evidence"][0]])
    keys = _evidence_identity_keys(context, "observed:m", evidence["observed_public_evidence"][0])
    # The shared title key is emitted once (dict.fromkeys ordering preserved).
    assert keys == (
        "url:https://jobs.moka.com/job/1",
        "title:算法工程师|北京|",
        "url:https://jobs.moka.com/job/2",
    )


def test_dedup_input_rejects_blank_and_duplicate_ids() -> None:
    with pytest.raises(ValueError):
        DeduplicateObservedJobsInput(artifact_ids=["", "observed:a"])
    with pytest.raises(ValueError, match="unique"):
        DeduplicateObservedJobsInput(artifact_ids=["observed:a", "observed:a"])


def test_dedup_models_roundtrip() -> None:
    out = DeduplicateObservedJobsOutput(
        kept=["observed:a"],
        removed=[
            {"artifact_id": "observed:b", "reason": "duplicate_identity", "detail": "d"}
        ],
    )
    assert out.kept == ["observed:a"]
    assert out.removed[0].artifact_id == "observed:b"
    removal = DeduplicatedRemoval(artifact_id="c", reason="evidence_not_found", detail="d")
    assert removal.reason == "evidence_not_found"
