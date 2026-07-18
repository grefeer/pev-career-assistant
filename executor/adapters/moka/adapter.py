"""DJI Moka site adapter — implements SiteAdapter Protocol.

This adapter understands Moka's recruitment form structure and delegates
to sub-modules for topology, controls, repeat sections, and attachments.
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
from executor.adapters.moka.attachments import upload_attachment as _upload
from executor.adapters.moka.controls import FILL_STRATEGIES
from executor.adapters.moka.repeat import handle_repeat_section
from executor.adapters.moka.topology import (
    FIELD_LABEL_PATTERNS,
    NEXT_BUTTON_SELECTORS,
    SAVE_BUTTON_SELECTORS,
    SUBMIT_BUTTON_SELECTORS,
    find_page_entry,
)

if TYPE_CHECKING:
    from playwright.sync_api import Page

logger = logging.getLogger(__name__)

# Blocked label tokens — same as executor safety gate
_FINAL_TOKENS = frozenset({
    "\u63d0\u4ea4",       # 提交
    "\u6295\u9012",       # 投递
    "submit",
    "confirmapplication",
})


class MokaSiteAdapter:
    """Adapter for DJI's Moka-based recruitment platform.

    Covers 6-page forms with repeatable sections (education, work,
    projects), standard and rich-text controls, and file attachments.

    The adapter NEVER auto-clicks submit buttons — that invariant is
    enforced by the executor engine's safety gate.
    """

    adapter_id: str = "moka.dji"
    supported_domains: list[str] = ["moka.com", "mokahr.com", "zhaopin.dji.com"]
    version: str = "1.0.0"

    # ── Page fingerprint & topology ──────────────────────────────────────────

    def fingerprint_page(self, page: "Page") -> PageFingerprint:
        """Build a redacted page fingerprint from DOM structure."""
        try:
            url = page.url
        except Exception:
            url = ""
        # Compute a simplified DOM structure hash
        try:
            dom_hash = page.evaluate(
                """() => {
                    const parts = [];
                    const els = document.querySelectorAll(
                        'input, select, textarea, button, [data-field-key], [contenteditable]'
                    );
                    els.forEach(el => {
                        parts.push(
                            el.tagName.toLowerCase() + '|' +
                            (el.id || '') + '|' +
                            (el.getAttribute('name') || '') + '|' +
                            (el.type || '') + '|' +
                            (el.getAttribute('data-field-key') || '') + '|' +
                            (el.getAttribute('data-action-kind') || '')
                        );
                    });
                    return parts.sort().join('||');
                }"""
            )
            dom_hash_hex = (
                "sha256:"
                + hashlib.sha256(dom_hash.encode()).hexdigest()
            )
        except Exception:
            dom_hash_hex = "sha256:" + ("0" * 64)

        # Detect submit / ambiguous buttons
        has_submit = False
        has_ambiguous = False
        for sel in SUBMIT_BUTTON_SELECTORS:
            try:
                if page.locator(sel).count() > 0:
                    has_submit = True
                    break
            except Exception:
                pass

        # Detect fields on page
        fields: list[str] = []
        try:
            for key, patterns in FIELD_LABEL_PATTERNS.items():
                for pattern in patterns:
                    try:
                        if page.locator(f":has-text('{pattern}')").count() > 0:
                            fields.append(key)
                            break
                    except Exception:
                        pass
        except Exception:
            pass

        # Estimate page index from URL or step indicator
        page_index: int | None = None
        total_pages: int | None = None
        try:
            step_el = page.locator(
                ".step-indicator, .progress-text, [data-step-index]"
            ).first
            if step_el.count() > 0:
                text = step_el.text_content() or ""
                import re
                m = re.search(r"(\d+)\s*/\s*(\d+)", text)
                if m:
                    page_index = int(m.group(1))
                    total_pages = int(m.group(2))
        except Exception:
            pass

        return PageFingerprint(
            url_pattern=url,
            dom_hash=dom_hash_hex,
            page_index=page_index,
            total_pages=total_pages,
            has_submit_button=has_submit,
            has_ambiguous_button=has_ambiguous,
            fields_detected=fields,
        )

    def classify_topology(self, fp: PageFingerprint) -> str:
        """Classify the current page based on its fingerprint."""
        entry = find_page_entry(fp.page_index, fp.dom_hash)
        if entry:
            return entry["page_class"]
        # Heuristic: if submit button present and no navigation, it's the last page
        if fp.has_submit_button:
            # Check for next button — if absent, it's likely the final page
            try:
                for sel in NEXT_BUTTON_SELECTORS:
                    # Can't evaluate here — just use page_index
                    pass
            except Exception:
                pass
            return PageClass.MULTI_PAGE_LAST
        if fp.fields_detected:
            return PageClass.MULTI_PAGE_MIDDLE
        return PageClass.UNKNOWN

    # ── Field fill ───────────────────────────────────────────────────────────

    def fill_field(
        self, page: "Page", field_key: str, value: str
    ) -> FillResult:
        """Fill a single field using the appropriate strategy."""
        entry = find_page_entry(
            None
        )  # try to find page entry for control type
        strategy_name = "text_input"  # default
        selector = f'[data-field-key="{field_key}"]'

        if entry:
            strategy_name = entry.get("controls", {}).get(
                field_key, "text_input"
            )

        # Build selector: prefer data-field-key, then name/id match
        if page.locator(selector).count() == 0:
            selector = f'[name*="{field_key}"]'
        if page.locator(selector).count() == 0:
            selector = f'input[id*="{field_key}"]'

        strategy = FILL_STRATEGIES.get(strategy_name, FILL_STRATEGIES["text_input"])
        return strategy(page, selector, value, field_key=field_key)

    # ── Repeat sections ──────────────────────────────────────────────────────

    def handle_repeat_section(
        self,
        page: "Page",
        section_key: str,
        entries: list[dict[str, str]],
    ) -> RepeatSectionResult:
        """Delegate to the repeat section handler."""
        return handle_repeat_section(page, section_key, entries)

    # ── Attachments ──────────────────────────────────────────────────────────

    def upload_attachment(
        self, page: "Page", field_key: str, file_path: str
    ) -> UploadResult:
        """Delegate to the attachment upload handler."""
        return _upload(page, field_key, file_path)

    # ── Blocker detection ────────────────────────────────────────────────────

    def detect_blocker(self, page: "Page") -> BlockerInfo | None:
        """Check for login gates, captchas, or risk warnings."""
        # Login gate check
        try:
            login_indicators = [
                'input[type="password"]',
                ':has-text("登录")',
                ':has-text("扫码")',
                ':has-text("验证码")',
                'iframe[src*="captcha"]',
            ]
            for indicator in login_indicators:
                if page.locator(indicator).count() > 0:
                    return BlockerInfo(
                        blocker_type="login",
                        detail="Login or captcha page detected",
                    )
        except Exception:
            pass

        # Risk warning check
        try:
            risk_texts = [
                "操作频繁",
                "请稍后再试",
                "账号异常",
                "需要验证",
                "too many requests",
            ]
            body_text = page.locator("body").inner_text()
            for risk in risk_texts:
                if risk in body_text:
                    return BlockerInfo(
                        blocker_type="risk_warning",
                        detail=f"Risk warning: {risk}",
                    )
        except Exception:
            pass

        return None

    # ── Save page progress ───────────────────────────────────────────────────

    def save_page_progress(self, page: "Page") -> bool:
        """Click the save/draft button if present."""
        for sel in SAVE_BUTTON_SELECTORS:
            try:
                btn = page.locator(sel).first
                if btn.count() > 0 and btn.is_visible():
                    btn.click()
                    page.wait_for_timeout(1000)
                    return True
            except Exception:
                continue
        return False
