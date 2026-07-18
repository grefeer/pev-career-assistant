"""Xpeng Feishu recruitment page topology.

Feishu recruitment forms are typically 1-3 page forms with:
  - OAuth login gate (feishu.cn auth)
  - Remote search selects for university/profession
  - Separate sections for internship and project experience
"""

from __future__ import annotations

from executor.adapters.base import PageClass

XPENG_TOPOLOGY = [
    {
        "index": 1,
        "page_class": PageClass.MULTI_PAGE_FIRST,
        "label": "basic_info",
        "fingerprints": [],
        "controls": {
            "name": "text_input",
            "email": "text_input",
            "phone": "text_input",
            "university": "remote_search_select",
            "major": "remote_search_select",
            "degree": "select",
            "graduation_year": "select",
        },
    },
    {
        "index": 2,
        "page_class": PageClass.MULTI_PAGE_MIDDLE,
        "label": "internship_experience",
        "fingerprints": [],
        "controls": {},
        "repeat_section": "internship",
    },
    {
        "index": 3,
        "page_class": PageClass.MULTI_PAGE_LAST,
        "label": "attachments",
        "fingerprints": [],
        "controls": {
            "resume_upload": "file_upload",
        },
    },
]

# OAuth login indicators
LOGIN_INDICATORS = [
    'input[type="password"]',
    ':has-text("飞书登录")',
    ':has-text("扫码登录")',
    '#feishu-login',
]

REMOTE_SEARCH_SELECTOR = 'input[data-remote-search="true"]'
