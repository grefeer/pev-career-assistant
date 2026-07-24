"""Error classification for trajectory recording.

Maps raw exception/error messages to a fixed set of reason codes for
SQL-level aggregation and strategy health tracking.
"""
from __future__ import annotations

from dataclasses import dataclass


STRUCTURE_ERRORS = frozenset(
    {
        "selector_not_found",
        "pagination_shape_changed",
        "detail_schema_changed",
        "unexpected_iframe",
        "api_payload_changed",
    }
)
BLOCKED_ERRORS = frozenset(
    {
        "captcha",
        "slider",
        "login_required",
        "qr_login_required",
        "anti_bot",
    }
)
TRANSIENT_ERRORS = frozenset(
    {
        "network_timeout",
        "connection_reset",
        "rate_limited",
        "upstream_5xx",
    }
)
DATA_ERRORS = frozenset({"data_error", "malformed_candidate_payload", "empty_text"})


@dataclass(frozen=True)
class ExecutionErrorClassification:
    """Immutable PATH B/C error category used for recovery routing."""

    error_type: str
    reason: str


def classify_execution_error(message: str) -> ExecutionErrorClassification:
    """Map a deterministic executor failure to one recovery category."""
    normalized = (message or "").lower()
    for reason in STRUCTURE_ERRORS:
        if reason in normalized:
            return ExecutionErrorClassification("structure_error", reason)
    for reason in BLOCKED_ERRORS:
        if reason in normalized:
            return ExecutionErrorClassification("blocked", reason)
    if "completion_unverified" in normalized:
        return ExecutionErrorClassification("completion_unverified", "completion_unverified")
    for reason in TRANSIENT_ERRORS:
        if reason in normalized:
            return ExecutionErrorClassification("transient", reason)
    if "timeout" in normalized:
        return ExecutionErrorClassification("transient", "network_timeout")
    if "malformed candidate" in normalized or "candidate payload" in normalized:
        return ExecutionErrorClassification("data_error", "malformed_candidate_payload")
    for reason in DATA_ERRORS:
        if reason in normalized:
            return ExecutionErrorClassification("data_error", reason)
    return ExecutionErrorClassification("data_error", "unknown")


def classify_next_action(error_type: str) -> str:
    """Return the only permitted recovery path for an execution category."""
    return {
        "structure_error": "planner_repair_then_path_b",
        "transient": "resume_path_b",
        "blocked": "needs_manual_review",
        "completion_unverified": "needs_manual_review",
        "data_error": "partial_success",
    }.get(error_type, "partial_success")

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
