from __future__ import annotations

import re
from urllib.parse import urlsplit

from backend.app.services.job_discovery.schemas import TriageResult

# Known login-walled / captcha-blocked domains that make automated access impractical.
_BLOCKED_DOMAINS: set[str] = {
    "linkedin.com",
    "www.linkedin.com",
    "zhaopin.com",
    "www.zhaopin.com",
    "liepin.com",
    "www.liepin.com",
    "51job.com",
    "www.51job.com",
    "m.51job.com",
    "lagou.com",
    "www.lagou.com",
    "m.lagou.com",
    "kanzhun.com",
    "www.kanzhun.com",
    "m.kanzhun.com",
    "huntingwork.com",
    "www.huntingwork.com",
    "liepin.cn",
    "www.liepin.cn",
}

# URL path segment patterns that indicate a job listing / career page (not a detail page).
_CAREER_PATH_PATTERNS: list[re.Pattern] = [
    re.compile(r"/jobs/?$", re.IGNORECASE),
    re.compile(r"/jobs/\w*page", re.IGNORECASE),
    re.compile(r"/careers/?$", re.IGNORECASE),
    re.compile(r"/careers/\w*search", re.IGNORECASE),
    re.compile(r"/campus/?$", re.IGNORECASE),
    re.compile(r"/join/?$", re.IGNORECASE),
    re.compile(r"/recruit/?$", re.IGNORECASE),
    re.compile(r"/recruitment/?$", re.IGNORECASE),
    re.compile(r"/joblist", re.IGNORECASE),
    re.compile(r"/position", re.IGNORECASE),
    re.compile(r"/position/", re.IGNORECASE),
    re.compile(r"/job/list", re.IGNORECASE),
    re.compile(r"/job/search", re.IGNORECASE),
    re.compile(r"/social-recruitment", re.IGNORECASE),
    re.compile(r"/campus-recruitment", re.IGNORECASE),
    re.compile(r"招聘", re.IGNORECASE),  # 招聘
    re.compile(r"加入我们", re.IGNORECASE),  # 加入我们
]

# URL path segment patterns that indicate a specific job detail page.
_DETAIL_PATH_PATTERNS: list[re.Pattern] = [
    re.compile(r"/job/\d+", re.IGNORECASE),
    re.compile(r"/jobs/\d+", re.IGNORECASE),
    re.compile(r"/position/\d+", re.IGNORECASE),
    re.compile(r"/req/\w+", re.IGNORECASE),
    re.compile(r"/requisition/\w+", re.IGNORECASE),
    re.compile(r"/opening/\w+", re.IGNORECASE),
    re.compile(r"/jd/\d+", re.IGNORECASE),
    re.compile(r"/job-detail", re.IGNORECASE),
    re.compile(r"/position-detail", re.IGNORECASE),
    re.compile(r"/p/\d+", re.IGNORECASE),
    re.compile(r"/apply/\d+", re.IGNORECASE),
    re.compile(r"/job/\w{8,}", re.IGNORECASE),  # /job/XXXX with UUID-like identifiers
]


def triage_link(url: str) -> TriageResult:
    """Classify a URL into a site type and recommend the next action.

    This is a pure, deterministic heuristic -- no external calls, no ML.
    Returns a TriageResult with site_type, confidence, and recommended_action.
    """
    url = url.strip()

    # --- invalid ---
    if not url:
        return TriageResult(
            site_type="invalid",
            confidence=1.0,
            recommended_action="skip",
            notes="Empty URL",
        )

    # mailto: links
    if url.startswith("mailto:"):
        return TriageResult(
            site_type="email_only",
            confidence=1.0,
            recommended_action="finish_manual_review",
            notes="Email-only application channel",
        )

    # Must be http(s)
    if not url.startswith("http://") and not url.startswith("https://"):
        return TriageResult(
            site_type="invalid",
            confidence=1.0,
            recommended_action="skip",
            notes=f"Non-HTTP URL scheme: {url[:60]}",
        )

    try:
        parsed = urlsplit(url)
    except ValueError:
        return TriageResult(
            site_type="invalid",
            confidence=0.9,
            recommended_action="skip",
            notes=f"URL parse error: {url[:60]}",
        )

    domain = parsed.netloc.lower()
    path = parsed.path

    # --- wechat article ---
    if "mp.weixin.qq.com" in domain:
        return TriageResult(
            site_type="wechat_article",
            confidence=1.0,
            recommended_action="run_web_navigation",
            notes="WeChat article URL; use browser navigation to fetch public article text before parsing",
        )

    # --- blocked ---
    if domain in _BLOCKED_DOMAINS:
        return TriageResult(
            site_type="blocked",
            confidence=0.95,
            recommended_action="finish_manual_review",
            notes=f"Known login/captcha domain: {domain}",
        )

    # --- job detail page ---
    for pattern in _DETAIL_PATH_PATTERNS:
        if pattern.search(path):
            return TriageResult(
                site_type="job_detail",
                confidence=0.85,
                recommended_action="run_web_navigation",
                notes=f"Matched job detail pattern: {pattern.pattern}",
            )

    # --- career listing page ---
    for pattern in _CAREER_PATH_PATTERNS:
        if pattern.search(path):
            return TriageResult(
                site_type="career_site",
                confidence=0.80,
                recommended_action="run_web_navigation",
                notes=f"Matched career listing pattern: {pattern.pattern}",
            )

    # --- official site (homepage or unknown page) ---
    return TriageResult(
        site_type="official_site",
        confidence=0.60,
        recommended_action="run_web_navigation",
        notes="Company homepage or generic page — may need deeper navigation",
    )
