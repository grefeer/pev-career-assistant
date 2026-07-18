"""Moka control fill strategies.

Each strategy takes a Playwright Page, a CSS selector, and a value string.
It writes the value using the appropriate interaction pattern and reads back
the written value to produce a FillResult with readback verification.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from executor.adapters.base import FillResult

if TYPE_CHECKING:
    from playwright.sync_api import Page

logger = logging.getLogger(__name__)

# ── Individual fill strategies ──────────────────────────────────────────────────


def fill_text_input(
    page: "Page", selector: str, value: str, field_key: str = ""
) -> FillResult:
    """Fill a standard <input type="text"> or <textarea> field."""
    locator = page.locator(selector).first
    if locator.count() == 0:
        return FillResult(
            field_key=field_key,
            strategy="text_input",
            value_written="",
            readback_match=False,
            readback_value=None,
            confidence=0.0,
        )
    try:
        locator.click()
        locator.fill("")
        locator.fill(value)
        page.wait_for_timeout(200)  # let JS validation settle
        readback = locator.input_value()
    except Exception as exc:
        logger.warning("text_input fill failed: %s", exc)
        return FillResult(
            field_key=field_key,
            strategy="text_input",
            value_written=value,
            readback_match=False,
            readback_value=None,
            confidence=0.0,
        )
    match = readback.strip() == value.strip()
    return FillResult(
        field_key=field_key,
        strategy="text_input",
        value_written=value,
        readback_match=match,
        readback_value=readback,
        confidence=1.0 if match else 0.5,
    )


def fill_select(
    page: "Page", selector: str, value: str, field_key: str = ""
) -> FillResult:
    """Fill a <select> dropdown by visible label."""
    locator = page.locator(selector).first
    if locator.count() == 0:
        return FillResult(
            field_key=field_key,
            strategy="select",
            value_written="",
            readback_match=False,
            readback_value=None,
            confidence=0.0,
        )
    try:
        locator.select_option(label=value)
        page.wait_for_timeout(200)
        selected = locator.evaluate(
            "el => el.options[el.selectedIndex]?.text || ''"
        )
    except Exception as exc:
        logger.warning("select fill failed: %s", exc)
        return FillResult(
            field_key=field_key,
            strategy="select",
            value_written=value,
            readback_match=False,
            readback_value=None,
            confidence=0.0,
        )
    match = value in str(selected)
    return FillResult(
        field_key=field_key,
        strategy="select",
        value_written=value,
        readback_match=match,
        readback_value=str(selected),
        confidence=1.0 if match else 0.3,
    )


def fill_date_picker(
    page: "Page", selector: str, value: str, field_key: str = ""
) -> FillResult:
    """Fill a date input by typing the value directly."""
    locator = page.locator(selector).first
    if locator.count() == 0:
        return FillResult(
            field_key=field_key,
            strategy="date_picker",
            value_written="",
            readback_match=False,
            readback_value=None,
            confidence=0.0,
        )
    try:
        locator.click()
        locator.fill("")
        locator.fill(value)
        page.wait_for_timeout(300)
        readback = locator.input_value()
    except Exception as exc:
        logger.warning("date_picker fill failed: %s", exc)
        return FillResult(
            field_key=field_key,
            strategy="date_picker",
            value_written=value,
            readback_match=False,
            readback_value=None,
            confidence=0.0,
        )
    match = value in readback or readback in value
    return FillResult(
        field_key=field_key,
        strategy="date_picker",
        value_written=value,
        readback_match=match,
        readback_value=readback,
        confidence=0.9 if match else 0.3,
    )


def fill_rich_text(
    page: "Page", selector: str, value: str, field_key: str = ""
) -> FillResult:
    """Fill a rich-text / contenteditable field.

    Tries: direct contenteditable, then iframe body, then fallback textarea.
    """
    # Try contenteditable first
    editable = page.locator(selector).first
    if editable.count() == 0:
        # Try iframe-based editor
        try:
            frame = page.frame_locator("iframe").first
            body = frame.locator("body")
            if body.count() > 0:
                body.click()
                body.fill(value)
                page.wait_for_timeout(300)
                readback = body.inner_text()
                match = value.strip()[:50] in readback
                return FillResult(
                    field_key=field_key,
                    strategy="rich_text",
                    value_written=value,
                    readback_match=match,
                    readback_value=readback[:200],
                    confidence=0.9 if match else 0.3,
                )
        except Exception:
            pass
        return FillResult(
            field_key=field_key,
            strategy="rich_text",
            value_written="",
            readback_match=False,
            readback_value=None,
            confidence=0.0,
        )
    try:
        editable.click()
        # Clear existing content
        editable.evaluate("el => el.innerHTML = ''")
        editable.fill(value)
        page.wait_for_timeout(300)
        readback = editable.inner_text()
    except Exception as exc:
        logger.warning("rich_text fill failed: %s", exc)
        return FillResult(
            field_key=field_key,
            strategy="rich_text",
            value_written=value,
            readback_match=False,
            readback_value=None,
            confidence=0.0,
        )
    match = value.strip()[:50] in readback
    return FillResult(
        field_key=field_key,
        strategy="rich_text",
        value_written=value,
        readback_match=match,
        readback_value=readback[:200],
        confidence=0.9 if match else 0.3,
    )


def fill_multi_select(
    page: "Page", selector: str, value: str, field_key: str = ""
) -> FillResult:
    """Select one or more options in a custom multi-select (tag) component.

    Values are comma-separated; each tag is clicked individually.
    """
    tags = [t.strip() for t in value.split(",") if t.strip()]
    selected: list[str] = []
    try:
        input_el = page.locator(selector).first
        if input_el.count() == 0:
            return FillResult(
                field_key=field_key,
                strategy="multi_select",
                value_written="",
                readback_match=False,
                readback_value=None,
                confidence=0.0,
            )
        for tag in tags:
            input_el.click()
            input_el.fill(tag)
            page.wait_for_timeout(500)
            option = page.locator(f'[role="option"]:has-text("{tag}")').first
            if option.count() > 0:
                option.click()
                page.wait_for_timeout(200)
                selected.append(tag)
            else:
                option2 = page.locator(f"text={tag}").first
                if option2.count() > 0:
                    option2.click()
                    page.wait_for_timeout(200)
                    selected.append(tag)
    except Exception as exc:
        logger.warning("multi_select fill failed: %s", exc)
        return FillResult(
            field_key=field_key,
            strategy="multi_select",
            value_written=",".join(selected),
            readback_match=False,
            readback_value=None,
            confidence=0.0 if not selected else 0.5,
        )

    all_selected = len(selected) == len(tags)
    return FillResult(
        field_key=field_key,
        strategy="multi_select",
        value_written=",".join(selected),
        readback_match=all_selected,
        readback_value=",".join(selected),
        confidence=1.0 if all_selected else 0.7,
    )


def fill_radio(
    page: "Page", selector: str, value: str, field_key: str = ""
) -> FillResult:
    """Click a radio button matching the given value."""
    try:
        radio = page.locator(selector).first
        if radio.count() == 0:
            return FillResult(
                field_key=field_key,
                strategy="radio",
                value_written="",
                readback_match=False,
                readback_value=None,
                confidence=0.0,
            )
        radio.click()
        page.wait_for_timeout(200)
        checked = radio.is_checked()
    except Exception as exc:
        logger.warning("radio fill failed: %s", exc)
        return FillResult(
            field_key=field_key,
            strategy="radio",
            value_written=value,
            readback_match=False,
            readback_value=None,
            confidence=0.0,
        )
    return FillResult(
        field_key=field_key,
        strategy="radio",
        value_written=value,
        readback_match=checked,
        readback_value=str(checked),
        confidence=1.0 if checked else 0.3,
    )


def fill_checkbox(
    page: "Page", selector: str, value: str, field_key: str = ""
) -> FillResult:
    """Toggle a checkbox.  ``value`` should be 'true' or 'false'."""
    try:
        cb = page.locator(selector).first
        if cb.count() == 0:
            return FillResult(
                field_key=field_key,
                strategy="checkbox",
                value_written="",
                readback_match=False,
                readback_value=None,
                confidence=0.0,
            )
        should_be_checked = value.lower() in ("true", "yes", "1", "checked")
        is_checked = cb.is_checked()
        if should_be_checked != is_checked:
            cb.click()
            page.wait_for_timeout(200)
        final_state = cb.is_checked()
    except Exception as exc:
        logger.warning("checkbox fill failed: %s", exc)
        return FillResult(
            field_key=field_key,
            strategy="checkbox",
            value_written=value,
            readback_match=False,
            readback_value=None,
            confidence=0.0,
        )
    match = final_state == should_be_checked
    return FillResult(
        field_key=field_key,
        strategy="checkbox",
        value_written=value,
        readback_match=match,
        readback_value=str(final_state),
        confidence=1.0 if match else 0.0,
    )


def fill_file_upload(
    page: "Page", selector: str, file_path: str, field_key: str = ""
) -> FillResult:
    """Upload a file via <input type="file"> using Playwright's
    set_input_files (no system dialog)."""
    import os

    try:
        file_input = page.locator(selector).first
        if file_input.count() == 0:
            return FillResult(
                field_key=field_key,
                strategy="file_upload",
                value_written="",
                readback_match=False,
                readback_value=None,
                confidence=0.0,
            )
        file_input.set_input_files(file_path)
        page.wait_for_timeout(1000)
        file_name = os.path.basename(file_path)
        try:
            displayed = page.locator(
                f":has-text('{file_name}')"
            ).first.text_content(timeout=3000) or ""
        except Exception:
            displayed = ""
        success = file_name in displayed
    except Exception as exc:
        logger.warning("file_upload failed: %s", exc)
        return FillResult(
            field_key=field_key,
            strategy="file_upload",
            value_written=os.path.basename(file_path) if "os" in dir() else "",
            readback_match=False,
            readback_value=None,
            confidence=0.0,
        )
    return FillResult(
        field_key=field_key,
        strategy="file_upload",
        value_written=file_name,
        readback_match=success,
        readback_value=displayed,
        confidence=1.0 if success else 0.0,
    )


def fill_deferred(
    page: "Page", selector: str, value: str, field_key: str = ""
) -> FillResult:
    """Mark a field as 'deferred' — requires human attention."""
    return FillResult(
        field_key=field_key,
        strategy="deferred",
        value_written="",
        readback_match=False,
        readback_value=None,
        confidence=0.0,
    )


# ── Strategy lookup ─────────────────────────────────────────────────────────────


FILL_STRATEGIES = {
    "text_input": fill_text_input,
    "select": fill_select,
    "date_picker": fill_date_picker,
    "rich_text": fill_rich_text,
    "multi_select": fill_multi_select,
    "radio": fill_radio,
    "checkbox": fill_checkbox,
    "file_upload": fill_file_upload,
    "deferred": fill_deferred,
}
