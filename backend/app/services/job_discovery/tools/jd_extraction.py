from __future__ import annotations

import re

from backend.app.services.job_discovery.schemas import NormalizedJobCandidate

# --- Heading / section markers in Chinese and English ---

_TITLE_PATTERNS: list[re.Pattern] = [
    re.compile(r"(?:岗位名称|职位名称|招聘职位|Job Title)\s*[:：]?\s*(.+?)(?:\n|$)", re.IGNORECASE),
    re.compile(r"^(.{2,30}(?:岗|职位|engineer|developer|manager|analyst|designer|specialist|intern|实习生))", re.MULTILINE | re.IGNORECASE),
]

_COMPANY_PATTERNS: list[re.Pattern] = [
    re.compile(r"(?:公司名称|公司|企业名称|招聘单位|employer|company)\s*[:：]?\s*(.+?)(?:\n|$)", re.IGNORECASE),
]

_DEPARTMENT_PATTERNS: list[re.Pattern] = [
    re.compile(r"(?:所属部门|部门|事业部|业务线|department|division)\s*[:：]?\s*(.+?)(?:\n|$)", re.IGNORECASE),
]

_RESPONSIBILITIES_HEADERS: list[re.Pattern] = [
    re.compile(r"(?:岗位职责|工作职责|职位描述|工作内容|主要职责|职责描述|responsibilities|job description|what you.?ll do|key responsibilities)", re.IGNORECASE),
]

_REQUIREMENTS_HEADERS: list[re.Pattern] = [
    re.compile(r"(?:任职要求|岗位要求|职位要求|资格要求|招聘要求|应聘条件|基本要求|requirements|qualifications|what you.?ll need|required skills|basic requirements)", re.IGNORECASE),
]

_LOCATION_PATTERNS: list[re.Pattern] = [
    re.compile(r"(?:工作地点|工作地址|上班地点|工作城市|地点|location|work location)\s*[:：]?\s*(.+?)(?:\n|$)", re.IGNORECASE),
]

_RECRUITMENT_TYPE_PATTERNS: list[re.Pattern] = [
    re.compile(r"(?:实习|intern|实习生)", re.IGNORECASE),
    re.compile(r"(?:校招|校园|应届|campus|graduate)", re.IGNORECASE),
    re.compile(r"(?:社招|社会|全职|full.?time)", re.IGNORECASE),
]

_APPLY_METHOD_PATTERNS: list[re.Pattern] = [
    re.compile(r"(?:投递方式|申请方式|如何申请|how to apply|apply method)\s*[:：]?\s*(.+?)(?:\n|$)", re.IGNORECASE),
]

_DEADLINE_PATTERNS: list[re.Pattern] = [
    re.compile(r"(?:截止日期|截止时间|招聘截止|deadline|closing date|expires?)\s*[:：]?\s*(.+?)(?:\n|$)", re.IGNORECASE),
]

_REFERRAL_CODE_PATTERNS: list[re.Pattern] = [
    re.compile(r"(?:内推码|推荐码|内推|referral code|referral)\s*[:：]?\s*(.{0,30})(?:\n|$)", re.IGNORECASE),
]

# --- Multi-job page separators (Chinese numbered position markers) ---

_MULTI_JOB_SEPARATORS: list[re.Pattern] = [
    re.compile(r"\n\s*(?:岗位|职位)\s*(?:二|三|四|五|[2-5])\s*[：:]"),
    re.compile(r"\n\s*招聘岗位\s*(?:二|三|四|五|[2-5])"),
]

_TITLE_HEADER_RE: re.Pattern = re.compile(r"^(?:岗位名称|职位名称|招聘职位)", re.MULTILINE)


