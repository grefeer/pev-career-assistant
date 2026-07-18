"""Wait utilities for page interactions."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.sync_api import Page

logger = logging.getLogger(__name__)


def wait_for_text_present(
    page: "Page", text: str, timeout_ms: int = 30_000
) -> bool:
    """Wait until *text* appears in the page body.

    Returns True if found within the timeout, False otherwise.
    """
    try:
        page.wait_for_function(
            f"() => (document.body.innerText || '').includes({text!r})",
            timeout=timeout_ms,
        )
        return True
    except Exception:
        logger.debug("wait_for_text_present timeout: %s", text)
        return False


def wait_for_selector_gone(
    page: "Page", selector: str, timeout_ms: int = 5_000
) -> bool:
    """Wait until *selector* is no longer visible."""
    try:
        page.wait_for_selector(selector, state="hidden", timeout=timeout_ms)
        return True
    except Exception:
        return False
