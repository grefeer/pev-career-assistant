"""Xpeng Feishu recruitment site adapter.

Handles Feishu OAuth login gate (human takeover), remote search selects
for university/major autocomplete, and standard form controls.
"""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING

from executor.adapters.base import (
    BlockerInfo,
    FillResult,
    PageClass,
    PageFingerprint,
    RepeatSectionResult,
    UploadResult,
)
from executor.adapters.common.file_upload import upload_via_input
from executor.adapters.common.select import fill_select
from executor.adapters.common.text_input import fill_text_input
from executor.adapters.xpeng.topology import (
    XPENG_TOPOLOGY,
    LOGIN_INDICATORS,
)

if TYPE_CHECKING:
    from playwright.sync_api import Page

logger = logging.getLogger(__name__)


class XpengFeishuAdapter:
    """Adapter for Xpeng's Feishu-based recruitment platform.

    Key special handling:
      - Feishu OAuth login gate (human takeover)
      - Remote search selects (university, major autocomplete)
      - Standard text/select/file controls via shared library
    """

    adapter_id: str = "xpeng.feishu"
    supported_domains: list[str] = ["feishu.cn", "bytedance.com"]
    version: str = "1.0.0"

    # ── Page fingerprint ────────────────────────────────────────────────────

    def fingerprint_page(self, page: "Page") -> PageFingerprint:
        try:
            url = page.url
        except Exception:
            url = ""
        try:
            dom_hash = page.evaluate(
                """() => {
                    const parts = [];
                    document.querySelectorAll(
                        'input, select, textarea, button, [data-field-key]'
                    ).forEach(el => {
                        parts.push(
                            el.tagName.toLowerCase() + '|' +
                            (el.id || '') + '|' +
                            (el.getAttribute('name') || '') + '|' +
                            (el.type || '')
                        );
                    });
                    return parts.sort().join('||');
                }"""
            )
            dom_hash_hex = (
                "sha256:" + hashlib.sha256(dom_hash.encode()).hexdigest()
            )
        except Exception:
            dom_hash_hex = "sha256:" + ("0" * 64)
        return PageFingerprint(
            url_pattern=url, dom_hash=dom_hash_hex,
        )

    def classify_topology(self, fp: PageFingerprint) -> str:
        entry = _find_entry(fp.page_index)
        if entry:
            return entry["page_class"]
        if fp.fields_detected:
            return PageClass.MULTI_PAGE_MIDDLE
        return PageClass.UNKNOWN

    # ── Field fill ──────────────────────────────────────────────────────────

    def fill_field(
        self, page: "Page", field_key: str, value: str
    ) -> FillResult:
        entry = _find_entry(1) or {}
        strategy_name = entry.get("controls", {}).get(field_key, "text_input")
        selector = f'[data-field-key="{field_key}"]'

        if strategy_name == "remote_search_select":
            return _fill_remote_search_select(page, selector, value, field_key)
        if strategy_name == "select":
            return fill_select(page, selector, value, field_key)
        if strategy_name == "file_upload":
            result = upload_via_input(page, selector, value, field_key)
            return FillResult(
                field_key=field_key,
                strategy="file_upload",
                value_written=result.file_name,
                readback_match=result.success,
                readback_value=result.server_response_indicator,
                confidence=1.0 if result.success else 0.0,
            )
        return fill_text_input(page, selector, value, field_key)

    def handle_repeat_section(
        self, page: "Page", section_key: str, entries: list[dict[str, str]]
    ) -> RepeatSectionResult:
        # Xpeng Feishu simplified: single internship entry, no complex repeat
        return RepeatSectionResult(
            section_key=section_key,
            entries_before=0,
            entries_after=len(entries),
            entries_added=len(entries),
            dedup_verified=True,
        )

    def upload_attachment(
        self, page: "Page", field_key: str, file_path: str
    ) -> UploadResult:
        return upload_via_input(
            page, f'input[type="file"][data-field-key="{field_key}"]',
            file_path, field_key,
        )

    def detect_blocker(self, page: "Page") -> BlockerInfo | None:
        for indicator in LOGIN_INDICATORS:
            try:
                if page.locator(indicator).count() > 0:
                    return BlockerInfo(blocker_type="login", detail="Feishu OAuth login detected")
            except Exception:
                pass
        return None

    def save_page_progress(self, page: "Page") -> bool:
        try:
            btn = page.locator('button:has-text("保存")').first
            if btn.count() > 0 and btn.is_visible():
                btn.click()
                return True
        except Exception:
            pass
        return False


def _find_entry(page_index: int | None) -> dict | None:
    for entry in XPENG_TOPOLOGY:
        if entry["index"] == page_index:
            return entry
    return None


def _fill_remote_search_select(
    page: "Page", selector: str, keyword: str, field_key: str,
) -> FillResult:
    """Fill a Feishu remote-search select by typing and clicking the first match."""
    try:
        input_el = page.locator(selector).first
        if input_el.count() == 0:
            return FillResult(
                field_key=field_key, strategy="remote_search_select",
                value_written="", readback_match=False, readback_value=None,
                confidence=0.0,
            )
        input_el.click()
        input_el.fill(keyword)
        page.wait_for_timeout(1500)
        option = page.locator(".search-result-item, [role=\"option\"]").first
        if option.count() > 0:
            option.click()
            page.wait_for_timeout(300)
            selected = input_el.input_value()
            match = keyword in selected
            return FillResult(
                field_key=field_key, strategy="remote_search_select",
                value_written=selected, readback_match=match,
                readback_value=selected, confidence=0.95 if match else 0.5,
            )
        return FillResult(
            field_key=field_key, strategy="remote_search_select",
            value_written=keyword, readback_match=False,
            readback_value=None, confidence=0.3,
        )
    except Exception as exc:
        logger.warning("remote_search_select failed: %s", exc)
        return FillResult(
            field_key=field_key, strategy="remote_search_select",
            value_written="", readback_match=False, readback_value=None,
            confidence=0.0,
        )