def _split_multi_job_page(text: str) -> list[str]:
    """Split page text containing multiple job postings into segments.

    Detects Chinese multi-job separators like 岗位二： / 职位2：
    or repeated title headings (岗位名称 appearing twice).
    Returns up to 2 text segments, each fed separately to extraction.
    """
    if not text.strip():
        return [text]

    # Pattern 1: Numbered position markers
    for pattern in _MULTI_JOB_SEPARATORS:
        m = pattern.search(text)
        if m:
            split_pos = m.start()
            before = text[:split_pos].strip()
            after = text[split_pos:].strip()
            result = []
            if before:
                result.append(before)
            if after:
                result.append(after)
            return result[:2]

    # Pattern 2: Repeated title headers (e.g. 岗位名称 appearing twice)
    heading_matches = list(_TITLE_HEADER_RE.finditer(text))
    if len(heading_matches) >= 2:
        segments = []
        for i, m in enumerate(heading_matches):
            start = m.start()
            end = heading_matches[i + 1].start() if i + 1 < len(heading_matches) else len(text)
            segments.append(text[start:end].strip())
        return segments[:2]

    return [text]


def _extract_title(text: str) -> tuple[str | None, float]:
    """Extract job title from text using keyword heuristics."""
    for pattern in _TITLE_PATTERNS:
        m = pattern.search(text)
        if m:
            title = m.group(1).strip()
            if 1 <= len(title) <= 80:
                return title, 0.7
    return None, 0.0


def _extract_company(text: str) -> tuple[str | None, float]:
    """Extract company name from text using keyword heuristics."""
    for pattern in _COMPANY_PATTERNS:
        m = pattern.search(text)
        if m:
            company = m.group(1).strip()
            if 1 <= len(company) <= 100:
                return company, 0.7
    return None, 0.0


def _extract_department(text: str) -> str | None:
    """Extract department from text."""
    for pattern in _DEPARTMENT_PATTERNS:
        m = pattern.search(text)
        if m:
            dept = m.group(1).strip()
            if 1 <= len(dept) <= 80:
                return dept
    return None


def _extract_section(text: str, header_patterns: list[re.Pattern]) -> str:
    """Extract content under a section heading.

    Finds the first matching header, then captures text up to the next
    section heading (a line with common heading keywords) or end of string.
    """
    for pattern in header_patterns:
        m = pattern.search(text)
        if m:
            start = m.end()
            # Look for next section header to delimit this section
            remainder = text[start:]
            # Find the next line that looks like a heading
            next_header = re.search(
                r"\n\s*(?:岗位职责|工作职责|职位描述|任职要求|岗位要求|"
                r"职位要求|资格要求|工作地点|投递方式|截止日期|"
                r"公司介绍|公司简介|关于我们|responsibilities|"
                r"requirements|qualifications|location|about us)\s*[:：]?\s*\n",
                remainder,
                re.IGNORECASE,
            )
            if next_header:
                section_text = remainder[: next_header.start()]
            else:
                section_text = remainder

            # Clean up
            section_text = re.sub(r"\s+", " ", section_text).strip()
            if len(section_text) > 10:
                return section_text
    return ""


def _extract_locations(text: str) -> list[str]:
    """Extract location strings from text."""
    locations: list[str] = []
    for pattern in _LOCATION_PATTERNS:
        m = pattern.search(text)
        if m:
            loc_text = m.group(1).strip()
            # Split on common delimiters
            parts = re.split(r"[,;、/\s]{2,}", loc_text)
            for part in parts:
                part = part.strip()
                if part and len(part) <= 50:
                    locations.append(part)
    return locations


def _detect_recruitment_types(text: str) -> list[str]:
    """Detect recruitment type keywords in text."""
    types: list[str] = []
    for pattern in _RECRUITMENT_TYPE_PATTERNS:
        if pattern.search(text):
            type_str = pattern.pattern
            if "实习" in type_str or "intern" in type_str:
                if "internship" not in types:
                    types.append("internship")
            elif "校招" in type_str or "campus" in type_str or "graduate" in type_str:
                if "campus_recruitment" not in types:
                    types.append("campus_recruitment")
            elif "社招" in type_str or "full.?time" in type_str:
                if "full_time" not in types:
                    types.append("full_time")
    return types


