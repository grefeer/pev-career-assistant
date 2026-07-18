"""Generic file upload via <input type="file">."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from executor.adapters.base import UploadResult

if TYPE_CHECKING:
    from playwright.sync_api import Page

logger = logging.getLogger(__name__)


def upload_via_input(
    page: "Page", selector: str, file_path: str, field_key: str = ""
) -> UploadResult:
    """Upload a file using Playwright's set_input_files (no system dialog)."""
    if not os.path.isfile(file_path):
        return UploadResult(
            field_key=field_key,
            file_name=os.path.basename(file_path),
            success=False,
            server_response_indicator="file_not_found",
        )
    file_name = os.path.basename(file_path)
    try:
        file_input = page.locator(selector).first
        if file_input.count() == 0:
            return UploadResult(
                field_key=field_key,
                file_name=file_name,
                success=False,
                server_response_indicator="no_file_input_found",
            )
        file_input.set_input_files(file_path)
    except Exception as exc:
        logger.warning("upload_via_input failed: %s", exc)
        return UploadResult(
            field_key=field_key,
            file_name=file_name,
            success=False,
            server_response_indicator="set_input_files_error",
        )
    return UploadResult(
        field_key=field_key,
        file_name=file_name,
        success=True,
        server_response_indicator=file_name,
    )
