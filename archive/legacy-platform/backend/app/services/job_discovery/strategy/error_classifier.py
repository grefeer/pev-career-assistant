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

# Authentication/session-wall markers (Task 7). A career site that demands
# SPA session auth (Beisen 401, iFlytek authenticated detail API) must classify
# as ``blocked`` so the worker forwards it to manual review instead of
# entering the Planner repair loop or a legacy Supervisor retry.
_AUTH_BLOCKED_MARKERS: tuple[str, ...] = (
    "authentication required",
    "requires authentication",
    "session authentication",
    "spa session",
    "requires session",
    "requires authenticated",
    "authenticated access",
    "authentication wall",
    "鉴权墙",
    "会话鉴权",
    "需要登录认证",
)
# HTTP status / access-denied markers. Any 401/403/forbidden/unauthorized is
# treated as a wall: the public-API transient 403 must NOT auto-bypass either.
_STATUS_BLOCKED_MARKERS: tuple[str, ...] = (
    "401",
    "403",
    "unauthorized",
    "forbidden",
)
_AUTH_CONTEXT_MARKERS: tuple[str, ...] = (
    "auth",
    "session",
    "login",
    "认证",
    "登录",
)


@dataclass(frozen=True)
class ExecutionErrorClassification:
    """Immutable PATH B/C error category used for recovery routing."""

    error_type: str
    reason: str


def _match_authentication(normalized: str) -> str | None:
    """Return ``authentication_required`` when ``normalized`` indicates an
    authentication/session wall, else ``None``.

    Explicit auth-wall phrases (``session authentication``, ``鉴权墙``, ...)
    match on their own. A bare 401/403/forbidden/unauthorized only matches when
    paired with auth/session/login context, so a transient public-API 403 that
    carries no auth context is left for the transient check to retry.
    """
    for marker in _AUTH_BLOCKED_MARKERS:
        if marker in normalized:
            return "authentication_required"
    has_status = any(m in normalized for m in _STATUS_BLOCKED_MARKERS)
    has_context = any(m in normalized for m in _AUTH_CONTEXT_MARKERS)
    if has_status and has_context:
        return "authentication_required"
    return None


def classify_execution_error(message: str) -> ExecutionErrorClassification:
    """Map a deterministic executor failure to one recovery category.

    Priority (Task 7):

    1. ``blocked`` -- authentication/login/captcha/anti-bot walls. An
       authenticated career-site wall (Beisen 401, iFlytek SPA-session detail
       API) must route to manual review, never the Planner repair loop.
    2. ``completion_unverified`` -- explicit CoverageVerifier failure signal.
    3. ``transient`` -- network timeouts / rate limits. A transient public-API
       403 retries here instead of becoming a wall.
    4. ``blocked`` again for any remaining 401/403/forbidden/unauthorized: a
       non-transient status wall never silently auto-bypasses to partial
       success.
    5. ``structure_error`` -- pagination/selector/schema drift (Planner repair).
    6. ``data_error`` -- malformed candidate payloads (partial success).
    7. ``unknown`` -- unmappable failure, treated as partial success.
    """
    normalized = (message or "").lower()

    # 1. Blocked: explicit auth/session walls, then captcha/login/anti-bot.
    auth_reason = _match_authentication(normalized)
    if auth_reason is not None:
        return ExecutionErrorClassification("blocked", auth_reason)
    for reason in BLOCKED_ERRORS:
        if reason in normalized:
            return ExecutionErrorClassification("blocked", reason)

    # 2. Completion-unverified signal (kept ahead of transient/structure so a
    #    message like "completion_unverified: timed out" stays terminal).
    if "completion_unverified" in normalized:
        return ExecutionErrorClassification("completion_unverified", "completion_unverified")

    # 3. Transient network: retry before treating a 403 as a wall.
    for reason in TRANSIENT_ERRORS:
        if reason in normalized:
            return ExecutionErrorClassification("transient", reason)
    if "timeout" in normalized:
        return ExecutionErrorClassification("transient", "network_timeout")

    # 4. A 401/403/forbidden/unauthorized with no transient override is a wall.
    if any(m in normalized for m in _STATUS_BLOCKED_MARKERS):
        return ExecutionErrorClassification("blocked", "authentication_required")

    # 5. Structure.
    for reason in STRUCTURE_ERRORS:
        if reason in normalized:
            return ExecutionErrorClassification("structure_error", reason)

    # 6. Data.
    if "malformed candidate" in normalized or "candidate payload" in normalized:
        return ExecutionErrorClassification("data_error", "malformed_candidate_payload")
    for reason in DATA_ERRORS:
        if reason in normalized:
            return ExecutionErrorClassification("data_error", reason)

    # 7. Unknown.
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
