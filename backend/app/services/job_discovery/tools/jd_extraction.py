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
    re.compile(r"(?:岗位职责|工作职责|职位描述|工作内容|主要职责|职责描述|岗位定位|你将负责|responsibilities|job description|what you.?ll do|key responsibilities)", re.IGNORECASE),
]

_REQUIREMENTS_HEADERS: list[re.Pattern] = [
    re.compile(r"(?:任职要求|岗位要求|职位要求|资格要求|招聘要求|应聘条件|基本要求|requirements|qualifications|what you.?ll need|required skills|basic requirements)", re.IGNORECASE),
]

_LOCATION_PATTERNS: list[re.Pattern] = [
    re.compile(r"(?:工作地点|工作地址|上班地点|工作城市|地点|location|work location)\s*[:：]?\s*(.+?)(?:\n|$)", re.IGNORECASE),
]

# Each rule pairs a detection regex with the single normalized type it maps to.
# A type is appended at most once, so the ``type_name not in types`` guard is the
# only branch in ``_detect_recruitment_types`` and both its arms are reachable.
_RECRUITMENT_TYPE_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(?:实习|intern|实习生)", re.IGNORECASE), "internship"),
    (re.compile(r"(?:校招|校园|应届|campus|graduate)", re.IGNORECASE), "campus_recruitment"),
    (re.compile(r"(?:社招|社会|全职|full.?time)", re.IGNORECASE), "full_time"),
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

#: Hard ceiling on candidates produced from one page. Card-list portals
#: (Feishu careers) segment into dozens of openings, so the cap is a reachable
#: guard rather than the old unreachable 10-limit.
_MAX_CANDIDATES_PER_PAGE = 100

# Feishu-style career portals render every opening as a card whose first line
# is the job title and whose second line is a dense meta line carrying the
# ``职位 ID`` marker (with locations and recruitment type inline). Splitting on
# title+meta pairs lets a whole listing (e.g. 61 NIO agent roles) extract as
# individual candidates instead of one undifferentiated blob. The role token
# may sit anywhere in the title (real titles trail suffixes such as
# ``（AI安全方向）`` or ``-NOMI``), with at most 30 chars of trailing detail;
# a chrome line above a meta (e.g. ``推荐投递``) carries no role token and is
# therefore never misread as a title.
_CARD_TITLE_ROLE_SUFFIXES = (
    "工程师|开发|算法|研究员|架构师|科学家|设计师|分析师|顾问|专家|"
    "运营|产品|经理|管培生|培训生|实习生|专员|助理|PMO"
)
_CARD_LIST_SPLIT_RE: re.Pattern = re.compile(
    rf"(?m)^(?P<title>.{{2,60}}?(?:{_CARD_TITLE_ROLE_SUFFIXES}).{{0,30}}?)\n"
    r"(?P<meta>[^\n]*职位\s*ID[^\n]*)$"
)


def _card_meta_cities(meta: str) -> str | None:
    """Read the city list leading a Feishu card meta line, if present.

    The meta line begins with locations, either directly
    (``北京、上海校招正式...``) or with a count
    (``武汉、合肥、上海等 4 个城市校招正式...``); the capture is guarded so a
    non-city lead such as ``本科及以上校招...`` or a digit-led
    ``2027届校招...`` is rejected rather than emitted as a location.
    """
    m = re.match(
        r"^([一-鿿·、等]{2,20}?)(?:\s*\d+\s*个城市)?(?:校招|社招|实习)", meta
    )
    if m is None:
        return None
    candidate = m.group(1)
    if candidate.endswith("等"):
        candidate = candidate[:-1]
    if (
        "、" not in candidate
        and len(candidate) > 4
        and not candidate.endswith(("市", "省", "都", "州"))
    ):
        return None
    return candidate


def _normalize_card_segment(card_text: str, match: re.Match, segment_start: int) -> str:
    """Prefix one Feishu-style card with extractable title/location headers.

    The injected ``职位名称：`` / ``工作地点：`` lines reuse the labeled
    extraction patterns (``_TITLE_PATTERNS[0]``, ``_LOCATION_PATTERNS``), so
    per-card titles and cities surface without touching the heuristics that
    other page layouts rely on. ``match.start()`` is absolute in the source
    page text; ``card_text`` is the sliced segment, so the offset is rebased.
    """
    title = match.group("title").strip()
    header = f"职位名称：{title}\n"
    cities = _card_meta_cities(match.group("meta"))
    if cities is not None:
        header += f"工作地点：{cities}\n"
    return header + card_text[match.start() - segment_start:].strip()