def _extract_apply_method(text: str) -> dict | None:
    """Extract application method information."""
    for pattern in _APPLY_METHOD_PATTERNS:
        m = pattern.search(text)
        if m:
            method_text = m.group(1).strip()
            # Check if there's an email in the apply method
            email_match = re.search(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", method_text)
            if email_match:
                return {
                    "method": "email",
                    "email": email_match.group(),
                    "gui_eligible": False,
                }
            return {
                "method": "unknown",
                "gui_eligible": True,
            }
    return None


def _extract_deadline(text: str) -> str | None:
    """Extract deadline text."""
    for pattern in _DEADLINE_PATTERNS:
        m = pattern.search(text)
        if m:
            deadline = m.group(1).strip()
            if 1 <= len(deadline) <= 100:
                return deadline
    return None


def _extract_referral_code(text: str) -> str | None:
    """Extract referral / referral code."""
    for pattern in _REFERRAL_CODE_PATTERNS:
        m = pattern.search(text)
        if m:
            code = m.group(1).strip()
            if code and len(code) <= 40:
                return code
    return None


def _estimate_confidence(
    title: str | None,
    company: str | None,
    responsibilities: str,
    requirements: str,
    has_section_content: bool,
) -> float:
    """Estimate how likely this text is a real job posting."""
    score = 0.0
    if title:
        score += 0.25
    if company:
        score += 0.20
    if responsibilities:
        score += 0.25
    if requirements:
        score += 0.20
    if has_section_content:
        score += 0.10
    return min(score, 1.0)


def extract_jd_candidates(page_text: str, url: str) -> list[NormalizedJobCandidate]:
    """Parse job description text using deterministic keyword heuristics.

    This is a pure function — no LLM, no DB, no network.
    Returns 0-2 NormalizedJobCandidate objects with confidence scores.

    Args:
        page_text: Raw text content from a job detail page.
        url: The source URL for reference.

    Returns:
        List of extracted candidates (typically 0 or 1, max 2 for dual-post pages).
    """
    page_text = page_text or ""

    if not page_text.strip():
        return []

    # Split multi-job pages into individual segments (max 2)
    segments = _split_multi_job_page(page_text)

    results: list[NormalizedJobCandidate] = []
    seen_dedup_keys: set[str] = set()

    for segment in segments:
        if len(results) >= 2:
            break

        title, title_conf = _extract_title(segment)
        company, company_conf = _extract_company(segment)
        department = _extract_department(segment)
        responsibilities = _extract_section(segment, _RESPONSIBILITIES_HEADERS)
        requirements = _extract_section(segment, _REQUIREMENTS_HEADERS)
        locations = _extract_locations(segment)
        recruitment_types = _detect_recruitment_types(segment)
        apply_method = _extract_apply_method(segment)
        deadline = _extract_deadline(segment)
        referral_code = _extract_referral_code(segment)

        has_section_content = bool(responsibilities or requirements)
        confidence = _estimate_confidence(
            title, company, responsibilities, requirements, has_section_content
        )

        warnings: list[str] = []
        if not title:
            warnings.append("No job title found via heuristics")
        if not responsibilities and not requirements:
            warnings.append("No responsibilities or requirements sections found")
        if not locations:
            warnings.append("No location information found")

        # Build description_text from what we have
        desc_parts = []
        if responsibilities:
            desc_parts.append(responsibilities)
        if requirements:
            desc_parts.append(requirements)
        description_text = "\n\n".join(desc_parts) if desc_parts else segment[:2000]

        # Deduplicate by title + company to avoid identical candidates from overlapping segments
        dedup_key = f"{title or ''}|{company or ''}"
        if dedup_key in seen_dedup_keys:
            continue
        seen_dedup_keys.add(dedup_key)

        candidate = NormalizedJobCandidate(
            title=title,
            company_name=company,
            department=department,
            description_text=description_text,
            responsibilities=responsibilities,
            requirements=requirements,
            locations=locations,
            recruitment_types=recruitment_types,
            apply_url=url,
            application_channel_json=apply_method,
            deadline_text=deadline,
            referral_code=referral_code,
            confidence=confidence,
            normalization_warnings=warnings,
        )

        results.append(candidate)

    return results
