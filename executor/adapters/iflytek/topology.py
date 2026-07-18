"""iFlytek zhiye.com recruitment page topology.

zhiye.com forms are multi-page (typically 4-6 pages) with:
  - Front-end validation on each page
  - Attachment upload with server processing wait
  - Field linkage (school/profession dropdowns)
"""

from __future__ import annotations

from executor.adapters.base import PageClass

IFLYTEK_TOPOLOGY = [
    {
        "index": 1,
        "page_class": PageClass.MULTI_PAGE_FIRST,
        "label": "personal_info",
        "fingerprints": [],
        "controls": {},
    },
    {
        "index": 2,
        "page_class": PageClass.MULTI_PAGE_MIDDLE,
        "label": "education",
        "fingerprints": [],
        "controls": {},
    },
    {
        "index": 3,
        "page_class": PageClass.MULTI_PAGE_MIDDLE,
        "label": "experience",
        "fingerprints": [],
        "controls": {},
    },
    {
        "index": 4,
        "page_class": PageClass.MULTI_PAGE_LAST,
        "label": "attachments",
        "fingerprints": [],
        "controls": {},
    },
]

# Front-end validation error indicators
VALIDATION_ERROR_SELECTORS = [
    ".error-tip",
    ".field-error",
    '[class*="error"]',
    ':has-text("格式不正确")',
    ':has-text("请填写")',
]