def _split_multi_job_page(text: str) -> list[str]:
    """Split page text containing multiple job postings into segments.

    Detects Chinese multi-job separators like 岗位二： / 职位2：
    or repeated title headings (岗位名称 appearing twice), or Feishu-style
    card listings (title line followed by a ``职位 ID`` meta line - each card
    becomes its own segment). Returns the detected segments, each fed
    separately to extraction.
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
            # ``after`` always begins at the matched ``\n`` and therefore always
            # contains the separator marker (e.g. ``岗位二：``), so its falsy
            # arm is unreachable.
            if after:  # pragma: no cover
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

    # Pattern 3: Feishu-style card listings. Every card becomes its own
    # segment; header/navigation chrome between cards is dropped because it is
    # not a job posting.
    card_matches = list(_CARD_LIST_SPLIT_RE.finditer(text))
    if card_matches:
        segments = []
        for i, m in enumerate(card_matches):
            start = m.start()
            end = card_matches[i + 1].start() if i + 1 < len(card_matches) else len(text)
            segments.append(_normalize_card_segment(text[start:end], m, start))
        return segments

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

            # Clean up. The capture begins right after the heading label, so it
            # may still lead with the label's colon/whitespace (the heading
            # pattern does not consume ``:``) - strip those separators first.
            section_text = re.sub(r"\s+", " ", section_text.lstrip("：:、，")).strip()
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
    for pattern, type_name in _RECRUITMENT_TYPE_RULES:
        if pattern.search(text) and type_name not in types:
            types.append(type_name)
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
    Returns 0-100 NormalizedJobCandidate objects with confidence scores.

    For structured pages (with clear section headers like 岗位职责/任职要求),
    extracts precise fields. For unstructured text (WeChat articles, OCR results),
    uses aggressive heuristics to extract whatever information is available.

    Args:
        page_text: Raw text content from a job detail page or article.
        url: The source URL for reference.

    Returns:
        List of extracted candidates (0-10, one per distinct position).
    """
    page_text = page_text or ""

    if not page_text.strip():
        return []

    # Split multi-job pages into individual segments
    segments = _split_multi_job_page(page_text)

    results: list[NormalizedJobCandidate] = []
    seen_dedup_keys: set[str] = set()

    for segment in segments:
        # Hard ceiling per page: Feishu card listings segment into dozens of
        # openings, so this guard is reachable and covered by tests.
        if len(results) >= _MAX_CANDIDATES_PER_PAGE:
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

        # ── Fallback for unstructured text ──
        # When no structured sections are found (common in WeChat articles,
        # OCR text), treat the full segment as a description and extract
        # whatever we can from keywords and context.
        uses_unstructured_fallback = False
        if not responsibilities and not requirements and not title:
            # Try harder: look for position-related keywords anywhere in text
            fm_title = _fuzzy_extract_title(segment)
            if fm_title:
                title = fm_title
            uses_unstructured_fallback = True

        # Build warnings
        warnings: list[str] = []
        if not title:
            warnings.append("No job title found via heuristics")
        if not responsibilities and not requirements:
            warnings.append("No responsibilities or requirements sections found")
        if not locations:
            warnings.append("No location information found")

        # ── Build description_text ──
        desc_parts = []
        if responsibilities:
            desc_parts.append(responsibilities)
        if requirements:
            desc_parts.append(requirements)
        if desc_parts:
            description_text = "\n\n".join(desc_parts)
        elif uses_unstructured_fallback and len(segment.strip()) >= 20:
            # For unstructured text, use the full segment as description
            # (trimmed to a reasonable length)
            description_text = segment.strip()[:4000]
        else:
            description_text = segment[:2000]

        # ── Deduplicate by title + company ──
        dedup_key = f"{title or ''}|{company or ''}"
        if dedup_key in seen_dedup_keys:
            continue
        seen_dedup_keys.add(dedup_key)

        # ── Adjusted confidence for unstructured text ──
        confidence = _estimate_confidence(
            title, company, responsibilities, requirements, has_section_content
        )
        if uses_unstructured_fallback:
            # Reduce confidence but keep above "too low to use"
            confidence = max(confidence, 0.35)

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

    # ── If still no results, try whole-text extraction ──
    # Unreachable: the loop above always appends at least one candidate for
    # non-empty text (the first segment is never a dedup hit, and
    # _split_multi_job_page returns >=1 segment for non-empty input). The
    # empty-text case returns early at line ~284. Retained as a last-resort
    # guard; _extract_from_unstructured_text is exercised directly in tests.
    if not results:  # pragma: no cover
        result = _extract_from_unstructured_text(page_text, url)
        if result:
            results.append(result)

    return results


