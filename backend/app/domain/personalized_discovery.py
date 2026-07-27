"""Pure safety and relevance contracts for personalized job discovery v1.

Closed status/state enums, role normalization/recall, application-URL
validation, and safe display copy. No SQL, no HTTP, no LLM. Every public
helper is deterministic so the service layer can gate on it without touching
raw upstream payloads, cookies, tokens, or wall text.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit


class SourceStatusReason(StrEnum):
    """Closed reason code for a source that could not be recommended.

    Members are the only values a status row may carry; an ``other``/raw
    upstream string is never stored. ``source_status_copy`` maps each member
    to fixed display text + retry guidance.
    """

    LOGIN_REQUIRED = "login_required"
    CAPTCHA = "captcha"
    ANTI_BOT = "anti_bot"
    AUTHENTICATION_REQUIRED = "authentication_required"
    COVERAGE_INCOMPLETE = "coverage_incomplete"
    URL_UNSAFE = "url_unsafe"
    NEEDS_MANUAL_REVIEW = "needs_manual_review"


class RecommendationPresentationState(StrEnum):
    """Latest presentation event for a delivered recommendation.

    This is the current state of a delivery row, not an append-only interaction
    history. ``dismissed`` is sticky: a later re-run must not resurrect it.
    """

    NEW = "new"
    VIEWED = "viewed"
    SAVED = "saved"
    DISMISSED = "dismissed"
    APPLY_CLICKED = "apply_clicked"


# Role list / term limits guard against a runaway DTO filling the JSON column
# or the LLM prompt with junk.
_MAX_ROLE_TERMS = 100
_MAX_ROLE_TERM_CHARS = 128


def normalize_role_terms(terms: list[str] | None) -> list[str]:
    """Trim, dedupe (case-insensitive, first-seen wins), and validate terms.

    Blank / whitespace-only terms raise ``ValueError`` so a caller cannot clear
    a preference into an all-empty list. The list is capped at
    ``_MAX_ROLE_TERMS`` unique terms; each term is capped at
    ``_MAX_ROLE_TERM_CHARS`` characters.
    """
    if not terms:
        return []
    seen: dict[str, str] = {}
    out: list[str] = []
    for raw in terms:
        s = (raw or "").strip()
        if not s:
            raise ValueError("role term must not be blank")
        if len(s) > _MAX_ROLE_TERM_CHARS:
            s = s[:_MAX_ROLE_TERM_CHARS]
        key = s.lower()
        if key not in seen:
            seen[key] = s
            out.append(s)
        if len(out) >= _MAX_ROLE_TERMS:
            break
    return out


def title_matches_role_recall(
    title: str | None,
    desired_roles: list[str] | None,
    role_synonyms: list[str] | None,
    excluded_roles: list[str] | None,
) -> bool:
    """Broad title recall: a wanted term substring hits; an excluded term wins.

    Recall is intentionally broad (substring, case-insensitive) so the ranker
    receives every plausibly-matching candidate; the LLM score + threshold are
    the real filter. An excluded role always wins: if the title contains an
    excluded term the candidate is dropped even when it also matches a wanted
    term.
    """
    t = (title or "").lower()
    if not t:
        return False
    for ex in (excluded_roles or []):
        e = (ex or "").strip().lower()
        if e and e in t:
            return False
    wanted = [
        (r or "").strip().lower()
        for r in list(desired_roles or []) + list(role_synonyms or [])
    ]
    for w in wanted:
        if w and w in t:
            return True
    return False


@dataclass(frozen=True)
class ValidatedApplicationUrl:
    """A URL that passed every safety check. ``host`` is the exact allowed host."""

    url: str
    host: str


@dataclass(frozen=True)
class UrlValidationFailure:
    """A URL that failed validation. ``reason`` is a closed, safe code."""

    reason: str
    detail: str = ""


_MAX_URL_LENGTH = 2048


def validate_application_url(
    raw_url: str | None,
    allowed_hosts: set[str],
) -> ValidatedApplicationUrl | UrlValidationFailure:
    """Validate an apply URL against the closed URL-safety gate.

    Checks (no DNS resolution, ever):
      * non-empty and at most ``_MAX_URL_LENGTH`` chars;
      * scheme is exactly ``http`` or ``https`` (rejects ``javascript:``,
        ``mailto:``, ``data:`` etc.);
      * no embedded credentials (``user:pass@host``);
      * host is not a literal IP and not ``localhost`` (covers loopback /
        link-local / private / multicast / reserved - any literal IP is
        rejected outright);
      * host exactly matches one of ``allowed_hosts`` (the source/adapter
        application-host allowlist).
    """
    if not raw_url or not raw_url.strip():
        return UrlValidationFailure(reason="empty")
    url = raw_url.strip()
    if len(url) > _MAX_URL_LENGTH:
        return UrlValidationFailure(reason="too_long")
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        return UrlValidationFailure(reason="malformed", detail=str(exc))
    if parts.scheme not in ("http", "https"):
        return UrlValidationFailure(reason="bad_scheme")
    if parts.username or parts.password:
        return UrlValidationFailure(reason="credentials")
    host = (parts.hostname or "").lower()
    if not host:
        return UrlValidationFailure(reason="no_host")
    if host == "localhost" or host.endswith(".localhost"):
        return UrlValidationFailure(reason="loopback")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass  # a hostname - allowed to proceed to the allowlist check
    else:
        return UrlValidationFailure(reason="literal_ip")
    if host not in {h.lower() for h in allowed_hosts}:
        return UrlValidationFailure(reason="host_not_allowed")
    return ValidatedApplicationUrl(url=url, host=host)


# Fixed display text + retry guidance per closed reason code. Never accepts raw
# upstream wall text; a status row stores only the enum plus this fixed copy.
_STATUS_COPY: dict[SourceStatusReason, tuple[str, str]] = {
    SourceStatusReason.LOGIN_REQUIRED: (
        "该来源需要登录后才能查看完整职位，自动发现已停止。",
        "请自行登录官方招聘页确认；系统不会代为登录。",
    ),
    SourceStatusReason.CAPTCHA: (
        "该来源出现验证码拦截，自动发现已停止。",
        "请在浏览器中手动完成验证后查看；系统不会绕过验证码。",
    ),
    SourceStatusReason.ANTI_BOT: (
        "该来源触发反爬虫机制，自动发现已停止。",
        "请稍后自行访问官网确认；系统不会尝试绕过风控。",
    ),
    SourceStatusReason.AUTHENTICATION_REQUIRED: (
        "该来源要求授权访问，自动发现已停止。",
        "请通过官方渠道申请访问权限后确认。",
    ),
    SourceStatusReason.COVERAGE_INCOMPLETE: (
        "该来源的职位列表未完整抓取，暂不作为自动推荐依据。",
        "可稍后重试或自行访问官网查看完整列表。",
    ),
    SourceStatusReason.URL_UNSAFE: (
        "该来源的申请链接未通过安全校验，已排除。",
        "请通过官网原路径投递，勿使用不明链接。",
    ),
    SourceStatusReason.NEEDS_MANUAL_REVIEW: (
        "该来源暂无法自动判定，建议人工确认。",
        "可自行访问官网核实是否有合适岗位。",
    ),
}


def source_status_copy(reason: SourceStatusReason) -> tuple[str, str]:
    """Return ``(display_text, retry_guidance)`` for a closed reason code."""
    return _STATUS_COPY[SourceStatusReason(reason)]
