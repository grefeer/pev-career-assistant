"""DJI Moka recruitment page topology and fingerprint definitions.

Moka forms typically have 6 pages:
  1. Basic info (text, select)
  2. Education history (repeat section)
  3. Work/internship history (repeat section, rich text)
  4. Project experience (repeat section, rich text)
  5. Skills & certifications (text, multi-select)
  6. Attachments (file upload)
  Final: Preview & review page (NO auto-submit)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from executor.adapters.base import PageClass

if TYPE_CHECKING:
    pass


# ── Known page fingerprints based on simplified DOM structure ───────────────────
# fmt: off
MOKA_TOPOLOGY = [
    {
        "index": 1,
        "page_class": PageClass.MULTI_PAGE_FIRST,
        "label": "basic_info",
        "fingerprints": [],  # populated after real sampling
        "controls": {
            "name": "text_input",
            "gender": "radio",
            "birth_date": "date_picker",
            "email": "text_input",
            "phone": "text_input",
            "current_location": "text_input",
            "graduation_year": "select",
            "highest_degree": "select",
            "school": "text_input",
            "major": "text_input",
        },
    },
    {
        "index": 2,
        "page_class": PageClass.MULTI_PAGE_MIDDLE,
        "label": "education_history",
        "fingerprints": [],
        "controls": {},
        "repeat_section": "education",
    },
    {
        "index": 3,
        "page_class": PageClass.MULTI_PAGE_MIDDLE,
        "label": "work_history",
        "fingerprints": [],
        "controls": {},
        "repeat_section": "work_experience",
    },
    {
        "index": 4,
        "page_class": PageClass.MULTI_PAGE_MIDDLE,
        "label": "project_experience",
        "fingerprints": [],
        "controls": {},
        "repeat_section": "project_experience",
    },
    {
        "index": 5,
        "page_class": PageClass.MULTI_PAGE_MIDDLE,
        "label": "skills_certs",
        "fingerprints": [],
        "controls": {
            "skills": "multi_select",
            "certifications": "text_input",
            "languages": "multi_select",
        },
    },
    {
        "index": 6,
        "page_class": PageClass.MULTI_PAGE_LAST,
        "label": "attachments",
        "fingerprints": [],
        "controls": {
            "resume_upload": "file_upload",
            "portfolio_upload": "file_upload",
        },
    },
]
# fmt: on

# ── Page selectors ──────────────────────────────────────────────────────────────

# Navigation button selectors (site-specific, refined after real sampling)
NEXT_BUTTON_SELECTORS = [
    'button:has-text("下一步")',
    'button:has-text("保存并下一步")',
    '[data-action-kind="next"]',
    ".next-btn",
]

SAVE_BUTTON_SELECTORS = [
    'button:has-text("保存")',
    'button:has-text("保存草稿")',
    '[data-action-kind="save"]',
    ".save-btn",
    ".draft-btn",
]

PREV_BUTTON_SELECTORS = [
    'button:has-text("上一步")',
    '[data-action-kind="prev"]',
    ".prev-btn",
]

# Submit button selectors (MUST NEVER be auto-clicked by executor)
SUBMIT_BUTTON_SELECTORS = [
    'button:has-text("提交")',
    'button:has-text("提交申请")',
    'button:has-text("确认投递")',
    '[data-action-kind="submit"]',
    ".submit-btn",
    'input[type="submit"]',
]

# Field patterns for detection (label-based, refined after real sampling)
FIELD_LABEL_PATTERNS = {
    "name": ["姓名", "name", "fullname"],
    "gender": ["性别", "gender", "sex"],
    "birth_date": ["出生日期", "生日", "birth", "date of birth"],
    "email": ["邮箱", "电子邮件", "email", "e-mail"],
    "phone": ["手机", "电话", "phone", "mobile", "tel"],
    "school": ["学校", "毕业院校", "school", "university", "college"],
    "major": ["专业", "major", "specialization"],
    "degree": ["学历", "学位", "degree", "education level"],
    "graduation_year": ["毕业年份", "毕业时间", "graduation"],
    "company": ["公司", "企业", "company", "employer", "organization"],
    "position": ["职位", "岗位", "position", "title", "role"],
    "work_description": ["工作描述", "工作内容", "description", "responsibilities"],
    "project_name": ["项目名称", "项目", "project"],
    "project_description": ["项目描述", "project description"],
    "skills": ["技能", "skills", "technologies"],
    "certifications": ["证书", "certifications", "certificates"],
    "languages": ["语言", "languages"],
    "resume_upload": ["上传简历", "简历附件", "resume", "cv", "上传附件"],
    "portfolio_upload": ["作品集", "portfolio", "作品附件"],
}


def find_page_entry(
    page_index: int | None, fingerprint: str | None = None
) -> dict | None:
    """Find the topology entry matching the current page index.

    When ``fingerprint`` is provided, prefer exact fingerprint match.
    Otherwise fall back to index-based lookup.
    """
    if fingerprint:
        for entry in MOKA_TOPOLOGY:
            if fingerprint in entry.get("fingerprints", []):
                return entry
    for entry in MOKA_TOPOLOGY:
        if entry["index"] == page_index:
            return entry
    return None