def _fuzzy_extract_title(text: str) -> str | None:
    """Try to find a job title in unstructured text using keyword proximity.

    Looks for patterns like:
    - "招募XXX岗位" / "招聘XXX" / "招收XXX(实习)"
    - "岗位包括：XXX、YYY" / "职位：XXX"
    - "面向XXX专业招聘XXX"
    """
    # Pattern 1: 招募/招聘/招收 + job-like noun phrase (or just the recruitment prefix)
    m = re.search(
        r"(?:招募|招聘|招收|急招)[：:\s]*"
        r"(.{2,60}?(?:工程师|经理|专员|设计师|分析师|运营|开发|"
        r"实习生|实习|培训生|管培生|顾问|助理|主管|总监|代表|"
        r"岗位|职位|人员|人才))",
        text,
    )
    if m:
        return m.group(1).strip()[:60]

    # Pattern 1b: Company丨Recruitment Title (WeChat article title format)
    m = re.search(r"(.{2,40})[丨\|\-]\s*(.{2,60}?(?:招聘|实习|校招|招募|内推|春招|秋招).{0,30})", text)
    if m:
        # Return the part after 丨 as the title (more likely the job description)
        return m.group(2).strip()[:60]

    # Pattern 2: title-like line starting with position keywords
    m = re.search(
        r"(?:岗位|职位|招聘岗位|招聘职位)[：:\s]*"
        r"(.{2,60})",
        text,
    )
    if m:
        candidate = m.group(1).strip()
        # Filter out lines that are clearly not job titles
        if len(candidate) <= 60 and not candidate.startswith("http"):
            return candidate

    # Pattern 3: line ending with 岗 or 岗位
    for line in text.splitlines():
        line = line.strip()
        m = re.match(r"(.{2,30}(?:岗|岗位))$", line)
        if m:
            title_text = m.group(1)
            if not any(skip in title_text for skip in ("投递", "招聘", "求职", "关注")):
                return title_text

    # Pattern 4: 丨 separated title (common in WeChat article titles)
    # Use 2+ chars prefix to capture "XX招聘丨YY" style titles
    m = re.search(r"(.{2,60}?(?:招聘|实习|校招|内推|招募|春招|秋招).{0,30})", text)
    if m:
        raw = m.group(1).strip()
        # Clean up: strip leading/trailing separators and repeated content
        raw = re.sub(r"^[丨|\-\s]+", "", raw)
        raw = re.sub(r"[丨|\-\s]+$", "", raw)
        if len(raw) <= 80:
            return raw[:60]

    # Pattern 5: First meaningful line looks like a title
    first_line = text.strip().split("\n")[0].strip() if text.strip() else ""
    if first_line and len(first_line) >= 4 and len(first_line) <= 100:
        # Check if it contains recruitment-related keywords
        rec_kw = ["招聘", "实习", "校招", "招募", "内推", "春招", "秋招", "岗位", "入职"]
        if any(kw in first_line for kw in rec_kw):
            # Clean up common prefixes
            clean = re.sub(r"^(?:原创|分享|收藏|点赞|在看)\s*", "", first_line)
            clean = re.sub(r"\s*!{1,3}$", "", clean)
            return clean[:60]

    return None


def _extract_from_unstructured_text(text: str, url: str) -> NormalizedJobCandidate | None:
    """Last-resort extraction from completely unstructured text.

    Used when structured section headers and fuzzy title extraction both fail.
    Treats the entire text as a job description and tries to extract at minimum
    a plausible title and recruitment type.
    """
    text = text.strip()
    if len(text) < 50:
        return None

    # Must have at least some recruitment-related keywords
    recruitment_keywords = ["招聘", "实习", "校招", "内推", "岗位", "投递", "简历", "面试",
                            "intern", "campus", "recruit", "job", "career"]
    keyword_hits = sum(1 for kw in recruitment_keywords if kw.lower() in text.lower())
    if keyword_hits < 2:
        return None

    # Try fuzzy title extraction
    title = _fuzzy_extract_title(text)

    # Try to find company name
    company = None
    for pattern in _COMPANY_PATTERNS:
        m = pattern.search(text)
        if m:
            company = m.group(1).strip()
            break

    # If still no company, check first line for company-like content
    if not company:
        first_line = text.split("\n")[0].strip()
        if "丨" in first_line:
            parts = first_line.split("丨")
            # ``split`` on a string known to contain ``丨`` always yields a
            # non-empty list, so the falsy arm here is unreachable.
            if parts:  # pragma: no cover
                company = parts[0].strip()[:60]

    recruitment_types = _detect_recruitment_types(text)
    locations = _extract_locations(text)
    deadline = _extract_deadline(text)
    referral_code = _extract_referral_code(text)

    return NormalizedJobCandidate(
        title=title or "招聘信息",
        company_name=company,
        description_text=text[:4000],
        locations=locations,
        recruitment_types=recruitment_types,
        apply_url=url,
        deadline_text=deadline,
        referral_code=referral_code,
        confidence=0.30,
        normalization_warnings=[
            "Unstructured text extraction — fields may be incomplete",
            "No structured sections found; full text used as description",
        ],
    )
