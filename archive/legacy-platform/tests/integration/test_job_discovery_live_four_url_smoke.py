from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from backend.app.config import Settings, _literal_tencent_dotenv_values
from backend.app.services.job_discovery.deepagents_runner import run_web_navigation
from backend.app.services.job_mappers import BUILTIN_SOURCES, extract_discovery_urls
from backend.app.services.tencent_smartsheet import TencentRecord, TencentSmartsheetGateway


SOURCE_KEYS = ("tencent-27-referrals", "tencent-intern-referrals")
MAIN_PROJECT_DOTENV = Path("D:/Python/langgraph-multi-agent-career-assistant-main/.env")


pytestmark = pytest.mark.skipif(
    not os.environ.get("RUN_LIVE_TENCENT_DISCOVERY"),
    reason="set RUN_LIVE_TENCENT_DISCOVERY=1 to read real Tencent docs and public URLs",
)


def _live_tencent_token() -> str | None:
    values = _literal_tencent_dotenv_values(MAIN_PROJECT_DOTENV)
    return (
        os.environ.get("TEST_TENCENT_DOCS_TOKEN")
        or os.environ.get("TENCENT_DOCS_TOKEN")
        or values.get("test_tencent_docs_token")
        or values.get("tencent_docs_token")
    )


def _source_definition(source_key: str):
    for source in BUILTIN_SOURCES:
        if source.source_key == source_key:
            return source
    raise AssertionError(f"unknown source: {source_key}")


def _select_two_records_with_urls(
    gateway: TencentSmartsheetGateway,
    source_key: str,
) -> list[TencentRecord]:
    source = _source_definition(source_key)
    selected: list[TencentRecord] = []
    offset = 0
    while len(selected) < 2:
        page = gateway.list_records(source.file_id, source.sheet_id, offset=offset, limit=10)
        for record in page.records:
            if extract_discovery_urls(record, source_key):
                selected.append(record)
            if len(selected) == 2:
                break
        if len(selected) == 2 or not page.has_more:
            break
        offset = page.next_offset
    assert len(selected) == 2, f"{source_key} did not expose two URL records"
    return selected


def _field_text(record: TencentRecord, name: str) -> str:
    for field in record.field_values:
        if field.get("field") != name:
            continue
        parts: list[str] = []
        for key in ("text_value", "option_value", "url_value"):
            block = field.get(key) or {}
            for item in block.get("items", []) or []:
                text = item.get("text") or item.get("link")
                if text:
                    parts.append(text)
        return "、".join(parts)
    return ""


def _smoke_settings() -> Settings:
    return Settings(
        app_auth_secret="test-secret-with-at-least-32-characters",
        database_url="sqlite+pysqlite:///:memory:",
        redis_url="redis://localhost:6379/15",
        object_encryption_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        job_discovery_enabled=True,
        job_discovery_task_timeout_seconds=120,
        job_discovery_max_pages_per_task=5,
    )


def _summary(
    *,
    source_key: str,
    record: TencentRecord,
    url: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    evidence = result.get("evidence_pages") or []
    return {
        "source_key": source_key,
        "record_id": record.record_id,
        "company": _field_text(record, "公司名称") or _field_text(record, "企业名称"),
        "title": _field_text(record, "招聘岗位"),
        "url": url,
        "evidence_count": len(evidence),
        "page_count": result.get("page_count"),
        "has_error": bool(result.get("error")),
        "error": result.get("error"),
        "evidence_types": [
            item.get("evidence_type")
            for item in evidence[:10]
            if isinstance(item, dict)
        ],
        "evidence_titles": [
            item.get("title")
            for item in evidence[:10]
            if isinstance(item, dict)
        ],
    }


def test_live_tencent_four_url_web_navigation_smoke() -> None:
    token = _live_tencent_token()
    assert token

    gateway = TencentSmartsheetGateway(token=token)
    settings = _smoke_settings()
    summaries: list[dict[str, Any]] = []

    for source_key in SOURCE_KEYS:
        for record in _select_two_records_with_urls(gateway, source_key):
            url = extract_discovery_urls(record, source_key)[0]
            result = run_web_navigation(url, settings=settings)
            summaries.append(
                _summary(source_key=source_key, record=record, url=url, result=result)
            )

    assert len(summaries) == 4, json.dumps(summaries, ensure_ascii=False, indent=2)

    wechat_summaries = [
        item for item in summaries
        if item["source_key"] == "tencent-27-referrals"
    ]
    assert len(wechat_summaries) == 2
    for item in wechat_summaries:
        assert item["evidence_count"] > 0 or item["has_error"], json.dumps(
            summaries, ensure_ascii=False, indent=2
        )

    alibaba_summaries = [
        item for item in summaries
        if item["source_key"] == "tencent-intern-referrals"
    ]
    assert len(alibaba_summaries) == 2
    for item in alibaba_summaries:
        assert item["evidence_count"] > 0, json.dumps(
            summaries, ensure_ascii=False, indent=2
        )
        assert "job_detail_json" in item["evidence_types"], json.dumps(
            summaries, ensure_ascii=False, indent=2
        )
