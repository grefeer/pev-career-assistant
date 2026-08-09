"""Smartsheet-backed career-record evidence tool (PEV job-discovery Skill).

Covers the keyword/time-filtered ``query-career-sheet-records`` tool, its
mcporter bridge degradation, and the sheet-first executor instruction
ordering. Bridge calls go through the module-level seam, never the network.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from backend.app.services.career_skills import career_sheets
from backend.app.services.career_skills.career_sheets import (
    CareerSheetRecord,
    QueryCareerSheetRecordsInput,
    SHEET_REGISTRY,
    SheetQueryError,
    _default_list_records_impl,
    _field_map,
    _content_hash_of,
    _normalize_field_value,
    _pick_field,
    _record_matches,
    _scan_sheet,
    _updated_within_days,
    query_career_sheet_records,
    _MAX_OUTPUT_RECORDS,
    _MAX_RECORDS_SCANNED_PER_SHEET,
    _PAGE_SIZE,
)
from backend.app.services.agent_runtime.tool_context import ToolContext

_NOW_MS = int(time.time() * 1000)


# ------------------------------------------------------------- field helpers
def test_normalize_field_value_flattens_every_shape() -> None:
    assert _normalize_field_value(None) is None
    assert _normalize_field_value("x") == "x"
    assert _normalize_field_value(42) == "42"
    assert _normalize_field_value(3.5) == "3.5"
    assert _normalize_field_value(["a"]) is None  # not a dict
    assert (
        _normalize_field_value({"items": [{"text": "a", "link": "https://u"}, {"text": ""}, "junk"]})
        == "a https://u"
    )
    assert _normalize_field_value({"items": []}) is None
    assert _normalize_field_value({"items": "not-list", "string_value": "s"}) == "s"
    assert _normalize_field_value({"string_value": None}) is None


def test_field_map_falls_through_value_kinds_and_skips_garbage() -> None:
    assert _field_map([]) == {}
    assert _field_map(["junk", {}, {"field": ""}]) == {}
    assert _field_map([{"field": "企业", "text_value": "字节"}]) == {"企业": "字节"}
    assert _field_map(
        [{"field": "链接", "text_value": None, "url_value": {"items": [{"link": "u"}]}}]
    ) == {"链接": "u"}
    assert _field_map([{"field": "类型", "option_value": "内推"}]) == {"类型": "内推"}
    assert _field_map([{"field": "备注", "string_value": "s"}]) == {"备注": "s"}
    assert (
        _field_map(
            [{"field": "空", "text_value": None, "url_value": None, "option_value": None, "string_value": None}]
        )
        == {}
    )


def test_pick_field_matches_needle_substring_or_returns_none() -> None:
    fields = {"企业名称": "字节", "行业类型": "互联网"}
    assert _pick_field(fields, "企业", "公司") == "字节"
    assert _pick_field(fields, "不存在") is None


# ------------------------------------------------------------- time filter
def test_updated_within_days_accepts_ms_s_and_date_forms() -> None:
    assert _updated_within_days(None, None) is True
    assert _updated_within_days("", 3) is False
    assert _updated_within_days(None, 3) is False
    assert _updated_within_days("garbage", 3) is False
    one_day_ago_ms = _NOW_MS - 86_400_000
    assert _updated_within_days(str(one_day_ago_ms), 3) is True
    assert _updated_within_days(str(_NOW_MS - 10 * 86_400_000), 3) is False
    assert _updated_within_days(str(int(time.time())), 3) is True
    assert _updated_within_days(datetime.now(timezone.utc).strftime("%Y-%m-%d"), 3) is True
    assert _updated_within_days("2026-08-04 09:00:00", None) is True
    # recent_days=0 keeps only future-dated updates.
    assert _updated_within_days(str(_NOW_MS + 86_400_000), 0) is True
    assert _updated_within_days(str(_NOW_MS), 0) is False


# ------------------------------------------------------------- match filter
def test_record_matches_requires_every_provided_keyword_group() -> None:
    fields = {"企业名称": "字节跳动", "行业类型": "互联网", "工作地点": "北京", "整体文案": "AI Agent 开发工程师"}
    assert _record_matches(fields, ["字节"], ["agent"], ["北京"]) is True
    assert _record_matches(fields, ["字节"], [], []) is True
    assert _record_matches(fields, [], [], []) is True
    assert _record_matches(fields, ["腾讯"], [], []) is False
    assert _record_matches(fields, ["字节"], ["算法"], []) is False
    assert _record_matches(fields, ["字节"], ["agent"], ["上海"]) is False
    # Case-insensitive: haystack and keywords are lowercased; role keywords
    # also match the summary/文案 text, not only the company field.
    assert _record_matches(fields, [], ["AI AGENT"], []) is True


# ------------------------------------------------------------- scan + handler
def _entry(**fields: str) -> dict:
    return {"field_values": [{"field": k, "text_value": v} for k, v in fields.items()]}


def _matching_entry(now_ms: int = _NOW_MS) -> dict:
    return _entry(
        **{
            "企业名称": "字节跳动",
            "内推链接": "https://job.example/1",
            "行业类型": "互联网",
            "工作地点": "北京",
            "招聘类型": "内推",
            "更新时间": str(now_ms),
            "整体文案": "AI Agent 开发工程师" + "x" * 300,
        }
    )


def test_scan_sheet_appends_matching_records_and_normalizes_fields(monkeypatch) -> None:
    responses = iter(
        [
            {"records": [_matching_entry()], "has_more": False},
        ]
    )
    monkeypatch.setattr(career_sheets, "_list_records_impl", lambda *a: next(responses))
    output: list[CareerSheetRecord] = []
    scanned, stopped = _scan_sheet(SHEET_REGISTRY[0], QueryCareerSheetRecordsInput(), output)
    assert scanned == 1
    assert stopped is False
    assert len(output) == 1
    record = output[0]
    assert record.company_name == "字节跳动"
    assert record.apply_url == "https://job.example/1"
    assert record.industry == "互联网"
    assert record.location == "北京"
    assert record.recruitment_type == "内推"
    assert record.updated_at == str(_NOW_MS)
    assert record.sheet_name == SHEET_REGISTRY[0]["name"]
    assert len(record.raw_summary) == 200  # truncated
    assert record.prior_metadata is not None
    assert record.prior_metadata.company_name == "字节跳动"
    assert record.prior_metadata.apply_url == "https://job.example/1"
    assert record.prior_metadata.update_time == str(_NOW_MS)


def test_scan_sheet_prior_metadata_carries_referral_code(monkeypatch) -> None:
    entry = _entry(
        **{
            "企业名称": "字节跳动",
            "内推链接": "https://job.example/1",
            "内推码(区分大小写)": "ABC123",
            "更新时间": str(_NOW_MS),
        }
    )
    monkeypatch.setattr(
        career_sheets, "_list_records_impl", lambda *a: {"records": [entry], "has_more": False}
    )
    output: list[CareerSheetRecord] = []
    _scan_sheet(SHEET_REGISTRY[0], QueryCareerSheetRecordsInput(), output)
    prior = output[0].prior_metadata
    assert prior is not None
    assert prior.referral_code == "ABC123"
    # The link column must not double-capture the referral code (or vice versa).
    assert prior.apply_url == "https://job.example/1"
    assert output[0].apply_url == "https://job.example/1"


def test_scan_sheet_referral_code_never_leaks_into_apply_url(monkeypatch) -> None:
    # A record with only a referral code column has no apply URL at all; the
    # mapping must not fall back to the code as if it were a link.
    entry = _entry(**{"企业名称": "腾讯", "内推码": "TX123", "更新时间": str(_NOW_MS)})
    monkeypatch.setattr(
        career_sheets, "_list_records_impl", lambda *a: {"records": [entry], "has_more": False}
    )
    output: list[CareerSheetRecord] = []
    _scan_sheet(SHEET_REGISTRY[0], QueryCareerSheetRecordsInput(), output)
    record = output[0]
    assert record.apply_url is None
    assert record.prior_metadata is not None
    assert record.prior_metadata.referral_code == "TX123"
    assert record.prior_metadata.apply_url is None


def test_scan_sheet_binds_apply_url_and_content_hash(monkeypatch) -> None:
    monkeypatch.setattr(
        career_sheets,
        "_list_records_impl",
        lambda *a: {"records": [_matching_entry()], "has_more": False},
    )
    first: list[CareerSheetRecord] = []
    _scan_sheet(SHEET_REGISTRY[0], QueryCareerSheetRecordsInput(), first)
    record = first[0]
    assert record.source_url == "https://job.example/1"  # apply_url is the source
    assert isinstance(record.content_hash, str)
    assert len(record.content_hash) == 64
    # Deterministic: identical sheet content yields the identical hash.
    second: list[CareerSheetRecord] = []
    _scan_sheet(SHEET_REGISTRY[0], QueryCareerSheetRecordsInput(), second)
    assert second[0].content_hash == record.content_hash
    # The hash covers sheet-carried content, not the derived binding fields.
    assert record.content_hash != _content_hash_of({})


def test_scan_sheet_record_without_apply_url_stays_unbound(monkeypatch) -> None:
    entry = _entry(**{"企业名称": "腾讯", "内推码": "TX123", "更新时间": str(_NOW_MS)})
    monkeypatch.setattr(
        career_sheets, "_list_records_impl", lambda *a: {"records": [entry], "has_more": False}
    )
    output: list[CareerSheetRecord] = []
    _scan_sheet(SHEET_REGISTRY[0], QueryCareerSheetRecordsInput(), output)
    record = output[0]
    assert record.apply_url is None
    # No apply URL -> nothing to bind -> the runtime persistence loop skips it.
    assert record.source_url is None
    assert record.content_hash is None


def test_query_output_binds_records_evidence_and_is_deterministic(monkeypatch) -> None:
    monkeypatch.setattr(
        career_sheets,
        "_list_records_impl",
        lambda *a: {"records": [_matching_entry()], "has_more": False},
    )
    result = query_career_sheet_records(None, QueryCareerSheetRecordsInput())
    assert result.records[0].source_url == "https://job.example/1"
    assert result.source_url == "https://job.example/1"
    assert isinstance(result.content_hash, str)
    assert len(result.content_hash) == 64
    again = query_career_sheet_records(None, QueryCareerSheetRecordsInput())
    assert again.content_hash == result.content_hash
    assert again.records == result.records


def test_query_output_empty_records_falls_back_to_sheet_file_url(monkeypatch) -> None:
    monkeypatch.setattr(
        career_sheets,
        "_list_records_impl",
        lambda *a: {"records": [], "has_more": False},
    )
    result = query_career_sheet_records(None, QueryCareerSheetRecordsInput())
    assert result.records == []
    assert result.source_url == f"https://docs.qq.com/sheet/{SHEET_REGISTRY[0]['file_id']}"
    assert isinstance(result.content_hash, str)
    assert len(result.content_hash) == 64


def test_prior_metadata_model_roundtrip_and_default() -> None:
    from backend.app.services.career_skills.career_sheets import CareerSheetPriorMetadata

    record = CareerSheetRecord(sheet_name="s")
    assert record.prior_metadata is None
    prior = CareerSheetPriorMetadata(
        company_name="字节", apply_url="https://u", referral_code="C", update_time="t"
    )
    record = CareerSheetRecord(
        sheet_name="s",
        prior_metadata={
            "company_name": "字节",
            "apply_url": "https://u",
            "referral_code": "C",
            "update_time": "t",
        },
    )
    assert record.prior_metadata == prior


def test_scan_sheet_skips_undated_stale_and_non_matching_records(monkeypatch) -> None:
    stale = _entry(**{"企业名称": "腾讯", "更新时间": str(_NOW_MS - 10 * 86_400_000)})
    undated = _entry(**{"企业名称": "字节跳动"})
    wrong_company = _entry(**{"企业名称": "百度", "更新时间": str(_NOW_MS)})
    responses = iter([{"records": ["junk", stale, undated, wrong_company], "has_more": False}])
    monkeypatch.setattr(career_sheets, "_list_records_impl", lambda *a: next(responses))
    output: list[CareerSheetRecord] = []
    scanned, _ = _scan_sheet(
        SHEET_REGISTRY[0],
        QueryCareerSheetRecordsInput(company_keywords=["字节"], recent_days=7),
        output,
    )
    assert scanned == 4  # every row counts toward the scan cap, even skipped ones
    assert output == []  # stale, undated and wrong-company rows all filtered out


def test_scan_sheet_tolerates_malformed_records_payload(monkeypatch) -> None:
    monkeypatch.setattr(
        career_sheets,
        "_list_records_impl",
        lambda *a: {"records": "not-a-list", "has_more": False},
    )
    output: list[CareerSheetRecord] = []
    scanned, stopped = _scan_sheet(SHEET_REGISTRY[0], QueryCareerSheetRecordsInput(), output)
    assert scanned == 0
    assert stopped is False
    assert output == []


def test_scan_sheet_hits_per_sheet_scan_cap(monkeypatch) -> None:
    # Undated rows with a recency window never match, so the loop must burn
    # full pages until the per-sheet scan cap stops it.
    page = {"records": [_entry()] * _PAGE_SIZE, "has_more": True, "next": 0}
    monkeypatch.setattr(career_sheets, "_list_records_impl", lambda *a: dict(page))
    output: list[CareerSheetRecord] = []
    scanned, stopped = _scan_sheet(
        SHEET_REGISTRY[0], QueryCareerSheetRecordsInput(recent_days=7), output
    )
    assert scanned == _MAX_RECORDS_SCANNED_PER_SHEET
    assert stopped is True


def test_scan_sheet_stops_at_output_cap_without_scan_cap(monkeypatch) -> None:
    page = {"records": [_matching_entry()] * 25, "has_more": True, "next": 0}
    monkeypatch.setattr(career_sheets, "_list_records_impl", lambda *a: dict(page))
    output: list[CareerSheetRecord] = []
    scanned, stopped = _scan_sheet(SHEET_REGISTRY[0], QueryCareerSheetRecordsInput(), output)
    assert len(output) == _MAX_OUTPUT_RECORDS
    assert scanned == 25
    assert stopped is False


def test_scan_sheet_follows_next_offset_or_defaults_to_page_size(monkeypatch) -> None:
    calls: list[int] = []

    def fake(file_id: str, sheet_id: str, limit: int, offset: int) -> dict:
        calls.append(offset)
        if len(calls) == 1:
            return {"records": ["junk", "junk", "junk"], "has_more": True, "next": "not-an-int"}
        return {"records": [], "has_more": False}

    monkeypatch.setattr(career_sheets, "_list_records_impl", fake)
    output: list[CareerSheetRecord] = []
    scanned, stopped = _scan_sheet(SHEET_REGISTRY[0], QueryCareerSheetRecordsInput(), output)
    assert calls == [0, 3]  # non-int ``next`` falls back to offset + page size
    assert scanned == 3
    assert stopped is False


def test_query_career_sheet_records_queries_all_sheets_and_echoes_query(monkeypatch) -> None:
    monkeypatch.setattr(
        career_sheets,
        "_list_records_impl",
        lambda *a: {"records": [], "has_more": False},
    )
    result = query_career_sheet_records(
        ToolContext(user_id="u", run_id="r"),
        QueryCareerSheetRecordsInput(company_keywords=["字节"], recent_days=7),
    )
    assert result.records == []
    assert result.matched_count == 0
    assert result.sheets_queried == len(SHEET_REGISTRY)
    assert result.truncated is False
    assert result.query == {
        "company_keywords": ["字节"],
        "role_keywords": [],
        "location_keywords": [],
        "recent_days": 7,
    }


def test_query_career_sheet_records_stops_early_once_output_cap_is_reached(monkeypatch) -> None:
    monkeypatch.setattr(
        career_sheets,
        "_list_records_impl",
        lambda *a: {"records": [_matching_entry()] * 25, "has_more": False},
    )
    result = query_career_sheet_records(None, QueryCareerSheetRecordsInput())
    assert len(result.records) == _MAX_OUTPUT_RECORDS
    assert result.sheets_queried == 1  # later sheets never opened
    assert result.truncated is True
    assert result.scanned_count == 25


def test_query_career_sheet_records_flags_scan_cap_truncation(monkeypatch) -> None:
    monkeypatch.setattr(
        career_sheets,
        "_list_records_impl",
        lambda *a: {"records": [_entry()] * _PAGE_SIZE, "has_more": True, "next": 0},
    )
    result = query_career_sheet_records(None, QueryCareerSheetRecordsInput(recent_days=7))
    assert result.records == []
    assert result.truncated is True
    assert result.scanned_count == _MAX_RECORDS_SCANNED_PER_SHEET * len(SHEET_REGISTRY)


# ------------------------------------------------------------- bridge
def test_default_list_records_impl_reports_unavailable_bridge(monkeypatch) -> None:
    monkeypatch.setattr(career_sheets.shutil, "which", lambda *a: None)
    with pytest.raises(SheetQueryError, match="sheet_bridge_unavailable"):
        _default_list_records_impl("f", "s", 50, 0)


def test_default_list_records_impl_parses_json_via_cmd_shim(monkeypatch) -> None:
    which = lambda *a: "C:\\tools\\mcporter.cmd"  # noqa: E731
    monkeypatch.setattr(career_sheets.shutil, "which", which)
    captured: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured.append(cmd)
        return SimpleNamespace(returncode=0, stdout='{"records": [1]}')

    monkeypatch.setattr(career_sheets.subprocess, "run", fake_run)
    assert _default_list_records_impl("file", "sheet", 50, 100) == {"records": [1]}
    assert captured[0][:2] == ["cmd", "/c"]  # CreateProcess cannot run .CMD shims


def test_default_list_records_impl_fails_closed_on_bad_exit_code(monkeypatch) -> None:
    monkeypatch.setattr(career_sheets.shutil, "which", lambda *a: "mcporter")
    monkeypatch.setattr(
        career_sheets.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout="traceback..."),
    )
    with pytest.raises(SheetQueryError, match="sheet_call_failed"):
        _default_list_records_impl("f", "s", 50, 0)


def test_default_list_records_impl_fails_closed_on_invalid_json(monkeypatch) -> None:
    monkeypatch.setattr(career_sheets.shutil, "which", lambda *a: "mcporter")
    monkeypatch.setattr(
        career_sheets.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="not json"),
    )
    with pytest.raises(SheetQueryError, match="sheet_call_failed"):
        _default_list_records_impl("f", "s", 50, 0)


def test_default_list_records_impl_fails_closed_on_spawn_and_timeout(monkeypatch) -> None:
    monkeypatch.setattr(career_sheets.shutil, "which", lambda *a: "mcporter")

    def raise_oserror(*a, **k):
        raise OSError("spawn failed")

    monkeypatch.setattr(career_sheets.subprocess, "run", raise_oserror)
    with pytest.raises(SheetQueryError, match="sheet_call_failed"):
        _default_list_records_impl("f", "s", 50, 0)

    def raise_timeout(*a, **k):
        raise career_sheets.subprocess.TimeoutExpired("mcporter", 30)

    monkeypatch.setattr(career_sheets.subprocess, "run", raise_timeout)
    with pytest.raises(SheetQueryError, match="sheet_call_failed"):
        _default_list_records_impl("f", "s", 50, 0)


def test_sheet_error_code_is_stable_non_sensitive() -> None:
    assert SheetQueryError("sheet_call_failed").code == "sheet_call_failed"
    assert str(SheetQueryError("x")) == "x"


# ------------------------------------------------------------- executor order
def test_executor_instruction_is_sheet_first_search_fallback() -> None:
    from backend.app.services.agent_runtime.executor_agent import _EXECUTOR_INSTRUCTION

    assert _EXECUTOR_INSTRUCTION.index("query-career-sheet-records") < _EXECUTOR_INSTRUCTION.index(
        "public-job search tool"
    )
    assert "Only when the sheet query returns no matching records" in _EXECUTOR_INSTRUCTION


def test_executor_instruction_constructs_official_careers_url_after_empty_search() -> None:
    from backend.app.services.agent_runtime.executor_agent import _EXECUTOR_INSTRUCTION

    # An empty search must not dead-end at "ask the user": for a company-named
    # goal the executor should first construct the official careers listing URL
    # (fetch renders JS; search engines often omit such pages). Ordering:
    # sheet-first < constructed-URL guidance < ask-the-user fallback.
    sheet_first = _EXECUTOR_INSTRUCTION.index("query-career-sheet-records")
    construct_url = _EXECUTOR_INSTRUCTION.index("listing or search URL directly")
    ask_user = _EXECUTOR_INSTRUCTION.index("ask the user for an official careers URL")
    assert sheet_first < construct_url < ask_user
    assert "careers.tencent.com/search.html?keyword=" in _EXECUTOR_INSTRUCTION


def test_verifier_instruction_accepts_sheet_evidence_without_page_text() -> None:
    # C005: a sheet-backed step is validated by its persisted records artifact
    # (content_hash + source_url binding), never by page text it cannot have.
    from backend.app.services.agent_runtime.verifier_agent import _VERIFIER_INSTRUCTION

    assert "query-career-sheet-records" in _VERIFIER_INSTRUCTION
    assert "content_hash and source_url" in _VERIFIER_INSTRUCTION
    assert (
        "RETRY_EXECUTOR a sheet-backed step solely because no page text was captured"
        in _VERIFIER_INSTRUCTION
    )
