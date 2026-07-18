from __future__ import annotations

import re
from html.parser import HTMLParser

from backend.app.services.job_discovery.schemas import WechatArticleResult

# Chinese keywords that indicate email delivery instructions for resume submission.
_EMAIL_DELIVERY_KEYWORDS: list[re.Pattern] = [
    re.compile(r"发送简历", re.IGNORECASE),
    re.compile(r"投递邮箱", re.IGNORECASE),
    re.compile(r"邮件主题", re.IGNORECASE),
    re.compile(r"邮件标题", re.IGNORECASE),
    re.compile(r"请将简历发送至", re.IGNORECASE),
    re.compile(r"简历请发送至", re.IGNORECASE),
    re.compile(r"简历投递", re.IGNORECASE),
    re.compile(r"请将简历投递至", re.IGNORECASE),
    re.compile(r"简历发送到", re.IGNORECASE),
    re.compile(r"投递简历", re.IGNORECASE),
    re.compile(r"请发送简历至", re.IGNORECASE),
]

# Email address pattern.
_EMAIL_PATTERN = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")

# Inaccessible content markers that indicate the article cannot be read.
_INACCESSIBLE_MARKERS: list[str] = [
    "请在微信客户端打开",
    "请长按识别二维码",
    "请在微信中打开",
    "请在微信客户端打开链接",
    "登录后查看",
]


class _WeChatHTMLParser(HTMLParser):
    """Minimal HTML parser to extract title, text, and image URLs from a WeChat article."""

    def __init__(self) -> None:
        super().__init__()
        self._title: str | None = None
        self._text_parts: list[str] = []
        self._image_urls: list[str] = []
        self._in_js_content = False
        self._in_title = False
        self._in_og_title = False
        self._skip_next = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_dict = dict(attrs)
        tag_lower = tag.lower()

        # Detect js_content div
        if tag_lower == "div" and attr_dict.get("id") == "js_content":
            self._in_js_content = True

        # OG title meta tag
        if tag_lower == "meta" and attr_dict.get("property") == "og:title":
            content = attr_dict.get("content")
            if content:
                self._title = content

        # <title> tag
        if tag_lower == "title":
            self._in_title = True

        # Images
        if tag_lower == "img":
            src = attr_dict.get("data-src") or attr_dict.get("src")
            if src:
                self._image_urls.append(src)

        # <br> and <p> add newlines
        if tag_lower in ("br", "p", "div", "tr"):
            if self._in_js_content:
                self._text_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag_lower = tag.lower()
        if tag_lower == "div" and self._in_js_content:
            self._in_js_content = False
        if tag_lower == "title":
            self._in_title = False
        # <p>, <div> end tags add newline for readability
        if tag_lower in ("p", "div", "h1", "h2", "h3", "h4", "li"):
            if self._in_js_content:
                self._text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title and self._title is None:
            self._title = data.strip()
        elif self._in_js_content:
            stripped = data.strip()
            if stripped:
                self._text_parts.append(stripped)


def _extract_text_via_regex(html_content: str) -> str | None:
    """Fallback: extract text from #js_content using regex."""
    m = re.search(
        r'<div[^>]*id=["\']js_content["\'][^>]*>(.*?)</div>\s*</div>\s*</div>',
        html_content,
        re.DOTALL,
    )
    if not m:
        m = re.search(
            r'<div[^>]*id=["\']js_content["\'][^>]*>(.*?)</div>',
            html_content,
            re.DOTALL,
        )
    if m:
        inner = m.group(1)
        # Strip all HTML tags
        text = re.sub(r"<[^>]+>", "", inner)
        # Collapse whitespace
        text = re.sub(r"\s+", " ", text).strip()
        return text
    return None


def _extract_title_via_regex(html_content: str) -> str | None:
    """Extract title from og:title meta tag or <title> via regex."""
    m = re.search(
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
        html_content,
        re.IGNORECASE,
    )
    if m:
        return m.group(1).strip()
    m = re.search(
        r'<title[^>]*>([^<]+)</title>',
        html_content,
        re.IGNORECASE | re.DOTALL,
    )
    if m:
        return m.group(1).strip()
    return None


def _scan_inaccessible_markers(html_content: str, text_content: str) -> str | None:
    """Check for markers that indicate the article is inaccessible."""
    reasons = []
    for marker in _INACCESSIBLE_MARKERS:
        if marker in html_content or marker in text_content:
            reasons.append(marker)
    return "; ".join(reasons) if reasons else None


def parse_wechat_article(html_content: str, url: str) -> WechatArticleResult:
    """Parse a WeChat article HTML and extract structured content.

    This is a pure, deterministic parser -- no network, no LLM.
    Returns a WechatArticleResult with extracted title, text, images,
    and delivery instructions if found.
    """
    html_content = html_content or ""
    url = url or ""

    # --- Parse with HTMLParser ---
    parser = _WeChatHTMLParser()
    try:
        parser.feed(html_content)
        parser.close()
    except Exception:
        # If parser fails, fall through to regex extraction
        pass

    title = parser._title or _extract_title_via_regex(html_content)

    # Build text content
    if parser._text_parts:
        text_content = " ".join(parser._text_parts)
        text_content = re.sub(r"\s+", " ", text_content).strip()
    else:
        text_content = _extract_text_via_regex(html_content) or ""

    image_urls = parser._image_urls

    # --- Scan for email delivery instructions ---
    combined = html_content + "\n" + text_content
    email_instructions: str | None = None

    for pattern in _EMAIL_DELIVERY_KEYWORDS:
        m = pattern.search(combined)
        if m:
            # Extract ~200 chars of context around the match
            start = max(0, m.start() - 50)
            end = min(len(combined), m.end() + 150)
            snippet = combined[start:end].strip()
            # Extract any email addresses in the snippet
            emails = _EMAIL_PATTERN.findall(snippet)
            if emails:
                email_instructions = (
                    f"{m.group()}: {', '.join(emails)}"
                )
            else:
                email_instructions = m.group()
            break

    # --- Check for inaccessible markers ---
    inaccessible_reason = _scan_inaccessible_markers(html_content, text_content)

    return WechatArticleResult(
        title=title,
        text_content=text_content,
        image_urls=image_urls,
        email_delivery_instructions=email_instructions,
        needs_manual_review=inaccessible_reason is not None,
        manual_review_reason=inaccessible_reason or "",
    )
