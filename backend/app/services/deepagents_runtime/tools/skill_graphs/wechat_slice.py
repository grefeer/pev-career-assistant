"""WeChat image-article (OCR) slice for the job-discovery subgraph (Task 9).

Implements ``skill/job-discovery/references/wechat-image-handling.md``
(read-only ground truth) Levels 1-6 for one WeChat article URL:

- **L1**: public-URL guard (scheme http(s), no userinfo, global IP;
  redirects followed manually, max 5 hops, a private/cloud-metadata
  target is blocked and never fetched) - then the article is fetched
  through the ``fetch_html_fn`` seam (default: the same guarded fetch).
- **L2/L3**: parse the article HTML for ``<img>`` srcs (``data:`` URIs
  dropped; text-rich articles cap at 5 images, image-heavy articles at
  10); download each image through the size filters (undersized/oversized
  are skipped, never raised); OCR each surviving image exactly once with
  the allowlisted ``ocr_image`` script (``--engine auto --out output/ocr``).
- **L5**: combine article text + OCR sections in the doc's L4 combine
  format (``=== 文章正文 ===`` / ``=== 图片N OCR内容 (置信度: x.xx) ===``).
- **L6**: channel triage A/B/C/D (brief-authoritative semantics) -
  A = job content in the article text (extract from the combined text);
  B = job content only in OCR (REPLACE-OCR: the OCR text replaces the
  article text as extraction input); C = contact-only -> needs_manual_review;
  D = non-job/promotional -> skipped.  Every A/B candidate is enriched with
  the application-channel JSON (email / career URL / primary+alternative)
  and per-image OCR evidence; a career URL marks ``needs_deep_crawl`` and
  appends the doc's hand-off entry to ``output/errors.jsonl``.

Security gates: non-public article URLs are blocked before any fetch; a
blocked page is never re-fetched; per-image failures fold to "skip" and
never crash the slice; the OCR script's output is the only OCR text
source (no model-proposed URIs).
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import socket
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit

import requests

from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.career_skills.job_discovery import (
    ExtractObservedJobDetailsInput,
    ExtractedJobDetails,
    PublicJobFetchError,
)
from backend.app.services.deepagents_runtime.tools.extract_gate import extract_with_gate
from backend.app.services.deepagents_runtime.tools.skill_graphs.persistence import (
    _append_errors_jsonl_at,
    append_errors_jsonl,
)
from backend.app.services.deepagents_runtime.tools.skill_graphs.subprocess_runner import (
    run_skill_script,
)

#: L1: article text below this is treated as image-heavy (doc L1 table).
_ARTICLE_TEXT_MIN_CHARS = 200
#: L2: text-rich articles OCR at most 5 images (doc L2 procedure step 2).
_L2_MAX_IMAGES = 5
#: L3: image-heavy articles OCR at most 10 images (doc L3 step 1).
_L3_MAX_IMAGES = 10
#: L2: images below 10KB are likely icons/emojis and are skipped (doc L2).
_MIN_IMAGE_BYTES = 10 * 1024
#: Oversized guard (brief Step 3 "skip undersized/oversized - never raise"):
#: a >20MiB image would exhaust OCR memory, so it is skipped, never OCR'd.
_MAX_IMAGE_BYTES = 20 * 1024 * 1024
#: L3: ">=1 image produced >100 chars of structured text" is a usable OCR.
_OCR_USABLE_TEXT_CHARS = 100
#: Skill ``_fetch_validated`` semantics: manual redirect walk, max 5 hops.
_MAX_PUBLIC_REDIRECTS = 5
#: Evidence projection bound (same as the graph's per-page extraction).
_VISIBLE_TEXT_LIMIT = 1200
#: ocr_image.py slice threshold: taller images are "long" (doc L4 metadata).
_LONG_IMAGE_HEIGHT = 2000
#: Same public-fetch UA as career_skills job discovery.
_PUBLIC_FETCH_HEADERS = {"User-Agent": "CareerAssistantPEV/1.0 (+public-job-fetch)"}
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


@dataclass
class WechatResult:
    """Outcome of the Level 1-6 slice for one WeChat article URL.

    ``status`` is one of ``succeeded`` / ``partial_success`` (weak OCR,
    doc L3) / ``blocked`` (non-public URL or fetch failure) /
    ``needs_manual_review`` (contact-only, doc L1 no-content, the doc's
    Unknown channel, or zero usable OCR on an image-heavy article) /
    ``skipped`` (non-job) / ``failed`` (slice crash - the graph folds any
    exception into this).  ``reason`` carries the doc-verbatim
    degradation reason; ``application_channel_json`` and
    ``needs_deep_crawl`` are the doc L6 Step 4 hand-off values.
    ``visible_text``/``content_hash`` carry the produced extraction text
    (bounded to the evidence projection limit) and its sha256 - the
    per-URL entry's evidence projection, mirroring the regular page
    contract; slices that produce no text carry ""/None.
    """

    url: str
    status: str
    channel: str | None
    candidates: list[ExtractedJobDetails]
    application_channel_json: dict | None
    needs_deep_crawl: bool
    reason: str | None
    #: Produced extraction text (bounded) + its sha256; ""/None when the
    #: slice produced no text (blocked / skipped / manual-review outcomes).
    visible_text: str = ""
    content_hash: str | None = None


# ------------------------------------------------------------------ Level 1


def _l1_guard(url: str) -> str | None:
    """Structural public-URL guard (scheme, userinfo, IP-literal locality).

    Never performs DNS - a hostname is structurally public here and the
    resolver-backed ``_assert_public_url`` re-checks it at fetch time.
    Returns the blocked reason, or None when the URL is structurally
    public.
    """
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return "unsafe_public_url"
    if parsed.username is not None or parsed.password is not None:
        return "unsafe_public_url"
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        return None
    if not address.is_global:
        return "unsafe_public_url"
    return None


def _assert_public_url(url: str) -> None:
    """DNS-resolving public-URL guard (skill ``_assert_public_url`` semantics).

    Scheme must be http(s), no userinfo, and every resolved address must be
    globally routable - a private/loopback/cloud-metadata target raises
    ``unsafe_public_url``; an unresolvable host raises
    ``public_host_unresolvable``.
    """
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise PublicJobFetchError("unsafe_public_url")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, None, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise PublicJobFetchError("public_host_unresolvable") from exc
    for _family, _kind, _proto, _canon, sockaddr in addresses:
        if not ipaddress.ip_address(sockaddr[0]).is_global:
            raise PublicJobFetchError("unsafe_public_url")


def _default_fetch_html(url: str) -> str:
    """Fetch a public article, following redirects manually (max 5 hops).

    Every ``Location`` hop is re-guarded before the next GET; a redirect to
    a private/cloud-metadata target or a redirect chain longer than 5 hops
    raises ``unsafe_public_url`` (the target is never fetched).
    """
    current = url
    for _ in range(_MAX_PUBLIC_REDIRECTS + 1):
        _assert_public_url(current)
        response = requests.get(
            current, timeout=20, allow_redirects=False, headers=_PUBLIC_FETCH_HEADERS
        )
        if not response.is_redirect:
            return response.text
        target = response.headers.get("Location")
        if not target:
            return response.text
        current = urljoin(current, target)
    raise PublicJobFetchError("unsafe_public_url")


def _default_download_image(url: str) -> bytes:
    """Download one article image through the same public-URL guard.

    Mirrors ``_default_fetch_html``: redirects are followed manually and
    every ``Location`` hop is re-guarded with ``_assert_public_url``
    (max 5 hops) - a redirect to a private/cloud-metadata target raises
    ``unsafe_public_url`` and is never fetched, keeping the image path's
    "non-public URLs never fetched" invariant identical to the article
    path.
    """
    current = url
    for _ in range(_MAX_PUBLIC_REDIRECTS + 1):
        _assert_public_url(current)
        response = requests.get(
            current, timeout=30, allow_redirects=False, headers=_PUBLIC_FETCH_HEADERS
        )
        if not response.is_redirect or not response.headers.get("Location"):
            response.raise_for_status()
            return response.content
        current = urljoin(current, response.headers["Location"])
    raise PublicJobFetchError("unsafe_public_url")


def _safe_download(download_fn: Callable[[str], bytes], image_url: str) -> bytes | None:
    """Download one image; any failure folds to None (the image is skipped).

    A non-public image URL is rejected before any download; an exception
    from the downloader never crashes the slice.
    """
    if _l1_guard(image_url) is not None:
        return None
    try:
        return download_fn(image_url)
    except Exception:  # noqa: BLE001 - a bad image never crashes the slice
        return None


# ------------------------------------------------------------- L2/L3 parse


class _WechatArticleParser(HTMLParser):
    """One-pass parse of a WeChat article: body text + ``<img>`` srcs.

    ``script``/``style`` subtrees are skipped (WeChat pages embed config
    and CSS, neither is article content).
    """

    def __init__(self) -> None:
        super().__init__()
        self.text_parts: list[str] = []
        self.srcs: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"script", "style"}:
            self._skip_depth += 1
        elif tag == "img":
            for key, value in attrs:
                if key == "src" and value:
                    self.srcs.append(value)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            stripped = data.strip()
            if stripped:
                self.text_parts.append(stripped)


def _parse_article(html: str) -> tuple[str, list[str]]:
    """Parse the article HTML into (body text, raw ``<img>`` srcs)."""
    parser = _WechatArticleParser()
    parser.feed(html)
    return "\n".join(parser.text_parts), parser.srcs


def _image_suffix(image_url: str) -> str:
    """Real extension of the image URL when known, else the PNG default
    (doc L3 step 1 writes ``wechat_img_NN.png``)."""
    suffix = Path(urlsplit(image_url).path).suffix.lower()
    return suffix if suffix in _IMAGE_SUFFIXES else ".png"


# ------------------------------------------------------------------- L4/L5


def _ocr_section(index: int, text: str, confidence: float) -> str:
    """One combine-format OCR section (doc L4: ``=== 图片N OCR内容 (置信度: x.xx) ===``)."""
    return f"=== 图片{index} OCR内容 (置信度: {confidence:.2f}) ===\n{text}"


def _build_combined(
    article_text: str, ocr_items: list[tuple[str, float, dict[str, str]]]
) -> str:
    """Doc L4 combine format: article body + one marked section per image."""
    parts = ["=== 文章正文 ===", article_text]
    for index, (text, confidence, _meta) in enumerate(ocr_items, start=1):
        parts.append(_ocr_section(index, text, confidence))
    return "\n\n".join(parts)


def _build_ocr_only(ocr_items: list[tuple[str, float, dict[str, str]]]) -> str:
    """REPLACE-OCR input: the OCR sections alone replace the article text."""
    return "\n\n".join(
        _ocr_section(index, text, confidence)
        for index, (text, confidence, _meta) in enumerate(ocr_items, start=1)
    )


def _run_ocr(
    image_path: str, out_dir: str, *, runner: Callable | None
) -> tuple[str, float, dict[str, Any]] | None:
    """One ``ocr_image`` invocation with the doc's exact flags.

    The runner seam may return the real script's JSON with a ``[stderr]``
    tail; any unparsable, error, or empty output folds to None and the
    image is skipped - the slice never crashes on OCR failure.
    """
    out = (runner or run_skill_script)(
        "ocr_image", cli_args=f"{image_path} --engine auto --out {out_dir}"
    )
    try:
        result = json.JSONDecoder().raw_decode(out, 0)[0]
    except ValueError:
        return None
    if not isinstance(result, dict) or result.get("status") != "ok":
        return None
    raw_text = result.get("full_text")
    if not isinstance(raw_text, str) or not raw_text.strip():
        return None
    try:
        confidence = float(result.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    return raw_text, confidence, result


def _ocr_evidence_meta(
    image_url: str, data: bytes, raw: dict[str, Any], confidence: float
) -> dict[str, str]:
    """Doc L4 evidence entry for one OCR'd image (evidence_refs-compatible).

    ``evidence_refs`` carries ``dict[str, str]`` only, so the doc's
    metadata object (ocr_engine / ocr_confidence / image_dimensions /
    is_long_image) is JSON-encoded into the string value.
    """
    dimensions = raw.get("dimensions")
    is_long_image = False
    if isinstance(dimensions, dict):
        try:
            height = int(dimensions.get("height") or 0)
        except (TypeError, ValueError):
            height = 0
        is_long_image = height > _LONG_IMAGE_HEIGHT
    metadata = {
        "ocr_engine": str(raw.get("engine") or ""),
        "ocr_confidence": round(confidence, 4),
        "image_dimensions": dimensions if isinstance(dimensions, dict) else None,
        "is_long_image": is_long_image,
    }
    return {
        "evidence_type": "ocr_text",
        "url": image_url,
        # doc L4: content_hash of the image bytes
        "content_hash": f"sha256_{hashlib.sha256(data).hexdigest()}",
        "metadata": json.dumps(metadata, ensure_ascii=False),
    }


# ------------------------------------------------------- L6 channel triage


_JOB_CONTENT_RE = re.compile(
    r"(?:岗位|职位|招聘|职责|要求|校招|社招|实习|工程师|开发|算法|"
    r"researcher|engineer|developer|intern|hiring|career|job)",
    re.IGNORECASE,
)
_CONTACT_ONLY_RE = re.compile(
    r"(?:微信|加微信|邮箱|二维码|扫码|联系方式|投递|wechat|contact)", re.IGNORECASE
)


def classify_wechat_channel(
    *, article_text: str, ocr_texts: list[str]
) -> tuple[str, str | None]:
    """Triage the article into content channel A/B/C/D (brief semantics).

    A = job content in the article text (extract from the combined text);
    B = job content only in OCR (REPLACE-OCR rule); C = contact-only
    (微信/邮箱/二维码, no job content) -> needs_manual_review; D =
    non-job/promotional -> skipped.  Returns (channel, reason); the
    reason is only set for the non-extractable channels C/D.
    """
    if _JOB_CONTENT_RE.search(article_text):
        return "A", None
    if any(_JOB_CONTENT_RE.search(text) for text in ocr_texts):
        return "B", None
    combined = article_text + "\n" + "\n".join(ocr_texts)
    if _CONTACT_ONLY_RE.search(combined):
        return "C", "推文仅含联系方式（微信/邮箱/二维码），无职位内容，需人工处理"
    return "D", "推文为推广/非招聘内容，无职位信息"


_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_URL_RE = re.compile(r"https?://[^\s，。；：（）()\"'“”]+")
#: Doc L6 Step 1 career-site signals: zhiye.com / mokahr.com /
#: jobs.feishu.cn / campus. / /careers (bytedance included for
#: bytedance.com / jobs.bytedance.com).
_CAREER_HOST_RE = re.compile(r"(?:zhiye|mokahr|feishu|bytedance)")
_CAREER_PATH_RE = re.compile(r"campus|careers", re.IGNORECASE)
_BARE_CAREER_RE = re.compile(
    r"(?:[a-z0-9-]+\.)+(?:zhiye|mokahr|feishu|bytedance)\.[a-z]{2,6}"
    r"(?:/[^\s，。；：（）()]*)?",
    re.IGNORECASE,
)
#: OCR may corrupt URLs (doc L6 Step 1 "Important": ``zhiye`` ->
#: ``zhlye``); a recognizable-but-garbled career mention - including the
#: doc's own ``zhlye`` example - is reconstructed per the doc's pattern
#: table with the uncertainty flagged.
_CORRUPTED_CAREER_TOKEN_RE = re.compile(
    r"(?:zhiye|zhlye|feishu|mokahr)", re.IGNORECASE
)
_CAREER_PATTERNS = {
    "zhiye": "*.zhiye.com/*",
    # doc L6 Step 1 example: OCR corrupts `zhiye` into `zhlye`
    "zhlye": "*.zhiye.com/*",
    "feishu": "*jobs.feishu.cn/*",
    "mokahr": "*mokahr.com/*",
}
_QR_RE = re.compile(r"扫码|二维码|QR", re.IGNORECASE)


def _is_career_url(raw: str) -> bool:
    parsed = urlsplit(raw if "://" in raw else "https://" + raw)
    host = (parsed.hostname or "").lower()
    return bool(_CAREER_HOST_RE.search(host)) or bool(_CAREER_PATH_RE.search(parsed.path))


def _detect_application_channel(
    text: str, *, ocr_contributed: bool
) -> tuple[dict | None, str | None, bool, list[str], bool]:
    """Scan the text for the doc's L6 Step 1 channel signals.

    Returns ``(application_channel_json, career_url, needs_deep_crawl,
    channel_warnings, unknown_channel)``.  A career URL marks
    ``needs_deep_crawl`` - the article is an index and the real JDs live
    at the career site (doc L6 Step 4 hand-off).  Email + career URL ->
    primary/alternative (doc Channel D); a scheme-less or garbled career
    mention is reconstructed (add https:// / the doc's pattern) with the
    uncertainty flagged in ``normalization_warnings``.  No email/URL/QR
    signal is the doc's **Unknown** (无渠道) row: ``unknown_channel`` is
    set so the caller treats it as QR-code only and marks
    needs_manual_review.
    """
    warnings: list[str] = []
    career_url: str | None = None
    for raw in _URL_RE.findall(text):
        if _is_career_url(raw):
            career_url = raw
            break
    if career_url is None:
        # scheme-less career domain (``jereh.zhiye.com/campus``); the regex
        # already guarantees a career domain, so no re-check is needed
        bare = _BARE_CAREER_RE.search(text)
        if bare is not None:
            career_url = "https://" + bare.group(0)
            warnings.append(f"OCR可能损坏了URL，已按模式重建：{career_url}")
    if career_url is None:
        token = _CORRUPTED_CAREER_TOKEN_RE.search(text)
        if token:
            career_url = _CAREER_PATTERNS[token.group(0).lower()]
            warnings.append(f"OCR可能损坏了URL，已按模式重建：{career_url}")
    email_match = _EMAIL_RE.search(text)
    email = email_match.group(0) if email_match else None
    unknown_channel = False
    if email is not None and career_url is not None:
        channel_json = {
            "primary": {"type": "url", "value": career_url},
            "alternative": {"type": "email", "value": email},
        }
    elif email is not None:
        channel_json = {"type": "email", "value": email}
        warnings.append(
            "从微信推文OCR提取，申请通过邮箱投递"
            if ocr_contributed
            else "申请通过邮箱投递"
        )
    elif career_url is not None:
        channel_json = {"type": "url", "value": career_url}
    elif _QR_RE.search(text):
        channel_json = None
        warnings.append("仅支持扫码投递，无邮箱或官网链接")
    else:
        # doc L6 Step 1 Unknown (无渠道): no email/URL/QR signal - treat
        # as QR-code only; mark needs_manual_review
        channel_json = None
        unknown_channel = True
    return channel_json, career_url, career_url is not None, warnings, unknown_channel


# ------------------------------------------------------- extraction + enrich


def _extract_from_text(
    context: ToolContext,
    text: str,
    url: str,
    extract_fn: Callable,
    llm_extractor: Callable | None,
) -> list[ExtractedJobDetails]:
    """Gated extraction over the slice's input text (combined or OCR-only).

    Registers the text as observed evidence - full sha256 as artifact_id,
    ``visible_text`` bounded to the evidence projection limit, the same
    registration contract the graph's per-page extraction uses - then runs
    exactly one gated extraction.  A PublicJobFetchError folds to [].
    """
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    context.metadata.setdefault("observed_public_evidence", []).append(
        {
            "artifact_id": content_hash,
            "source_url": url,
            "content_hash": content_hash,
            "visible_text": text[:_VISIBLE_TEXT_LIMIT],
        }
    )
    payload = ExtractObservedJobDetailsInput(artifact_id=content_hash)
    try:
        if llm_extractor is not None:
            output = extract_with_gate(
                context, payload, enabled=True, llm_extractor=llm_extractor
            )
        else:
            output = extract_fn(context, payload)
    except PublicJobFetchError:
        return []
    return list(output.candidates)


def _enrich_candidates(
    candidates: list[ExtractedJobDetails],
    *,
    ocr_entries: list[dict[str, str]],
    channel_json: dict | None,
    ocr_warnings: list[str],
    channel_warnings: list[str],
) -> None:
    """Attach OCR evidence, application-channel evidence, and warnings.

    ``ExtractedJobDetails`` has no application-channel field, so the
    channel JSON rides in ``evidence_refs`` (JSON-encoded) and survives
    ``write_page_candidates`` -> page JSON -> dedup -> merged_final.json.
    """
    for candidate in candidates:
        candidate.evidence_refs.extend(ocr_entries)
        if channel_json is not None:
            candidate.evidence_refs.append(
                {
                    "evidence_type": "application_channel",
                    "application_channel_json": json.dumps(
                        channel_json, ensure_ascii=False
                    ),
                }
            )
        candidate.normalization_warnings.extend(ocr_warnings)
        candidate.normalization_warnings.extend(channel_warnings)


# ---------------------------------------------------------------- pipeline


def _no_images(
    url: str,
    article_text: str,
    *,
    context: ToolContext,
    extract_fn: Callable,
    llm_extractor: Callable | None,
    out_dir: str,
    runner: Callable | None,
    state_dir: str | None = None,
) -> WechatResult:
    """L1 end-state for an article with no usable images.

    ``text < 200 chars`` -> needs_manual_review "article has no content"
    (doc L1); otherwise the normal channel triage runs on the article text
    alone (a career URL in a text-only article still triggers the doc's
    deep-crawl hand-off, which is why ``out_dir``/``runner``/``state_dir``
    are threaded through).
    """
    if len(article_text) < _ARTICLE_TEXT_MIN_CHARS:
        return WechatResult(
            url, "needs_manual_review", None, [], None, False, "article has no content"
        )
    return _classify_and_extract(
        url,
        article_text,
        [],
        context=context,
        extract_fn=extract_fn,
        llm_extractor=llm_extractor,
        out_dir=out_dir,
        runner=runner,
        state_dir=state_dir,
    )


def _classify_and_extract(
    url: str,
    article_text: str,
    ocr_items: list[tuple[str, float, dict[str, str]]],
    *,
    context: ToolContext,
    extract_fn: Callable,
    llm_extractor: Callable | None,
    out_dir: str,
    runner: Callable | None,
    state_dir: str | None = None,
) -> WechatResult:
    """Channel triage -> extraction (REPLACE-OCR for B) -> enrichment.

    C (contact-only), the doc's Unknown (无渠道) row, and D (non-job)
    produce no candidates; A extracts from the combined text, B from the
    OCR text alone.  Every A/B candidate is enriched with per-image OCR
    evidence + the application-channel JSON; a career URL marks
    ``needs_deep_crawl`` and appends the doc's hand-off entry to
    ``<state_dir>/output/errors.jsonl`` (stable store, Task 10) or
    ``<out_dir>/errors.jsonl`` in single-shot mode.
    ``partial_success`` (doc L3) when the article is image-heavy AND every
    OCR text is under 100 chars.
    """
    channel, reason = classify_wechat_channel(
        article_text=article_text,
        ocr_texts=[text for text, _conf, _meta in ocr_items],
    )
    if channel == "C":
        return WechatResult(url, "needs_manual_review", channel, [], None, False, reason)
    if channel == "D":
        return WechatResult(url, "skipped", channel, [], None, False, reason)
    combined = _build_combined(article_text, ocr_items)
    channel_json, career_url, deep_crawl, channel_warnings, unknown_channel = (
        _detect_application_channel(combined, ocr_contributed=bool(ocr_items))
    )
    if unknown_channel:
        # doc L6 Step 1 Unknown (无渠道): "treat as QR-code only; mark
        # needs_manual_review" - no candidates, extraction never runs
        return WechatResult(
            url,
            "needs_manual_review",
            channel,
            [],
            None,
            False,
            "未检测到投递渠道（无邮箱、无URL、无二维码），按扫码投递处理，需人工确认投递方式",
        )
    if channel == "B":
        input_text = _build_ocr_only(ocr_items)
    else:
        input_text = combined
    candidates = _extract_from_text(
        context, input_text, url, extract_fn, llm_extractor
    )
    ocr_warnings = [
        f"部分或全部内容来自图片OCR提取，置信度: {confidence:.2f}"
        for _text, confidence, _meta in ocr_items
    ]
    _enrich_candidates(
        candidates,
        ocr_entries=[meta for _text, _conf, meta in ocr_items],
        channel_json=channel_json,
        ocr_warnings=ocr_warnings,
        channel_warnings=channel_warnings,
    )
    weak_ocr = all(
        len(text) < _OCR_USABLE_TEXT_CHARS for text, _conf, _meta in ocr_items
    )
    status = (
        "partial_success"
        if len(article_text) < _ARTICLE_TEXT_MIN_CHARS and weak_ocr
        else "succeeded"
    )
    if deep_crawl:
        entry = {
            "url": url,
            "cause": "needs_deep_crawl",
            "career_url": career_url,
            "status": "needs_deep_crawl",
            "reason": (
                "Career URL present - the article is an index; real JDs live "
                "at the career site and require a downstream deep crawl"
            ),
            "ocr_extracted_titles": [c.title for c in candidates if c.title],
            "retry_strategy": (
                "Use playwright skill to click into each category → each "
                "position → capture detail page text"
            ),
        }
        if state_dir is not None:
            # incremental mode: hand off at the stable store
            # (<state_dir>/output/errors.jsonl, idempotent by url+cause)
            append_errors_jsonl(entry, runner=runner, state_dir=state_dir)
        else:
            _append_errors_jsonl_at(Path(out_dir) / "errors.jsonl", entry)
    return WechatResult(
        url,
        status,
        channel,
        candidates,
        channel_json,
        deep_crawl,
        None,
        visible_text=input_text[:_VISIBLE_TEXT_LIMIT],
        content_hash=hashlib.sha256(input_text.encode("utf-8")).hexdigest(),
    )


def run_wechat_slice(
    url: str,
    *,
    runner: Callable | None = None,
    out_dir: str,
    context: ToolContext,
    extract_fn: Callable,
    llm_extractor: Callable | None = None,
    fetch_html_fn: Callable[[str], str] | None = None,
    download_fn: Callable[[str], bytes] | None = None,
    state_dir: str | None = None,
) -> WechatResult:
    """Run the Level 1-6 WeChat image-article pipeline for one URL.

    L1: structural public-URL guard, then fetch the article (the default
    fetch follows redirects manually, max 5 hops; a private/cloud-metadata
    target is blocked and never fetched; the image download follows the
    same per-hop re-validation).  L2/L3: parse ``<img>`` srcs (``data:``
    URIs dropped), download each image through the size filters
    (undersized/oversized skipped, never raised), OCR each surviving image
    once with the allowlisted ``ocr_image`` script.  L5: combine article
    text + OCR sections per the doc's format.  L6: channel triage A/B/C/D,
    gated extraction (REPLACE-OCR for B), application-channel enrichment,
    and the ``needs_deep_crawl`` -> errors.jsonl hand-off.  When every
    image fails OCR, a text-rich article degrades to the text path (OCR
    is supplementary); the doc's image-heavy reason applies only to
    text-poor articles.  No email/URL/QR signal is the doc's Unknown
    (无渠道) row -> needs_manual_review, no candidates.

    The ``fetch_html_fn`` / ``download_fn`` / ``runner`` seams keep unit
    tests deterministic (never live HTTP, Playwright, or LLM); the defaults
    are the real guarded fetch / download / ``ocr_image`` invocation.

    ``state_dir`` is the incremental-mode stable store: when set, the
    ``needs_deep_crawl`` hand-off appends to
    ``<state_dir>/output/errors.jsonl``; when None (single-shot), it lands
    at ``<out_dir>/errors.jsonl`` as before.
    """
    guard_reason = _l1_guard(url)
    if guard_reason is not None:
        return WechatResult(url, "blocked", None, [], None, False, guard_reason)
    fetch = fetch_html_fn or _default_fetch_html
    try:
        html = fetch(url)
    except PublicJobFetchError as exc:
        reason = (
            "unsafe_public_url"
            if exc.code == "unsafe_public_url"
            else f"ReadGZH proxy error: {exc.code}"
        )
        return WechatResult(url, "blocked", None, [], None, False, reason)
    except Exception as exc:  # noqa: BLE001 - fold any fetch failure
        return WechatResult(
            url, "blocked", None, [], None, False, f"ReadGZH proxy error: {exc}"
        )
    article_text, raw_srcs = _parse_article(html)
    image_urls = [urljoin(url, src) for src in raw_srcs if not src.startswith("data:")]
    if len(article_text) >= _ARTICLE_TEXT_MIN_CHARS:
        image_urls = image_urls[:_L2_MAX_IMAGES]
    else:
        image_urls = image_urls[:_L3_MAX_IMAGES]
    if not image_urls:
        return _no_images(
            url,
            article_text,
            context=context,
            extract_fn=extract_fn,
            llm_extractor=llm_extractor,
            out_dir=out_dir,
            runner=runner,
            state_dir=state_dir,
        )
    download = download_fn or _default_download_image
    ocr_dir = Path(out_dir) / "ocr"
    ocr_dir.mkdir(parents=True, exist_ok=True)
    ocr_items: list[tuple[str, float, dict[str, str]]] = []
    ocr_attempted = False
    for index, image_url in enumerate(image_urls):
        data = _safe_download(download, image_url)
        if (
            data is None
            or len(data) < _MIN_IMAGE_BYTES
            or len(data) > _MAX_IMAGE_BYTES
        ):
            continue
        ocr_attempted = True
        image_path = ocr_dir / f"wechat_img_{index:02d}{_image_suffix(image_url)}"
        image_path.write_bytes(data)
        ocr = _run_ocr(str(image_path), str(ocr_dir), runner=runner)
        if ocr is None:
            continue
        text, confidence, raw = ocr
        ocr_items.append(
            (text, confidence, _ocr_evidence_meta(image_url, data, raw, confidence))
        )
    if not ocr_attempted:
        # every image was skipped pre-OCR (size filter / download failure):
        # the article stands alone, exactly like the no-images L1 path
        return _no_images(
            url,
            article_text,
            context=context,
            extract_fn=extract_fn,
            llm_extractor=llm_extractor,
            out_dir=out_dir,
            runner=runner,
            state_dir=state_dir,
        )
    if not ocr_items:
        # images were downloaded but OCR produced no usable text: a
        # text-rich article degrades to the text path (doc L1/L2 - OCR is
        # supplementary, so a dead image CDN must not flip the article to
        # manual review, exactly like the sibling _no_images path); only an
        # image-heavy article (text < 200 chars) hits the doc L3 reason
        if len(article_text) >= _ARTICLE_TEXT_MIN_CHARS:
            return _no_images(
                url,
                article_text,
                context=context,
                extract_fn=extract_fn,
                llm_extractor=llm_extractor,
                out_dir=out_dir,
                runner=runner,
                state_dir=state_dir,
            )
        return WechatResult(
            url,
            "needs_manual_review",
            None,
            [],
            None,
            False,
            "image-heavy article — OCR produced no usable text",
        )
    return _classify_and_extract(
        url,
        article_text,
        ocr_items,
        context=context,
        extract_fn=extract_fn,
        llm_extractor=llm_extractor,
        out_dir=out_dir,
        runner=runner,
        state_dir=state_dir,
    )
