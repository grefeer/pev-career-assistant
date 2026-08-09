"""WeChat image-article (OCR) tool for the PEV ``job-discovery`` Skill.

Wraps the deepagents ``wechat_slice.run_wechat_slice`` (skill/job-discovery
references/wechat-image-handling.md Levels 1-6) as a PEV career tool. The
slice is a self-contained implementation - public-URL guard, guarded fetch,
image download with size filters, one allowlisted ``ocr_image`` invocation
per image, channel triage A/B/C/D, gated extraction - and depends only on a
``ToolContext`` and an extraction function, not on LangGraph, so direct
in-process reuse keeps a single source of truth instead of a second
implementation of the OCR pipeline.

The channel is gated by ``Settings.job_discovery_ocr_enabled`` (mirror of
the Playwright fallback toggle): when off, the tool reports
``needs_manual_review`` (reason ``ocr_disabled``) without touching the
network, matching the config flag's documented semantics.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field, field_validator

from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.career_skills.job_discovery import (
    ExtractedJobDetails,
    extract_observed_job_details,
)

_WECHAT_OCR_ENABLED = False
_WECHAT_OUT_DIR_DEFAULT = str(
    Path(__file__).resolve().parents[3] / "var" / "job-discovery-skill" / "ocr"
)


def enable_wechat_ocr(enabled: bool) -> None:
    """Toggle the WeChat OCR channel (called from runtime assembly)."""
    global _WECHAT_OCR_ENABLED
    _WECHAT_OCR_ENABLED = enabled


class FetchWechatArticleInput(BaseModel):
    """One public WeChat article URL (mp.weixin.qq.com or a ReadGZH mirror)."""

    url: str = Field(min_length=1, max_length=2_048)
    out_dir: str | None = None

    @field_validator("url")
    @classmethod
    def normalize_url(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("url must not be empty")
        return cleaned


class FetchWechatArticleOutput(BaseModel):
    """OCR slice outcome; a usable text carries the standard page evidence.

    The page-evidence keys (artifact_id / source_url / content_hash /
    visible_text) mirror the fetch contract so a slice that produced text
    enters the observed-evidence pool through ``_with_observed_page``;
    slices without text carry None/"" and are never persisted as evidence.
    """

    url: str
    status: str
    channel: str | None
    candidates: list[ExtractedJobDetails]
    ocr_text: str
    needs_deep_crawl: bool
    reason: str | None
    # Page-evidence keys (same contract as FetchPublicJobPageOutput).
    artifact_id: str | None = None
    source_url: str | None = None
    content_hash: str | None = None
    visible_text: str = ""


def fetch_wechat_article(
    context: ToolContext, payload: FetchWechatArticleInput
) -> FetchWechatArticleOutput:
    """OCR one WeChat image article into text + candidates (Levels 1-6).

    When the OCR channel is disabled the tool returns
    ``needs_manual_review`` (``ocr_disabled``) without any network access;
    the runtime then surfaces the human question instead of a hard failure.
    """
    if not _WECHAT_OCR_ENABLED:
        return FetchWechatArticleOutput(
            url=payload.url,
            status="needs_manual_review",
            channel=None,
            candidates=[],
            ocr_text="",
            needs_deep_crawl=False,
            reason="ocr_disabled",
        )
    from backend.app.services.deepagents_runtime.tools.skill_graphs.wechat_slice import (
        run_wechat_slice,
    )

    result = run_wechat_slice(
        payload.url,
        out_dir=payload.out_dir or _WECHAT_OUT_DIR_DEFAULT,
        context=context,
        extract_fn=extract_observed_job_details,
    )
    content_hash = result.content_hash
    return FetchWechatArticleOutput(
        url=result.url,
        status=result.status,
        channel=result.channel,
        candidates=result.candidates,
        ocr_text=result.visible_text,
        needs_deep_crawl=result.needs_deep_crawl,
        reason=result.reason,
        artifact_id=f"observed:{content_hash}" if content_hash else None,
        source_url=result.url,
        content_hash=content_hash,
        visible_text=result.visible_text,
    )
