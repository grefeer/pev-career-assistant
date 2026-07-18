"""Generic <select> dropdown fill strategy."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from executor.adapters.base import FillResult

if TYPE_CHECKING:
    from playwright.sync_api import Page

logger = logging.getLogger(__name__)


def fill_select(
    page: "Page", selector: str, value: str, field_key: str = ""
) -> FillResult:
    """Select an option from a native <select> element by visible label."""
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
        logger.warning("fill_select failed: %s", exc)
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
