"""Moka attachment upload handler.

Uses Playwright's ``set_input_files()`` to inject file paths directly into
``<input type="file">`` elements — no system file dialog is opened.

After upload, waits for the server to acknowledge receipt by watching for
the file name to appear in the upload list or a success indicator.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from executor.adapters.base import UploadResult

if TYPE_CHECKING:
    from playwright.sync_api import Page

logger = logging.getLogger(__name__)

UPLOAD_TIMEOUT_MS = 30_000

# Known file-input selectors for Moka attachment pages
FILE_INPUT_SELECTORS = {
    "resume_upload": 'input[type="file"][accept*="pdf"],'
    'input[type="file"][accept*="doc"],'
    'input[type="file"]:near(:text("简历"))',
    "portfolio_upload": 'input[type="file"]:near(:text("作品")),'
    'input[type="file"]:near(:text("附件"))',
}


def upload_attachment(
    page: "Page", field_key: str, file_path: str
) -> UploadResult:
    """Upload a file to the Moka form.

    Args:
        page: Playwright Page.
        field_key: The logical field key (resume_upload, portfolio_upload).
        file_path: Absolute path to the file on the local filesystem.

    Returns:
        UploadResult indicating success and the server's response indicator.
    """
    if not os.path.isfile(file_path):
        return UploadResult(
            field_key=field_key,
            file_name=os.path.basename(file_path),
            success=False,
            server_response_indicator="file_not_found",
        )

    file_name = os.path.basename(file_path)

    # Try each candidate selector for the field
    selectors = FILE_INPUT_SELECTORS.get(
        field_key, 'input[type="file"]'
    ).split(",")
    file_input = None
    for sel in selectors:
        sel = sel.strip()
        try:
            el = page.locator(sel).first
            if el.count() > 0:
                file_input = el
                break
        except Exception:
            continue

    if file_input is None:
        return UploadResult(
            field_key=field_key,
            file_name=file_name,
            success=False,
            server_response_indicator="no_file_input_found",
        )

    try:
        # Inject file path (no system dialog)
        file_input.set_input_files(file_path)
    except Exception as exc:
        logger.warning("set_input_files failed for %s: %s", field_key, exc)
        return UploadResult(
            field_key=field_key,
            file_name=file_name,
            success=False,
            server_response_indicator="set_input_files_error",
        )

    # Wait for upload acknowledgement
    try:
        page.wait_for_function(
            f"""
            () => {{
                const body = document.body.innerText || '';
                return body.includes('{file_name}');
            }}
            """,
            timeout=UPLOAD_TIMEOUT_MS,
        )
        return UploadResult(
            field_key=field_key,
            file_name=file_name,
            success=True,
            server_response_indicator=file_name,
        )
    except Exception:
        # Try alternative indicator: upload progress bar gone
        try:
            page.wait_for_selector(".upload-progress", state="hidden", timeout=5000)
            return UploadResult(
                field_key=field_key,
                file_name=file_name,
                success=True,
                server_response_indicator="progress_bar_hidden",
            )
        except Exception:
            pass

        return UploadResult(
            field_key=field_key,
            file_name=file_name,
            success=False,
            server_response_indicator="upload_timeout",
        )
