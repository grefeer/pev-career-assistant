"""Smoke test verifying ReadGZH integration for WeChat article access.

Validates that the Web Navigation Agent can now read mp.weixin.qq.com
articles via the ReadGZH proxy service instead of hitting WeChat's
"环境异常" verification wall.

Test structure mirrors test_job_discovery_live_four_url_smoke.py:
- 4 URLs total (2 WeChat articles + 2 regular job pages)
- Uses run_web_navigation() end-to-end
- WeChat URLs are expected to return evidence after ReadGZH integration
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from backend.app.config import Settings, _literal_tencent_dotenv_values
from backend.app.services.job_discovery.deepagents_runner import (
    run_web_navigation,
    _fetch_wechat_via_readgzh,
)
from backend.app.services.job_mappers import BUILTIN_SOURCES, extract_discovery_urls
from backend.app.services.tencent_smartsheet import TencentRecord, TencentSmartsheetGateway


SOURCE_KEYS = ("tencent-27-referrals", "tencent-intern-referrals")
MAIN_PROJECT_DOTENV = Path("D:/Python/langgraph-multi-agent-career-assistant-main/.env")

# ── Optional: override WeChat test URLs via env vars ──
_TEST_WECHAT_URL_1 = os.environ.get("TEST_WECHAT_URL_1", "")
_TEST_WECHAT_URL_2 = os.environ.get("TEST_WECHAT_URL_2", "")
_TEST_REGULAR_URL_1 = os.environ.get("TEST_REGULAR_URL_1", "")
_TEST_REGULAR_URL_2 = os.environ.get("TEST_REGULAR_URL_2", "")

# Live (network + ReadGZH + Tencent docs) tests are gated individually so the
# non-live unit assertions below still run in the default suite.
_LIVE_SKIP = pytest.mark.skipif(
    not os.environ.get("RUN_LIVE_TENCENT_DISCOVERY"),
    reason="set RUN_LIVE_TENCENT_DISCOVERY=1 to run live ReadGZH smoke tests",
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


# ---------------------------------------------------------------------------
# Unit-level: direct ReadGZH API test
# ---------------------------------------------------------------------------


@_LIVE_SKIP
def test_readgzh_direct_fetch() -> None:
    """Verify ReadGZH API can fetch a WeChat article directly."""
    # Use env-provided URL or a known public article
    url = _TEST_WECHAT_URL_1
    if not url:
        pytest.skip("TEST_WECHAT_URL_1 not set — provide a WeChat article URL")

    print("\n── Testing ReadGZH direct fetch ──")
    print(f"   URL: {url}")

    text, title, error = _fetch_wechat_via_readgzh(url)

    if error:
        print(f"   ERROR: {error}")
        # Don't hard-fail on API issues — log and let integration test decide
        pytest.fail(f"ReadGZH direct fetch failed: {error}")

    print(f"   Title: {title}")
    print(f"   Text length: {len(text) if text else 0} chars")
    print(f"   Text preview: {(text or '')[:200]}...")

    assert text, "ReadGZH should return non-empty text"
    assert len(text.strip()) >= 50, f"ReadGZH text too short: {len(text.strip())} chars"
    assert "环境异常" not in text, "ReadGZH returned WeChat verification page"
    assert "完成验证后即可继续访问" not in text, "ReadGZH returned WeChat verification page"


# ---------------------------------------------------------------------------
# Integration: 4-URL smoke test via run_web_navigation
# ---------------------------------------------------------------------------


@_LIVE_SKIP
def test_live_readgzh_four_url_web_navigation_smoke() -> None:
    """4-URL smoke test: 2 WeChat articles + 2 regular job pages.

    Verifies that:
    - WeChat URLs (tencent-27-referrals) can be read via ReadGZH integration
    - Regular URLs (tencent-intern-referrals) continue to work
    - All 4 URLs return evidence or a meaningful error
    """
    token = _live_tencent_token()
    assert token, "Tencent docs token required"

    gateway = TencentSmartsheetGateway(token=token)
    settings = _smoke_settings()
    summaries: list[dict[str, Any]] = []
    wechat_urls: list[str] = []

    for source_key in SOURCE_KEYS:
        for record in _select_two_records_with_urls(gateway, source_key):
            urls = extract_discovery_urls(record, source_key)
            url = urls[0]
            if "mp.weixin.qq.com" in url:
                wechat_urls.append(url)

            print(f"\n── Navigation: {source_key} ──")
            print(f"   URL: {url}")
            print(f"   Is WeChat: {'mp.weixin.qq.com' in url}")

            result = run_web_navigation(url, settings=settings)
            summary_entry = _summary(
                source_key=source_key, record=record, url=url, result=result
            )
            summaries.append(summary_entry)

            print(f"   evidence_count: {summary_entry['evidence_count']}")
            print(f"   has_error: {summary_entry['has_error']}")
            if summary_entry["has_error"]:
                print(f"   error: {summary_entry['error']}")
            print(f"   evidence_types: {summary_entry['evidence_types']}")

    # ── Assertions ──
    assert len(summaries) == 4, json.dumps(summaries, ensure_ascii=False, indent=2)

    # WeChat articles (tencent-27-referrals): should NOW return evidence via ReadGZH
    wechat_summaries = [
        item for item in summaries
        if item["source_key"] == "tencent-27-referrals"
    ]
    assert len(wechat_summaries) == 2, (
        f"Expected 2 WeChat article summaries, got {len(wechat_summaries)}"
    )

    wechat_success_count = 0
    for item in wechat_summaries:
        if item["evidence_count"] > 0:
            wechat_success_count += 1
            print(f"\n✓ WeChat article successfully read: {item['title']}")
        elif item["has_error"]:
            # Check if error is NOT about WeChat verification
            error_msg = str(item.get("error", "")).lower()
            is_wechat_block = any(
                keyword in error_msg
                for keyword in ("环境异常", "验证", "verification", "captcha", "反爬")
            )
            if is_wechat_block:
                print(f"\n✗ WeChat article STILL blocked: {item['error']}")
            else:
                print(f"\n⚠ WeChat article has non-blocking error: {item['error']}")
                # Non-verification errors are acceptable (e.g., empty article, network)
        else:
            print("\n⚠ WeChat article returned 0 evidence (no error reported)")

    print(f"\n── WeChat ReadGZH results: {wechat_success_count}/2 success ──")

    # At least 1 WeChat article should succeed via ReadGZH
    assert wechat_success_count >= 1, (
        f"ReadGZH integration failed: 0/{len(wechat_summaries)} WeChat articles "
        f"returned evidence. Results: {json.dumps(wechat_summaries, ensure_ascii=False, indent=2)}"
    )

    # Regular job pages (tencent-intern-referrals): should still work
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


# ---------------------------------------------------------------------------
# Unit-level (non-live): Task 6 ReadGZH-failure -> manual review contract
# ---------------------------------------------------------------------------


def test_fetch_wechat_article_returns_manual_review_on_readgzh_failure() -> None:
    """Step 4: a ReadGZH failure surfaces as a valid manual-review result.

    ``fetch_wechat_article`` must never crash or silently return empty data
    when ReadGZH fails -- it returns ``needs_manual_review=True`` with the
    sanitized reason, which the worker forwards as a final (non-retried)
    manual-review outcome.
    """
    from unittest.mock import patch

    from backend.app.services.job_discovery.deepagents_runner import (
        fetch_wechat_article,
    )

    url = "https://mp.weixin.qq.com/s/readgzh-failure-fixture"
    with patch(
        "backend.app.services.job_discovery.deepagents_runner._fetch_wechat_via_readgzh",
        return_value=(None, None, "ReadGZH fetch failed: simulated timeout"),
    ):
        result = fetch_wechat_article(url)

    assert result["needs_manual_review"] is True
    assert "simulated timeout" in result["manual_review_reason"]
    assert result["url"] == url
    assert result["image_ocr_texts"] == []


def test_fetch_wechat_article_accepts_deadline_budget() -> None:
    """Step 3: the deadline budget param is accepted and threads to ReadGZH.

    With a generous budget the call still returns manual_review when ReadGZH
    fails, and the ReadGZH call is invoked with a shrunk ``readgzh_timeout``
    bounded by ``min(30, max(1.0, remaining))``.
    """
    from unittest.mock import patch

    from backend.app.services.job_discovery.deepagents_runner import (
        fetch_wechat_article,
    )

    url = "https://mp.weixin.qq.com/s/deadline-fixture"
    captured: dict = {}

    def _fake_readgzh(_url, readgzh_timeout=None):
        captured["readgzh_timeout"] = readgzh_timeout
        return (None, None, "ReadGZH fetch failed: simulated")

    with patch(
        "backend.app.services.job_discovery.deepagents_runner._fetch_wechat_via_readgzh",
        side_effect=_fake_readgzh,
    ):
        result = fetch_wechat_article(url, deadline_remaining_seconds=90)

    # With remaining ~90s, the ReadGZH timeout is min(30, max(1.0, ~90)) = 30.
    assert captured["readgzh_timeout"] == 30
    assert result["needs_manual_review"] is True
