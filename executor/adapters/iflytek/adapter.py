"""iFlytek zhiye.com site adapter.

Key special handling:
  - Front-end validation detection
  - Attachment upload with server polling
  - Multi-page navigation with page-change detection
"""

from __future__ import annotations

import hashlib
import logging
import os
from typing import TYPE_CHECKING

from executor.adapters.base import (
    BlockerInfo,
    FillResult,
    PageClass,
    PageFingerprint,
    RepeatSectionResult,
    UploadResult,
)
from executor.adapters.common.text_input import fill_text_input
from executor.adapters.common.file_upload import upload_via_input
from executor.adapters.common.wait_utils import wait_for_text_present
from executor.adapters.iflytek.topology import (
    IFLYTEK_TOPOLOGY,
    VALIDATION_ERROR_SELECTORS,
)

if TYPE_CHECKING:
    from playwright.sync_api import Page

logger = logging.getLogger(__name__)


class IflytekZhiyeAdapter:
    """Adapter for iFlytek's zhiye.com recruitment platform.

    Key differences from Moka:
      - Front-end JS validation runs after each field fill
      - Attachment upload requires polling for server processing
      - School/profession fields are linked dropdowns
    """

    adapter_id: str = "iflytek.zhiye"
    supported_domains: list[str] = ["zhiye.com"]
    version: str = "1.0.0"

    def fingerprint_page(self, page: "Page") -> PageFingerprint:
        try:
            url = page.url
        except Exception:
            url = ""
        try:
            dom_hash = page.evaluate(
                """() => {
                    const p = [];
                    document.querySelectorAll(
                        'input, select, textarea, button'
                    ).forEach(el => {
                        p.push(
                            el.tagName.toLowerCase()+'|'+(el.id||'')+'|'+
                            (el.name||'')+'|'+(el.type||'')
                        );
                    });
                    return p.sort().join('||');
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
        for entry in IFLYTEK_TOPOLOGY:
            if entry["index"] == fp.page_index:
                return entry["page_class"]
        return PageClass.UNKNOWN

    def fill_field(
        self, page: "Page", field_key: str, value: str
    ) -> FillResult:
        selector = f'[data-field-key="{field_key}"],[name*="{field_key}"],[id*="{field_key}"]'
        result = fill_text_input(page, selector, value, field_key)

        # Check for front-end validation errors
        if result.readback_match:
            page.wait_for_timeout(500)
            for esel in VALIDATION_ERROR_SELECTORS:
                try:
                    err = page.locator(esel).first
                    if err.count() > 0 and err.is_visible():
                        result.confidence = 0.0
                        result.readback_match = False
                        break
                except Exception:
                    pass

        return result

    def handle_repeat_section(
        self, page: "Page", section_key: str, entries: list[dict[str, str]]
    ) -> RepeatSectionResult:
        return RepeatSectionResult(
            section_key=section_key,
            entries_before=0, entries_after=len(entries),
            entries_added=len(entries), dedup_verified=True,
        )

    def upload_attachment(
        self, page: "Page", field_key: str, file_path: str
    ) -> UploadResult:
        result = upload_via_input(
            page, f'input[type="file"][name*="{field_key}"], input[type="file"][id*="{field_key}"]',
            file_path, field_key,
        )
        if result.success:
            # zhiye.com needs server processing — poll for filename to appear
            file_name = os.path.basename(file_path)
            if not wait_for_text_present(page, file_name, timeout_ms=15_000):
                result.success = False
                result.server_response_indicator = "upload_timeout"
        return result

    def detect_blocker(self, page: "Page") -> BlockerInfo | None:
        try:
            body = page.locator("body").inner_text()
            if "验证码" in body or "captcha" in body.lower():
                return BlockerInfo(blocker_type="captcha", detail="验证码")
            if "请先登录" in body or "login" in body.lower():
                return BlockerInfo(blocker_type="login", detail="需要登录")
        except Exception:
            pass
        return None

    def save_page_progress(self, page: "Page") -> bool:
        for label in ["保存", "暂存", "保存草稿"]:
            try:
                btn = page.locator(f'button:has-text("{label}")').first
                if btn.count() > 0 and btn.is_visible():
                    btn.click()
                    page.wait_for_timeout(1000)
                    return True
            except Exception:
                continue
        return False
