"""Generic <input type="text"> and <textarea> fill strategy.

Returns ``FillResult`` with readback verification.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from executor.adapters.base import FillResult

if TYPE_CHECKING:
    from playwright.sync_api import Page

logger = logging.getLogger(__name__)


def fill_text_input(
    page: "Page", selector: str, value: str, field_key: str = ""
) -> FillResult:
    """Fill a single-line text input or textarea by selector."""
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
        page.wait_for_timeout(200)
        readback = locator.input_value()
    except Exception as exc:
        logger.warning("fill_text_input failed: %s", exc)
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
