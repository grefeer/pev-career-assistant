"""Error classification for trajectory recording.

Maps raw exception/error messages to a fixed set of reason codes for
SQL-level aggregation and strategy health tracking.
"""
from __future__ import annotations

_PATTERNS: list[tuple[str, list[str]]] = [
    ("network_timeout", ["timeout", "timed out", "connectionerror", "readtimeout"]),
    ("http_blocked",    ["403", "401", "forbidden", "unauthorized"]),
    # wechat_blocked before captcha: "环境异常" is WeChat-specific; generic 验证
    # markers like 滑块/验证码 should still classify as captcha.
    ("wechat_blocked",  ["环境异常", "wechat_verification", "wechat verification"]),
    ("captcha",         ["captcha", "验证", "verify", "滑块", "验证码"]),
    ("site_changed",    ["404", "not found", "页面不存在"]),
    # ocr_failed before empty_text: "OCR engine returned no text" contains
    # "no text" but should classify as ocr_failed, not empty_text.
    ("ocr_failed",      ["ocr", "tesseract", "paddle", "no text in image"]),
    ("empty_text",      ["no text", "empty body", "无正文", "empty page"]),
    ("parse_error",     ["jsondecode", "parse error", "unexpected format"]),
]


def classify_error(error_message: str) -> str:
    """Classify a raw error message into a fixed category.

    Args:
        error_message: The raw error string or exception message.

    Returns:
        One of: network_timeout, http_blocked, captcha, wechat_blocked,
        site_changed, empty_text, parse_error, ocr_failed, unknown.
    """
    if not error_message:
        return "unknown"
    lower = error_message.lower()
    for reason, keywords in _PATTERNS:
        if any(kw in lower for kw in keywords):
            return reason
    return "unknown"
