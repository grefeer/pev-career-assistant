"""Task 9: WeChat image-article (OCR) slice unit tests.

Covers the Level 1-6 pipeline in ``wechat_slice.py`` (URL guard, HTML
parse, size filters, ``ocr_image`` invocation, combine format, channel
triage A-D, REPLACE-OCR, application-channel enrichment, errors.jsonl
hand-off) and the job-discovery subgraph wiring that routes
``wechat_pending`` URLs into the slice.  The runner / fetch / download
seams are always faked - never live HTTP, Playwright, or LLM.
"""

from __future__ import annotations

import hashlib
import json
import socket
from pathlib import Path

import pytest
import requests

from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.career_skills.job_discovery import (
    ExtractObservedJobDetailsOutput,
    ExtractedJobDetails,
    PublicJobFetchError,
)
from backend.app.services.deepagents_runtime.tools.skill_graphs import wechat_slice as ws
from backend.app.services.deepagents_runtime.tools.skill_graphs.job_discovery_graph import (
    build_job_discovery_graph,
)
from backend.app.services.deepagents_runtime.tools.skill_graphs.wechat_slice import (
    WechatResult,
    classify_wechat_channel,
    run_wechat_slice,
)
from tests.conftest import settings_override

#: A real-looking public WeChat article URL (never fetched in unit tests).
_WECHAT_URL = "https://mp.weixin.qq.com/s/abc123"


def _candidate(title: str = "后端工程师") -> ExtractedJobDetails:
    """A well-formed ExtractedJobDetails (all non-nullable fields filled)."""
    return ExtractedJobDetails(
        title=title,
        company_name="示例公司",
        locations=["上海"],
        responsibilities="负责后端服务开发",
        requirements="精通 Python",
        recruitment_types=[],
        apply_url=None,
        deadline_text=None,
        confidence=0.9,
        evidence_refs=[],
        normalization_warnings=[],
    )


def _fake_fetch_html(html: str):
    def fetch(url: str) -> str:
        assert url == _WECHAT_URL
        return html
    return fetch


def _fake_download(size: int = 20_000):
    def download(url: str) -> bytes:
        return b"x" * size
    return download


def _ocr_runner(
    text: str = (
        "招聘：后端工程师，薪资面议。岗位职责：负责后端服务开发与性能优化，"
        "参与架构设计，保障系统稳定可靠。任职要求：精通 Python 与分布式系统，"
        "3 年以上经验，本科及以上学历，良好的团队协作能力。"
        "简历投递至 hr@company.com"
    ),
    confidence: float = 0.87,
    status: str = "ok",
):
    def runner(script: str, *, cli_args: str = "", stdin: str = "") -> str:
        assert script == "ocr_image"
        assert "--engine auto" in cli_args
        assert "--out" in cli_args
        if status == "error":
            return json.dumps({"status": "error", "error": "File not found: x"})
        return json.dumps(
            {
                "status": "ok",
                "image_path": cli_args.split()[0],
                "content_hash": "sha256_abc",
                "dimensions": {"width": 1080, "height": 3200},
                "format": "png",
                "engine": "paddleocr",
                "full_text": text,
                "confidence": confidence,
                "text_length": len(text),
                "warnings": [],
                "needs_manual_review": False,
            }
        )
    return runner


def _fake_extract(seen: dict) -> object:
    def extract(context: ToolContext, payload) -> ExtractObservedJobDetailsOutput:
        evidence = context.metadata["observed_public_evidence"][-1]
        seen["input"] = evidence["visible_text"]
        seen["artifact"] = payload.artifact_id
        return ExtractObservedJobDetailsOutput(
            source_artifact_id=payload.artifact_id,
            source_url=_WECHAT_URL,
            content_hash=payload.artifact_id,
            candidates=[_candidate()],
        )
    return extract


# ---------------------------------------------------------------- channels


def test_channel_a_job_content_in_text() -> None:
    channel, reason = classify_wechat_channel(
        article_text="岗位：前端工程师。公司：某某科技。负责……",
        ocr_texts=[],
    )
    assert channel == "A"
    assert reason is None


def test_channel_b_job_content_only_in_ocr() -> None:
    channel, reason = classify_wechat_channel(
        article_text="欢迎转发",
        ocr_texts=["招聘：后端工程师，薪资面议，简历投递……"],
    )
    assert channel == "B"
    assert reason is None


def test_channel_c_contact_only() -> None:
    channel, reason = classify_wechat_channel(article_text="加微信: abc", ocr_texts=[])
    assert channel == "C"
    assert reason is not None


def test_channel_d_non_job_promotional() -> None:
    channel, reason = classify_wechat_channel(article_text="双十一大促，全场五折", ocr_texts=[])
    assert channel == "D"
    assert reason is not None


# ------------------------------------------------------------ errors.jsonl


def test_run_wechat_slice_deep_crawl_handoff_at_state_dir(tmp_path) -> None:
    # incremental mode (state_dir set): the deep-crawl hand-off lands at the
    # stable store <state_dir>/output/errors.jsonl, never <out_dir>/errors.jsonl
    html = "<html><body><p>欢迎转发</p><img src='https://mmbiz.qpic.cn/a'></body></html>"
    ocr_text = (
        "招聘：后端工程师，投递：jereh.zhiye.com/campus。岗位职责：负责后端服务开发与"
        "性能优化，参与架构设计，保障系统稳定可靠。任职要求：精通 Python 与分布式系统，"
        "3 年以上经验，本科及以上学历，良好的团队协作能力。"
    )
    result = run_wechat_slice(
        _WECHAT_URL,
        runner=_ocr_runner(text=ocr_text),
        out_dir=str(tmp_path),
        state_dir=str(tmp_path),
        context=ToolContext(user_id="", run_id="", metadata={}),
        extract_fn=_fake_extract({}),
        fetch_html_fn=_fake_fetch_html(html),
        download_fn=_fake_download(),
    )
    assert result.needs_deep_crawl is True
    lines = (
        (tmp_path / "output" / "errors.jsonl")
        .read_text(encoding="utf-8")
        .strip()
        .splitlines()
    )
    assert len(lines) == 1
    assert json.loads(lines[0])["cause"] == "needs_deep_crawl"
    assert not (tmp_path / "errors.jsonl").exists()


# ------------------------------------------------------ full pipeline (B)


def test_run_wechat_slice_full_pipeline_channel_b(tmp_path) -> None:
    html = (
        "<html><body><p>欢迎转发</p>"
        "<img src='https://mmbiz.qpic.cn/mmbiz_png/abc/640?wx_fmt=png'>"
        "<img src='data:image/png;base64,AAAA'>"
        "</body></html>"
    )
    seen: dict = {}
    downloaded: list[str] = []

    def download(url: str) -> bytes:
        downloaded.append(url)
        return b"x" * 20_000

    result = run_wechat_slice(
        _WECHAT_URL,
        runner=_ocr_runner(),
        out_dir=str(tmp_path),
        context=ToolContext(user_id="", run_id="", metadata={}),
        extract_fn=_fake_extract(seen),
        fetch_html_fn=_fake_fetch_html(html),
        download_fn=download,
    )
    assert result.status == "succeeded"
    assert result.channel == "B"
    assert len(result.candidates) == 1
    # application channel enrichment: the email lives in the OCR text
    assert result.application_channel_json == {
        "type": "email",
        "value": "hr@company.com",
    }
    assert result.needs_deep_crawl is False
    assert result.reason is None
    # REPLACE-OCR: the extraction input is the OCR text (with the doc's
    # combine marker), never the contact-only article text
    assert seen["input"].startswith("=== 图片1 OCR内容")
    assert "=== 文章正文 ===" not in seen["input"]
    assert "欢迎转发" not in seen["input"]
    # evidence identity = full sha256 of the extraction input text
    assert seen["artifact"] == hashlib.sha256(seen["input"].encode("utf-8")).hexdigest()
    # the data: URI is dropped, only the mmbiz image is downloaded
    assert downloaded == ["https://mmbiz.qpic.cn/mmbiz_png/abc/640?wx_fmt=png"]
    # OCR images were persisted under out_dir/ocr (doc: output/ocr)
    assert (tmp_path / "ocr" / "wechat_img_00.png").exists()
    # enrichment rides in evidence_refs / normalization_warnings
    candidate = result.candidates[0]
    ocr_ref = next(ref for ref in candidate.evidence_refs if ref["evidence_type"] == "ocr_text")
    assert ocr_ref["url"] == "https://mmbiz.qpic.cn/mmbiz_png/abc/640?wx_fmt=png"
    assert ocr_ref["content_hash"].startswith("sha256_")
    ocr_meta = json.loads(ocr_ref["metadata"])
    assert ocr_meta["ocr_engine"] == "paddleocr"
    assert ocr_meta["ocr_confidence"] == 0.87
    assert ocr_meta["image_dimensions"] == {"width": 1080, "height": 3200}
    assert ocr_meta["is_long_image"] is True
    channel_ref = candidate.evidence_refs[-1]
    assert channel_ref["evidence_type"] == "application_channel"
    assert json.loads(channel_ref["application_channel_json"]) == {
        "type": "email",
        "value": "hr@company.com",
    }
    assert "部分或全部内容来自图片OCR提取，置信度: 0.87" in candidate.normalization_warnings
    assert "从微信推文OCR提取，申请通过邮箱投递" in candidate.normalization_warnings


def test_run_wechat_slice_channel_a_extracts_from_combined_text(tmp_path) -> None:
    # ≥ 200 chars -> Level 2 path (images may carry supplementary JDs); the
    # email keeps the article's channel signal present so the L6 triage
    # classifies it as email-channel, not Unknown
    article = (
        "岗位：前端工程师。公司：某某科技。负责前端开发。"
        + ("负责前端开发。团队氛围好，弹性工作，带薪年假。" * 10)
        + "简历投递至 hr@example.com。"
    )
    html = (
        f"<html><body><p>{article}</p>"
        "<img src='https://mmbiz.qpic.cn/a/640'></body></html>"
    )
    seen: dict = {}
    result = run_wechat_slice(
        _WECHAT_URL,
        runner=_ocr_runner(text="兼职信息：招募发传单人员", confidence=0.8),
        out_dir=str(tmp_path),
        context=ToolContext(user_id="", run_id="", metadata={}),
        extract_fn=_fake_extract(seen),
        fetch_html_fn=_fake_fetch_html(html),
        download_fn=_fake_download(),
    )
    assert result.status == "succeeded"
    assert result.channel == "A"
    # A = job content in article text -> extraction input is the combined
    # text (article + OCR sections in the doc's L4 combine format)
    assert seen["input"].startswith("=== 文章正文 ===")
    assert "=== 图片1 OCR内容 (置信度: 0.80) ===" in seen["input"]
    assert "岗位：前端工程师" in seen["input"]


def test_run_wechat_slice_channel_c_contact_only_pipeline(tmp_path) -> None:
    # a long contact-only article (≥ 200 chars, no job content) passes the
    # L1 length gate and is triaged as contact-only -> needs_manual_review
    # with the channel surfaced and no extraction
    html = (
        "<html><body><p>" + ("加微信：abc123，欢迎联系咨询。电话咨询、到店参观均可。" * 10)
        + "</p></body></html>"
    )
    calls: list[str] = []

    def extract(context, payload) -> ExtractObservedJobDetailsOutput:
        calls.append(payload.artifact_id)
        return ExtractObservedJobDetailsOutput(
            source_artifact_id=payload.artifact_id,
            source_url=_WECHAT_URL,
            content_hash=payload.artifact_id,
            candidates=[_candidate()],
        )

    result = run_wechat_slice(
        _WECHAT_URL,
        runner=_ocr_runner(),
        out_dir=str(tmp_path),
        context=ToolContext(user_id="", run_id="", metadata={}),
        extract_fn=extract,
        fetch_html_fn=_fake_fetch_html(html),
        download_fn=_fake_download(),
    )
    assert result.status == "needs_manual_review"
    assert result.channel == "C"
    assert result.reason is not None
    assert result.candidates == []
    assert calls == []  # extraction never ran


def test_run_wechat_slice_channel_d_skips_extraction(tmp_path) -> None:
    # ≥ 200 chars of promotional content (no images): L1 passes it to normal
    # extraction, where the channel triage classifies it as non-job -> skipped
    html = "<html><body><p>" + ("双十一大促，全场五折，欢迎选购！品质保证，限时特惠。" * 10) + "</p></body></html>"
    calls: list[str] = []

    def extract(context, payload) -> ExtractObservedJobDetailsOutput:
        calls.append(payload.artifact_id)
        return ExtractObservedJobDetailsOutput(
            source_artifact_id=payload.artifact_id,
            source_url=_WECHAT_URL,
            content_hash=payload.artifact_id,
            candidates=[_candidate()],
        )

    result = run_wechat_slice(
        _WECHAT_URL,
        runner=_ocr_runner(),
        out_dir=str(tmp_path),
        context=ToolContext(user_id="", run_id="", metadata={}),
        extract_fn=extract,
        fetch_html_fn=_fake_fetch_html(html),
        download_fn=_fake_download(),
    )
    assert result.status == "skipped"
    assert result.channel == "D"
    assert result.reason is not None
    assert result.candidates == []
    assert calls == []  # extraction never ran


# ------------------------------------------------- article fetch blocked


def test_run_wechat_slice_article_fetch_blocked_non_public(tmp_path) -> None:
    fetch_calls: list[str] = []

    def fetch(url: str) -> str:
        fetch_calls.append(url)
        return "<html></html>"

    for bad_url in ("file:///C:/x", "http://user:pass@example.com/x", "http://127.0.0.1/x"):
        result = run_wechat_slice(
            bad_url,
            runner=_ocr_runner(),
            out_dir=str(tmp_path),
            context=ToolContext(user_id="", run_id="", metadata={}),
            extract_fn=_fake_extract({}),
            fetch_html_fn=fetch,
            download_fn=_fake_download(),
        )
        assert result.status == "blocked"
        assert result.reason == "unsafe_public_url"
        assert result.candidates == []
    assert fetch_calls == []  # the guarded URL is never fetched


def test_run_wechat_slice_article_fetch_blocked_nav_failure(tmp_path) -> None:
    def fetch(url: str) -> str:
        raise PublicJobFetchError("public_fetch_failed")

    result = run_wechat_slice(
        _WECHAT_URL,
        runner=_ocr_runner(),
        out_dir=str(tmp_path),
        context=ToolContext(user_id="", run_id="", metadata={}),
        extract_fn=_fake_extract({}),
        fetch_html_fn=fetch,
        download_fn=_fake_download(),
    )
    assert result.status == "blocked"
    assert result.reason == "ReadGZH proxy error: public_fetch_failed"
    assert result.candidates == []


def test_run_wechat_slice_article_fetch_blocked_generic_failure(tmp_path) -> None:
    def fetch(url: str) -> str:
        raise RuntimeError("connection refused")

    result = run_wechat_slice(
        _WECHAT_URL,
        runner=_ocr_runner(),
        out_dir=str(tmp_path),
        context=ToolContext(user_id="", run_id="", metadata={}),
        extract_fn=_fake_extract({}),
        fetch_html_fn=fetch,
        download_fn=_fake_download(),
    )
    assert result.status == "blocked"
    assert result.reason.startswith("ReadGZH proxy error")
    assert result.candidates == []


# ------------------------------------------------------ OCR failure folds


def test_ocr_image_failure_folds(tmp_path) -> None:
    def runner(script: str, *, cli_args: str = "", stdin: str = "") -> str:
        assert script == "ocr_image"
        return "ERROR: ocr_image timed out after 900s"

    html = "<html><body><p>欢迎转发</p><img src='https://mmbiz.qpic.cn/a'></body></html>"
    result = run_wechat_slice(
        _WECHAT_URL,
        runner=runner,
        out_dir=str(tmp_path),
        context=ToolContext(user_id="", run_id="", metadata={}),
        extract_fn=_fake_extract({}),
        fetch_html_fn=_fake_fetch_html(html),
        download_fn=_fake_download(),
    )
    # image-heavy article, zero usable OCR -> doc L3 degradation, not a crash
    assert result.status == "needs_manual_review"
    assert result.reason == "image-heavy article — OCR produced no usable text"
    assert result.candidates == []


def test_run_wechat_slice_text_rich_ocr_failed_degrades_to_text_path(tmp_path) -> None:
    # a ≥200-char job article whose images all fail OCR degrades to the
    # text path (doc L1/L2: OCR is supplementary - a dead image CDN must
    # not flip the article to needs_manual_review); the image-heavy reason
    # is reserved for text-poor articles (test_ocr_image_failure_folds)
    article = (
        "岗位：前端工程师。公司：某某科技。负责前端开发与性能优化，参与架构设计。"
        "简历投递至 hr@example.com。团队氛围好，弹性工作，带薪年假。" * 6
    )
    html = f"<html><body><p>{article}</p><img src='https://mmbiz.qpic.cn/a'></body></html>"
    seen: dict = {}

    def runner(script: str, *, cli_args: str = "", stdin: str = "") -> str:
        assert script == "ocr_image"
        return "ERROR: ocr_image timed out after 900s"

    result = run_wechat_slice(
        _WECHAT_URL,
        runner=runner,
        out_dir=str(tmp_path),
        context=ToolContext(user_id="", run_id="", metadata={}),
        extract_fn=_fake_extract(seen),
        fetch_html_fn=_fake_fetch_html(html),
        download_fn=_fake_download(),
    )
    assert result.status == "succeeded"
    assert result.channel == "A"
    assert result.reason is None
    assert len(result.candidates) == 1
    # extraction ran on the article text alone (no OCR sections survived)
    assert seen["input"].startswith("=== 文章正文 ===")
    assert "=== 图片1 OCR内容" not in seen["input"]


def test_ocr_image_error_status_folds(tmp_path) -> None:
    html = "<html><body><p>欢迎转发</p><img src='https://mmbiz.qpic.cn/a'></body></html>"
    result = run_wechat_slice(
        _WECHAT_URL,
        runner=_ocr_runner(status="error"),
        out_dir=str(tmp_path),
        context=ToolContext(user_id="", run_id="", metadata={}),
        extract_fn=_fake_extract({}),
        fetch_html_fn=_fake_fetch_html(html),
        download_fn=_fake_download(),
    )
    assert result.status == "needs_manual_review"
    assert result.reason == "image-heavy article — OCR produced no usable text"
    assert result.candidates == []


def test_ocr_image_unparsable_output_folds(tmp_path) -> None:
    def runner(script: str, *, cli_args: str = "", stdin: str = "") -> str:
        return "not json at all"

    html = "<html><body><p>欢迎转发</p><img src='https://mmbiz.qpic.cn/a'></body></html>"
    result = run_wechat_slice(
        _WECHAT_URL,
        runner=runner,
        out_dir=str(tmp_path),
        context=ToolContext(user_id="", run_id="", metadata={}),
        extract_fn=_fake_extract({}),
        fetch_html_fn=_fake_fetch_html(html),
        download_fn=_fake_download(),
    )
    assert result.status == "needs_manual_review"
    assert result.reason == "image-heavy article — OCR produced no usable text"


# ---------------------------------------------------- no-content / partial


def test_run_wechat_slice_article_no_content(tmp_path) -> None:
    html = "<html><body><p>hi</p></body></html>"  # < 200 chars, no images
    result = run_wechat_slice(
        _WECHAT_URL,
        runner=_ocr_runner(),
        out_dir=str(tmp_path),
        context=ToolContext(user_id="", run_id="", metadata={}),
        extract_fn=_fake_extract({}),
        fetch_html_fn=_fake_fetch_html(html),
        download_fn=_fake_download(),
    )
    assert result.status == "needs_manual_review"
    assert result.reason == "article has no content"
    assert result.candidates == []


def test_run_wechat_slice_partial_success_weak_ocr(tmp_path) -> None:
    html = "<html><body><p>欢迎转发</p><img src='https://mmbiz.qpic.cn/a'></body></html>"
    # the email keeps the text under 100 chars (weak OCR) while providing
    # the channel signal the L6 triage needs (not Unknown)
    result = run_wechat_slice(
        _WECHAT_URL,
        runner=_ocr_runner(text="招聘：后端工程师，详情见图片，投递至 hr@x.com。", confidence=0.45),
        out_dir=str(tmp_path),
        context=ToolContext(user_id="", run_id="", metadata={}),
        extract_fn=_fake_extract({}),
        fetch_html_fn=_fake_fetch_html(html),
        download_fn=_fake_download(),
    )
    assert result.status == "partial_success"
    assert result.channel == "B"
    assert len(result.candidates) == 1
    # low-confidence OCR is flagged in normalization_warnings
    assert "部分或全部内容来自图片OCR提取，置信度: 0.45" in result.candidates[0].normalization_warnings


# ------------------------------------------- application channel + deep crawl


def test_run_wechat_slice_needs_deep_crawl(tmp_path) -> None:
    html = "<html><body><p>欢迎转发</p><img src='https://mmbiz.qpic.cn/a'></body></html>"
    # ≥ 100 chars of OCR text (doc L3 "usable") so the run is a full
    # succeeded article whose only channel signal is the career URL
    ocr_text = (
        "招聘：后端工程师，投递：jereh.zhiye.com/campus。岗位职责：负责后端服务开发与"
        "性能优化，参与架构设计，保障系统稳定可靠。任职要求：精通 Python 与分布式系统，"
        "3 年以上经验，本科及以上学历，良好的团队协作能力。"
    )
    result = run_wechat_slice(
        _WECHAT_URL,
        runner=_ocr_runner(text=ocr_text),
        out_dir=str(tmp_path),
        context=ToolContext(user_id="", run_id="", metadata={}),
        extract_fn=_fake_extract({}),
        fetch_html_fn=_fake_fetch_html(html),
        download_fn=_fake_download(),
    )
    assert result.status == "succeeded"
    assert result.needs_deep_crawl is True
    # scheme-less career URL is reconstructed per the doc's pattern table
    assert result.application_channel_json == {
        "type": "url",
        "value": "https://jereh.zhiye.com/campus",
    }
    # the reconstruction warning carries the rebuilt URL as a suffix
    assert any(
        "OCR可能损坏了URL，已按模式重建" in w
        for w in result.candidates[0].normalization_warnings
    )
    # the hand-off entry is persisted to errors.jsonl
    lines = (tmp_path / "errors.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["url"] == _WECHAT_URL
    assert entry["cause"] == "needs_deep_crawl"
    assert entry["career_url"] == "https://jereh.zhiye.com/campus"
    assert entry["status"] == "needs_deep_crawl"
    assert entry["ocr_extracted_titles"] == ["后端工程师"]
    assert "playwright skill" in entry["retry_strategy"]


def test_run_wechat_slice_primary_alternative_channels(tmp_path) -> None:
    html = "<html><body><p>欢迎转发</p><img src='https://mmbiz.qpic.cn/a'></body></html>"
    result = run_wechat_slice(
        _WECHAT_URL,
        runner=_ocr_runner(text="招聘：后端工程师，投递：https://jereh.zhiye.com/campus，或 hr@jereh.com"),
        out_dir=str(tmp_path),
        context=ToolContext(user_id="", run_id="", metadata={}),
        extract_fn=_fake_extract({}),
        fetch_html_fn=_fake_fetch_html(html),
        download_fn=_fake_download(),
    )
    assert result.application_channel_json == {
        "primary": {"type": "url", "value": "https://jereh.zhiye.com/campus"},
        "alternative": {"type": "email", "value": "hr@jereh.com"},
    }
    assert result.needs_deep_crawl is True


def test_run_wechat_slice_qr_only_channel(tmp_path) -> None:
    html = "<html><body><p>欢迎转发</p><img src='https://mmbiz.qpic.cn/a'></body></html>"
    result = run_wechat_slice(
        _WECHAT_URL,
        runner=_ocr_runner(text="招聘：后端工程师，扫描下方二维码投递简历"),
        out_dir=str(tmp_path),
        context=ToolContext(user_id="", run_id="", metadata={}),
        extract_fn=_fake_extract({}),
        fetch_html_fn=_fake_fetch_html(html),
        download_fn=_fake_download(),
    )
    assert result.application_channel_json is None
    assert result.needs_deep_crawl is False
    assert "仅支持扫码投递，无邮箱或官网链接" in result.candidates[0].normalization_warnings


def test_run_wechat_slice_unknown_channel_marks_manual_review(tmp_path) -> None:
    # no email/URL/QR signal in the combined text -> doc L6 Step 1 Unknown
    # (无渠道): treat as QR-code only and mark needs_manual_review with the
    # doc's reason, no candidates, extraction never runs
    html = "<html><body><p>欢迎转发</p><img src='https://mmbiz.qpic.cn/a'></body></html>"
    calls: list[str] = []

    def extract(context, payload) -> ExtractObservedJobDetailsOutput:
        calls.append(payload.artifact_id)
        return ExtractObservedJobDetailsOutput(
            source_artifact_id=payload.artifact_id,
            source_url=_WECHAT_URL,
            content_hash=payload.artifact_id,
            candidates=[_candidate()],
        )

    result = run_wechat_slice(
        _WECHAT_URL,
        runner=_ocr_runner(
            text=(
                "招聘：后端工程师。岗位职责：负责后端服务开发与性能优化，参与架构设计，"
                "保障系统稳定可靠。任职要求：精通 Python 与分布式系统，3 年以上经验，"
                "本科及以上学历，良好的团队协作能力。"
            )
        ),
        out_dir=str(tmp_path),
        context=ToolContext(user_id="", run_id="", metadata={}),
        extract_fn=extract,
        fetch_html_fn=_fake_fetch_html(html),
        download_fn=_fake_download(),
    )
    assert result.status == "needs_manual_review"
    assert result.reason is not None
    assert "扫码投递" in result.reason  # doc: treat as QR-code only
    assert result.candidates == []
    assert calls == []  # extraction never ran


def test_run_wechat_slice_unknown_channel_text_rich_manual_review(tmp_path) -> None:
    # a ≥200-char job article with no email/URL/QR signal is Unknown too -
    # the doc's L6 Step 1 row applies to the article body text as well
    article = (
        "岗位：前端工程师。公司：某某科技。负责前端开发与性能优化，参与架构设计。" * 6
    )
    html = f"<html><body><p>{article}</p></body></html>"
    result = run_wechat_slice(
        _WECHAT_URL,
        runner=_ocr_runner(),
        out_dir=str(tmp_path),
        context=ToolContext(user_id="", run_id="", metadata={}),
        extract_fn=_fake_extract({}),
        fetch_html_fn=_fake_fetch_html(html),
        download_fn=_fake_download(),
    )
    assert result.status == "needs_manual_review"
    assert "扫码投递" in result.reason
    assert result.candidates == []


# --------------------------------------------------------- image handling


def test_run_wechat_slice_undersized_image_skipped(tmp_path) -> None:
    ocr_calls: list[str] = []

    def runner(script: str, *, cli_args: str = "", stdin: str = "") -> str:
        ocr_calls.append(cli_args)
        return _ocr_runner()(script, cli_args=cli_args, stdin=stdin)

    html = "<html><body><p>欢迎转发</p><img src='https://mmbiz.qpic.cn/a'></body></html>"
    result = run_wechat_slice(
        _WECHAT_URL,
        runner=runner,
        out_dir=str(tmp_path),
        context=ToolContext(user_id="", run_id="", metadata={}),
        extract_fn=_fake_extract({}),
        fetch_html_fn=_fake_fetch_html(html),
        download_fn=_fake_download(size=100),  # < 10KB
    )
    assert ocr_calls == []  # undersized images are skipped, never OCR'd
    assert result.status == "needs_manual_review"
    assert result.reason == "article has no content"


def test_run_wechat_slice_oversized_image_skipped(tmp_path) -> None:
    ocr_calls: list[str] = []

    def runner(script: str, *, cli_args: str = "", stdin: str = "") -> str:
        ocr_calls.append(cli_args)
        return _ocr_runner()(script, cli_args=cli_args, stdin=stdin)

    html = "<html><body><p>欢迎转发</p><img src='https://mmbiz.qpic.cn/a'></body></html>"
    result = run_wechat_slice(
        _WECHAT_URL,
        runner=runner,
        out_dir=str(tmp_path),
        context=ToolContext(user_id="", run_id="", metadata={}),
        extract_fn=_fake_extract({}),
        fetch_html_fn=_fake_fetch_html(html),
        download_fn=_fake_download(size=20 * 1024 * 1024 + 1),  # > 20MiB
    )
    assert ocr_calls == []
    assert result.status == "needs_manual_review"


def test_run_wechat_slice_download_failure_skips_image(tmp_path) -> None:
    def download(url: str) -> bytes:
        raise OSError("connection reset")

    html = "<html><body><p>欢迎转发</p><img src='https://mmbiz.qpic.cn/a'></body></html>"
    result = run_wechat_slice(
        _WECHAT_URL,
        runner=_ocr_runner(),
        out_dir=str(tmp_path),
        context=ToolContext(user_id="", run_id="", metadata={}),
        extract_fn=_fake_extract({}),
        fetch_html_fn=_fake_fetch_html(html),
        download_fn=download,
    )
    assert result.status == "needs_manual_review"
    assert result.reason == "article has no content"


def test_run_wechat_slice_private_image_skipped(tmp_path) -> None:
    downloaded: list[str] = []

    def download(url: str) -> bytes:
        downloaded.append(url)
        return b"x" * 20_000

    html = (
        "<html><body><p>欢迎转发</p>"
        "<img src='http://127.0.0.1/steal'>"
        "<img src='https://mmbiz.qpic.cn/a'></body></html>"
    )
    result = run_wechat_slice(
        _WECHAT_URL,
        runner=_ocr_runner(),
        out_dir=str(tmp_path),
        context=ToolContext(user_id="", run_id="", metadata={}),
        extract_fn=_fake_extract({}),
        fetch_html_fn=_fake_fetch_html(html),
        download_fn=download,
    )
    assert downloaded == ["https://mmbiz.qpic.cn/a"]  # private image never fetched
    assert result.status == "succeeded"


def test_run_wechat_slice_l3_caps_images_at_ten(tmp_path) -> None:
    images = "".join(
        f"<img src='https://mmbiz.qpic.cn/img{i}/640'>" for i in range(12)
    )
    html = f"<html><body><p>欢迎转发</p>{images}</body></html>"
    ocr_calls: list[str] = []

    def runner(script: str, *, cli_args: str = "", stdin: str = "") -> str:
        ocr_calls.append(cli_args)
        return _ocr_runner()(script, cli_args=cli_args, stdin=stdin)

    run_wechat_slice(
        _WECHAT_URL,
        runner=runner,
        out_dir=str(tmp_path),
        context=ToolContext(user_id="", run_id="", metadata={}),
        extract_fn=_fake_extract({}),
        fetch_html_fn=_fake_fetch_html(html),
        download_fn=_fake_download(),
    )
    # image-heavy article (text < 200 chars): max 10 images OCR'd
    assert len(ocr_calls) == 10


def test_run_wechat_slice_l2_caps_images_at_five(tmp_path) -> None:
    long_article = "岗位：后端工程师。岗位职责：负责后端服务开发。任职要求：精通 Python。" * 10
    images = "".join(
        f"<img src='https://mmbiz.qpic.cn/img{i}/640'>" for i in range(7)
    )
    html = f"<html><body><p>{long_article}</p>{images}</body></html>"
    ocr_calls: list[str] = []

    def runner(script: str, *, cli_args: str = "", stdin: str = "") -> str:
        ocr_calls.append(cli_args)
        return _ocr_runner()(script, cli_args=cli_args, stdin=stdin)

    run_wechat_slice(
        _WECHAT_URL,
        runner=runner,
        out_dir=str(tmp_path),
        context=ToolContext(user_id="", run_id="", metadata={}),
        extract_fn=_fake_extract({}),
        fetch_html_fn=_fake_fetch_html(html),
        download_fn=_fake_download(),
    )
    # text-rich article (≥ 200 chars): max 5 images OCR'd
    assert len(ocr_calls) == 5


def test_run_wechat_slice_resolves_relative_image_urls(tmp_path) -> None:
    html = (
        "<html><body><p>欢迎转发</p>"
        "<img src='/img/a.jpg'><img src='img/b'></body></html>"
    )
    downloaded: list[str] = []
    result = run_wechat_slice(
        _WECHAT_URL,
        runner=_ocr_runner(),
        out_dir=str(tmp_path),
        context=ToolContext(user_id="", run_id="", metadata={}),
        extract_fn=_fake_extract({}),
        fetch_html_fn=_fake_fetch_html(html),
        download_fn=lambda url: downloaded.append(url) or b"x" * 20_000,
    )
    assert downloaded == ["https://mp.weixin.qq.com/img/a.jpg", "https://mp.weixin.qq.com/s/img/b"]
    assert result.status == "succeeded"


# --------------------------------------------------------- LLM gate path


def test_run_wechat_slice_llm_gate_path(tmp_path) -> None:
    article = (
        "岗位：后端工程师。欢迎关注我们的招聘公众号，每天更新技术资讯、团队动态和"
        "职业发展内容，涵盖软件开发、工程效率、团队协作等多个方向，期待你的加入。"
        "简历投递至 hr@example.com。"
    ) * 3
    html = f"<html><body><p>{article}</p></body></html>"
    calls: list[str] = []

    def llm_extractor(context, payload) -> ExtractObservedJobDetailsOutput:
        calls.append(payload.artifact_id)
        return ExtractObservedJobDetailsOutput(
            source_artifact_id=payload.artifact_id,
            source_url=_WECHAT_URL,
            content_hash=payload.artifact_id,
            candidates=[_candidate("LLM 补充职位")],
        )

    def unexpected_extract(context, payload):
        raise AssertionError("extract_fn must not run when llm_extractor is set")

    result = run_wechat_slice(
        _WECHAT_URL,
        runner=_ocr_runner(),
        out_dir=str(tmp_path),
        context=ToolContext(user_id="", run_id="", metadata={}),
        extract_fn=unexpected_extract,
        llm_extractor=llm_extractor,
        fetch_html_fn=_fake_fetch_html(html),
        download_fn=_fake_download(),
    )
    assert calls  # gate fired (regex output was low-confidence)
    assert any(c.title == "LLM 补充职位" for c in result.candidates)


# ---------------------------------------------------- coverage-gap edges


def test_run_wechat_slice_default_seams(monkeypatch, tmp_path) -> None:
    # no fetch_html_fn / download_fn / runner: the real defaults are used,
    # each stubbed so no live HTTP or subprocess runs
    html = "<html><body><p>欢迎转发</p><img src='https://mmbiz.qpic.cn/a'></body></html>"
    monkeypatch.setattr(ws, "_default_fetch_html", lambda url: html)
    monkeypatch.setattr(ws, "_default_download_image", lambda url: b"x" * 20_000)
    monkeypatch.setattr(ws, "run_skill_script", _ocr_runner())
    result = run_wechat_slice(
        _WECHAT_URL,
        out_dir=str(tmp_path),
        context=ToolContext(user_id="", run_id="", metadata={}),
        extract_fn=_fake_extract({}),
    )
    assert result.status == "succeeded"
    assert result.channel == "B"
    assert result.application_channel_json == {"type": "email", "value": "hr@company.com"}


def test_run_wechat_slice_fetch_unsafe_public_url_reason(tmp_path) -> None:
    def fetch(url: str) -> str:
        raise PublicJobFetchError("unsafe_public_url")

    result = run_wechat_slice(
        _WECHAT_URL,
        runner=_ocr_runner(),
        out_dir=str(tmp_path),
        context=ToolContext(user_id="", run_id="", metadata={}),
        extract_fn=_fake_extract({}),
        fetch_html_fn=fetch,
        download_fn=_fake_download(),
    )
    assert result.status == "blocked"
    assert result.reason == "unsafe_public_url"
    assert result.candidates == []


def test_run_wechat_slice_extractor_evidence_error_folds(tmp_path) -> None:
    def extract(context, payload) -> ExtractObservedJobDetailsOutput:
        raise PublicJobFetchError("evidence_not_found")

    html = "<html><body><p>欢迎转发</p><img src='https://mmbiz.qpic.cn/a'></body></html>"
    result = run_wechat_slice(
        _WECHAT_URL,
        runner=_ocr_runner(),
        out_dir=str(tmp_path),
        context=ToolContext(user_id="", run_id="", metadata={}),
        extract_fn=extract,
        fetch_html_fn=_fake_fetch_html(html),
        download_fn=_fake_download(),
    )
    # a PublicJobFetchError from the extractor folds to [] - never a crash
    assert result.status == "succeeded"
    assert result.channel == "B"
    assert result.candidates == []


def test_run_wechat_slice_email_in_article_text(tmp_path) -> None:
    # channel A with the email in the article text (no OCR): the non-OCR
    # "申请通过邮箱投递" warning flavor and the email channel JSON
    article = (
        "岗位：前端工程师。公司：某某科技。负责前端开发。"
        + "负责前端开发。团队氛围好，弹性工作，带薪年假。" * 10
        + "简历投递至 hr@example.com"
    )
    html = f"<html><body><p>{article}</p></body></html>"
    result = run_wechat_slice(
        _WECHAT_URL,
        runner=_ocr_runner(),
        out_dir=str(tmp_path),
        context=ToolContext(user_id="", run_id="", metadata={}),
        extract_fn=_fake_extract({}),
        fetch_html_fn=_fake_fetch_html(html),
        download_fn=_fake_download(),
    )
    assert result.status == "succeeded"
    assert result.channel == "A"
    assert result.application_channel_json == {"type": "email", "value": "hr@example.com"}
    assert result.needs_deep_crawl is False
    assert "申请通过邮箱投递" in result.candidates[0].normalization_warnings
    assert "从微信推文OCR提取，申请通过邮箱投递" not in result.candidates[0].normalization_warnings


def test_run_wechat_slice_non_career_url_ignored(tmp_path) -> None:
    # a regular (non-career) URL in the text does not set the channel; the
    # email does - and no deep-crawl hand-off happens
    ocr_text = (
        "招聘：后端工程师，详情见 https://example.com/jobs，投递至 hr@x.com。岗位职责："
        "负责后端服务开发与性能优化，参与架构设计，保障系统稳定可靠。任职要求：精通 "
        "Python 与分布式系统，3 年以上经验，本科及以上学历。"
    )
    html = "<html><body><p>欢迎转发</p><img src='https://mmbiz.qpic.cn/a'></body></html>"
    result = run_wechat_slice(
        _WECHAT_URL,
        runner=_ocr_runner(text=ocr_text),
        out_dir=str(tmp_path),
        context=ToolContext(user_id="", run_id="", metadata={}),
        extract_fn=_fake_extract({}),
        fetch_html_fn=_fake_fetch_html(html),
        download_fn=_fake_download(),
    )
    assert result.application_channel_json == {"type": "email", "value": "hr@x.com"}
    assert result.needs_deep_crawl is False


def test_run_wechat_slice_corrupted_career_token_reconstructed(tmp_path) -> None:
    # a career-domain mention the bare-URL regex cannot consume (no
    # subdomain label) is reconstructed from the doc's pattern table
    ocr_text = (
        "招聘：后端工程师，详情投递：zhiye.com 或 feishu.cn。岗位职责：负责后端服务开发"
        "与性能优化，参与架构设计，保障系统稳定可靠。任职要求：精通 Python 与分布式系统，"
        "3 年以上经验，本科及以上学历。"
    )
    html = "<html><body><p>欢迎转发</p><img src='https://mmbiz.qpic.cn/a'></body></html>"
    result = run_wechat_slice(
        _WECHAT_URL,
        runner=_ocr_runner(text=ocr_text),
        out_dir=str(tmp_path),
        context=ToolContext(user_id="", run_id="", metadata={}),
        extract_fn=_fake_extract({}),
        fetch_html_fn=_fake_fetch_html(html),
        download_fn=_fake_download(),
    )
    assert result.application_channel_json == {"type": "url", "value": "*.zhiye.com/*"}
    assert result.needs_deep_crawl is True


def test_run_wechat_slice_doc_zhlye_corrupted_example_reconstructed(tmp_path) -> None:
    # the doc's canonical corrupted-URL example (doc L6 Step 1 "Important":
    # zhiye -> zhlye) is recognized and reconstructed to the doc's
    # *.zhiye.com/* pattern with the uncertainty flagged
    ocr_text = (
        "招聘：后端工程师，投递：jereh.zhlye.com/campus。岗位职责：负责后端服务开发与"
        "性能优化，参与架构设计，保障系统稳定可靠。任职要求：精通 Python 与分布式系统，"
        "3 年以上经验，本科及以上学历，良好的团队协作能力。"
    )
    html = "<html><body><p>欢迎转发</p><img src='https://mmbiz.qpic.cn/a'></body></html>"
    result = run_wechat_slice(
        _WECHAT_URL,
        runner=_ocr_runner(text=ocr_text),
        out_dir=str(tmp_path),
        context=ToolContext(user_id="", run_id="", metadata={}),
        extract_fn=_fake_extract({}),
        fetch_html_fn=_fake_fetch_html(html),
        download_fn=_fake_download(),
    )
    assert result.application_channel_json == {"type": "url", "value": "*.zhiye.com/*"}
    assert result.needs_deep_crawl is True
    assert any(
        "OCR可能损坏了URL，已按模式重建" in w
        for w in result.candidates[0].normalization_warnings
    )


def test_run_wechat_slice_result_exposes_visible_text_and_content_hash(tmp_path) -> None:
    html = "<html><body><p>欢迎转发</p><img src='https://mmbiz.qpic.cn/a'></body></html>"
    seen: dict = {}
    result = run_wechat_slice(
        _WECHAT_URL,
        runner=_ocr_runner(),
        out_dir=str(tmp_path),
        context=ToolContext(user_id="", run_id="", metadata={}),
        extract_fn=_fake_extract(seen),
        fetch_html_fn=_fake_fetch_html(html),
        download_fn=_fake_download(),
    )
    # the per-URL evidence projection: the produced extraction text
    # (bounded to ≤1200 chars) + its sha256, matching the observed
    # evidence the slice registered for the extraction
    assert result.visible_text == seen["input"]
    assert len(result.visible_text) <= 1200
    assert result.content_hash == hashlib.sha256(
        result.visible_text.encode("utf-8")
    ).hexdigest()


def test_run_wechat_slice_result_without_text_has_empty_projection(tmp_path) -> None:
    # blocked / needs_manual_review / skipped outcomes produce no text:
    # the projection fields stay empty ("" / None)
    html = "<html><body><p>hi</p></body></html>"  # < 200 chars, no images
    result = run_wechat_slice(
        _WECHAT_URL,
        runner=_ocr_runner(),
        out_dir=str(tmp_path),
        context=ToolContext(user_id="", run_id="", metadata={}),
        extract_fn=_fake_extract({}),
        fetch_html_fn=_fake_fetch_html(html),
        download_fn=_fake_download(),
    )
    assert result.status == "needs_manual_review"
    assert result.visible_text == ""
    assert result.content_hash is None


def test_ocr_image_non_dict_output_folds(tmp_path) -> None:
    def runner(script: str, *, cli_args: str = "", stdin: str = "") -> str:
        return "[1, 2]"

    html = "<html><body><p>欢迎转发</p><img src='https://mmbiz.qpic.cn/a'></body></html>"
    result = run_wechat_slice(
        _WECHAT_URL,
        runner=runner,
        out_dir=str(tmp_path),
        context=ToolContext(user_id="", run_id="", metadata={}),
        extract_fn=_fake_extract({}),
        fetch_html_fn=_fake_fetch_html(html),
        download_fn=_fake_download(),
    )
    assert result.status == "needs_manual_review"
    assert result.reason == "image-heavy article — OCR produced no usable text"


def test_ocr_image_empty_full_text_folds(tmp_path) -> None:
    def runner(script: str, *, cli_args: str = "", stdin: str = "") -> str:
        return json.dumps({"status": "ok", "full_text": "", "confidence": 0.9})

    html = "<html><body><p>欢迎转发</p><img src='https://mmbiz.qpic.cn/a'></body></html>"
    result = run_wechat_slice(
        _WECHAT_URL,
        runner=runner,
        out_dir=str(tmp_path),
        context=ToolContext(user_id="", run_id="", metadata={}),
        extract_fn=_fake_extract({}),
        fetch_html_fn=_fake_fetch_html(html),
        download_fn=_fake_download(),
    )
    assert result.status == "needs_manual_review"
    assert result.reason == "image-heavy article — OCR produced no usable text"


def test_ocr_image_non_numeric_confidence_folds(tmp_path) -> None:
    def runner(script: str, *, cli_args: str = "", stdin: str = "") -> str:
        return json.dumps(
            {
                "status": "ok",
                "full_text": (
                    "招聘：后端工程师。岗位职责：负责后端服务开发与性能优化，参与架构设计，"
                    "保障系统稳定可靠。负责接口设计与性能调优，参与系统重构。任职要求："
                    "精通 Python 与分布式系统，3 年以上经验，本科及以上学历，良好的团队协作"
                    "能力。简历投递至 hr@company.com，欢迎咨询。"
                ),
                "confidence": "high",
                "engine": "paddleocr",
            }
        )

    html = "<html><body><p>欢迎转发</p><img src='https://mmbiz.qpic.cn/a'></body></html>"
    result = run_wechat_slice(
        _WECHAT_URL,
        runner=runner,
        out_dir=str(tmp_path),
        context=ToolContext(user_id="", run_id="", metadata={}),
        extract_fn=_fake_extract({}),
        fetch_html_fn=_fake_fetch_html(html),
        download_fn=_fake_download(),
    )
    assert result.status == "succeeded"
    ocr_ref = next(
        ref for ref in result.candidates[0].evidence_refs
        if ref["evidence_type"] == "ocr_text"
    )
    assert json.loads(ocr_ref["metadata"])["ocr_confidence"] == 0.0


def test_ocr_evidence_meta_dimensions_variants(tmp_path) -> None:
    # is_long_image is False without dimensions, below the threshold, or
    # when the height cannot be parsed
    for dimensions, expected_long, expected_meta in (
        (None, False, None),
        ({"width": 1080, "height": 1600}, False, {"width": 1080, "height": 1600}),
        ({"width": 1080, "height": "tall"}, False, {"width": 1080, "height": "tall"}),
    ):
        def runner(script: str, *, cli_args: str = "", stdin: str = "") -> str:
            payload = {
                "status": "ok",
                "full_text": (
                    "招聘：后端工程师。岗位职责：负责后端服务开发与性能优化。"
                    "任职要求：精通 Python。投递至 hr@x.com。"
                ),
                "confidence": 0.9,
                "engine": "paddleocr",
            }
            if dimensions is not None:
                payload["dimensions"] = dimensions
            return json.dumps(payload)

        html = "<html><body><p>欢迎转发</p><img src='https://mmbiz.qpic.cn/a'></body></html>"
        result = run_wechat_slice(
            _WECHAT_URL,
            runner=runner,
            out_dir=str(tmp_path),
            context=ToolContext(user_id="", run_id="", metadata={}),
            extract_fn=_fake_extract({}),
            fetch_html_fn=_fake_fetch_html(html),
            download_fn=_fake_download(),
        )
        ocr_ref = next(
            ref for ref in result.candidates[0].evidence_refs
            if ref["evidence_type"] == "ocr_text"
        )
        ocr_meta = json.loads(ocr_ref["metadata"])
        assert ocr_meta["is_long_image"] is expected_long
        assert ocr_meta["image_dimensions"] == expected_meta


def test_parse_article_skips_script_style_and_srcless_images() -> None:
    html = (
        "<html><head><script>var cfg={a:1};</script><style>.job{color:red}</style>"
        "</head><body><p>岗位：前端工程师</p>"
        "<img><img src=''><img src='https://mmbiz.qpic.cn/a'></body></html>"
    )
    article_text, srcs = ws._parse_article(html)
    # script/style subtrees are never article content; an <img> without a
    # src (or with an empty src) contributes nothing
    assert "var cfg" not in article_text
    assert ".job" not in article_text
    assert "岗位：前端工程师" in article_text
    assert srcs == ["https://mmbiz.qpic.cn/a"]


def test_parse_article_skips_whitespace_only_text_nodes() -> None:
    # whitespace-only data nodes (indentation/newlines between tags) strip
    # to empty and never enter the article text
    html = "<html><body><p>职位：后端</p>\n   \n<p>职责：接口</p></body></html>"
    article_text, srcs = ws._parse_article(html)
    assert article_text.splitlines() == ["职位：后端", "职责：接口"]


# -------------------------------------- default fetch / guard internals


class _FakeResponse:
    def __init__(self, *, redirect: bool = False, location: str | None = None, text: str = "") -> None:
        self.is_redirect = redirect
        self.text = text
        self.headers = {"Location": location} if location else {}


def test_l1_guard_rejects_non_public_urls() -> None:
    assert ws._l1_guard("file:///C:/x") == "unsafe_public_url"
    assert ws._l1_guard("") == "unsafe_public_url"
    assert ws._l1_guard("http://user:pass@example.com/x") == "unsafe_public_url"
    assert ws._l1_guard("http://127.0.0.1/x") == "unsafe_public_url"
    assert ws._l1_guard("http://93.184.216.34/x") is None  # global IP literal
    assert ws._l1_guard(_WECHAT_URL) is None  # hostname passes the L1 check


def test_l1_guard_missing_hostname_and_password_only() -> None:
    # arc coverage: scheme-valid but hostname-less target; username-less
    # userinfo (both rejected structurally, before any fetch)
    assert ws._l1_guard("http:///x") == "unsafe_public_url"
    assert ws._l1_guard("http://:secret@example.com/x") == "unsafe_public_url"


def test_assert_public_url_rejects_non_global_targets(monkeypatch) -> None:
    with pytest.raises(PublicJobFetchError) as exc:
        ws._assert_public_url("ftp://example.com/x")
    assert exc.value.code == "unsafe_public_url"
    with pytest.raises(PublicJobFetchError) as exc:
        ws._assert_public_url("http://user:pass@example.com/x")
    assert exc.value.code == "unsafe_public_url"
    with pytest.raises(PublicJobFetchError) as exc:
        ws._assert_public_url("http://127.0.0.1/private")
    assert exc.value.code == "unsafe_public_url"
    ws._assert_public_url("http://93.184.216.34/x")  # global IP literal: no DNS
    # hostname resolves via a stubbed getaddrinfo -> global address (no live DNS)
    monkeypatch.setattr(
        ws.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))
        ],
    )
    ws._assert_public_url(_WECHAT_URL)


def test_assert_public_url_missing_hostname_and_password_only() -> None:
    # arc coverage: scheme-valid but hostname-less target; username-less
    # userinfo (both rejected before any resolution)
    with pytest.raises(PublicJobFetchError) as exc:
        ws._assert_public_url("http:///x")
    assert exc.value.code == "unsafe_public_url"
    with pytest.raises(PublicJobFetchError) as exc:
        ws._assert_public_url("http://:secret@example.com/x")
    assert exc.value.code == "unsafe_public_url"


def test_assert_public_url_unresolvable(monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise OSError("getaddrinfo failed")

    monkeypatch.setattr(ws.socket, "getaddrinfo", boom)
    with pytest.raises(PublicJobFetchError) as exc:
        ws._assert_public_url(_WECHAT_URL)
    assert exc.value.code == "public_host_unresolvable"


def test_default_fetch_html_follows_public_redirects(monkeypatch) -> None:
    responses = [
        _FakeResponse(redirect=True, location="https://93.184.216.34/final"),
        _FakeResponse(text="final html"),
    ]
    calls: list[str] = []

    def fake_get(url: str, **kwargs) -> _FakeResponse:
        calls.append(url)
        return responses.pop(0)

    monkeypatch.setattr(ws.requests, "get", fake_get)
    assert ws._default_fetch_html("http://93.184.216.34/page") == "final html"
    assert calls == ["http://93.184.216.34/page", "https://93.184.216.34/final"]


def test_default_fetch_html_blocks_private_redirect_target(monkeypatch) -> None:
    responses = [_FakeResponse(redirect=True, location="http://127.0.0.1/private")]
    calls: list[str] = []

    def fake_get(url: str, **kwargs) -> _FakeResponse:
        calls.append(url)
        return responses.pop(0)

    monkeypatch.setattr(ws.requests, "get", fake_get)
    with pytest.raises(PublicJobFetchError) as exc:
        ws._default_fetch_html("http://93.184.216.34/page")
    assert exc.value.code == "unsafe_public_url"
    assert calls == ["http://93.184.216.34/page"]  # the target is never fetched


def test_default_fetch_html_redirect_without_location(monkeypatch) -> None:
    monkeypatch.setattr(
        ws.requests, "get", lambda url, **kwargs: _FakeResponse(redirect=True, text="")
    )
    assert ws._default_fetch_html("http://93.184.216.34/page") == ""


def test_default_fetch_html_too_many_redirects(monkeypatch) -> None:
    monkeypatch.setattr(
        ws.requests,
        "get",
        lambda url, **kwargs: _FakeResponse(redirect=True, location="http://93.184.216.34/r1"),
    )
    with pytest.raises(PublicJobFetchError) as exc:
        ws._default_fetch_html("http://93.184.216.34/page")
    assert exc.value.code == "unsafe_public_url"


def test_default_fetch_html_network_error_propagates(monkeypatch) -> None:
    def fake_get(url: str, **kwargs) -> _FakeResponse:
        raise OSError("connection refused")

    monkeypatch.setattr(ws.requests, "get", fake_get)
    with pytest.raises(OSError):
        ws._default_fetch_html("http://93.184.216.34/page")


def test_default_download_image_guards_private_target(monkeypatch) -> None:
    calls: list[str] = []

    def fake_get(url: str, **kwargs) -> _FakeResponse:
        calls.append(url)
        raise AssertionError("must not fetch a private target")

    monkeypatch.setattr(ws.requests, "get", fake_get)
    with pytest.raises(PublicJobFetchError) as exc:
        ws._default_download_image("http://127.0.0.1/steal")
    assert exc.value.code == "unsafe_public_url"
    assert calls == []


class _FakeImageResponse:
    def __init__(
        self,
        *,
        redirect: bool = False,
        location: str | None = None,
        content: bytes = b"imgdata",
    ) -> None:
        self.is_redirect = redirect
        self.headers = {"Location": location} if location else {}
        self.content = content

    def raise_for_status(self) -> None:
        return None


def test_default_download_image_success(monkeypatch) -> None:
    monkeypatch.setattr(ws.requests, "get", lambda url, **kwargs: _FakeImageResponse())
    assert ws._default_download_image("http://93.184.216.34/img.png") == b"imgdata"


def test_default_download_image_rejects_private_redirect_target(monkeypatch) -> None:
    # a public-origin image that redirects to a private target is rejected
    # before the target is fetched (mirror of the article-fetch posture)
    responses = [
        _FakeImageResponse(redirect=True, location="http://127.0.0.1/steal"),
        _FakeImageResponse(),
    ]
    calls: list[str] = []

    def fake_get(url: str, **kwargs) -> _FakeImageResponse:
        calls.append(url)
        return responses.pop(0)

    monkeypatch.setattr(ws.requests, "get", fake_get)
    with pytest.raises(PublicJobFetchError) as exc:
        ws._default_download_image("http://93.184.216.34/img.png")
    assert exc.value.code == "unsafe_public_url"
    assert calls == ["http://93.184.216.34/img.png"]  # the target is never fetched


def test_default_download_image_follows_public_redirect_chain(monkeypatch) -> None:
    responses = [
        _FakeImageResponse(redirect=True, location="https://93.184.216.34/final.png"),
        _FakeImageResponse(content=b"final-img"),
    ]
    calls: list[str] = []

    def fake_get(url: str, **kwargs) -> _FakeImageResponse:
        calls.append(url)
        return responses.pop(0)

    monkeypatch.setattr(ws.requests, "get", fake_get)
    assert ws._default_download_image("http://93.184.216.34/img.png") == b"final-img"
    assert calls == ["http://93.184.216.34/img.png", "https://93.184.216.34/final.png"]


def test_default_download_image_redirect_without_location(monkeypatch) -> None:
    monkeypatch.setattr(
        ws.requests, "get", lambda url, **kwargs: _FakeImageResponse(redirect=True)
    )
    assert ws._default_download_image("http://93.184.216.34/img.png") == b"imgdata"


def test_default_download_image_too_many_redirects(monkeypatch) -> None:
    monkeypatch.setattr(
        ws.requests,
        "get",
        lambda url, **kwargs: _FakeImageResponse(
            redirect=True, location="http://93.184.216.34/r1"
        ),
    )
    with pytest.raises(PublicJobFetchError) as exc:
        ws._default_download_image("http://93.184.216.34/img.png")
    assert exc.value.code == "unsafe_public_url"


# ------------------------------------------------------ graph wiring


def _graph_fake_runner(script: str, cli_args: str = "", stdin: str = "") -> str:
    """Faithful fake for the graph's allowlisted scripts (no real subprocess)."""
    if script == "write_candidates":
        out_path = Path(cli_args.split("--out ")[1].split()[0])
        batch = json.loads(stdin) if stdin else []
        out_path.write_text(json.dumps(batch, ensure_ascii=False), encoding="utf-8")
        return json.dumps(
            {"status": "ok", "batch_received": len(batch), "batch_kept": len(batch)}
        )
    if script == "deduplicate":
        parts = cli_args.split()
        out_path = Path(parts[parts.index("--out") + 1])
        page_files = [
            p for p in parts if p.endswith(".json") and "merged_final" not in p
        ]
        merged: list[dict] = []
        for page_file in page_files:
            merged.extend(json.loads(Path(page_file).read_text(encoding="utf-8")))
        out_path.write_text(json.dumps(merged, ensure_ascii=False), encoding="utf-8")
        return json.dumps(
            {
                "status": "ok",
                "stats": {
                    "input_count": len(merged),
                    "output_count": len(merged),
                    "duplicates_removed": 0,
                },
                "load_errors": [],
                "verify_warnings_count": 0,
                "output_file": str(out_path),
            }
        )
    if script == "validate":
        return json.dumps({"ok": True})
    if script == "coverage_gate":
        return json.dumps(
            {
                "coverage_verified": True,
                "page_count": 1,
                "candidate_count": 1,
                "reasons": [],
            }
        )
    return "{}"


def test_graph_routes_wechat_pending_through_slice(tmp_path) -> None:
    def fetch(urls: list[str]) -> list[dict]:
        return [{"url": u, "source_url": u, "status": "wechat_pending"} for u in urls]

    def wechat_fn(url: str) -> WechatResult:
        return WechatResult(
            url=url,
            status="succeeded",
            channel="A",
            candidates=[_candidate("微信职位")],
            application_channel_json=None,
            needs_deep_crawl=False,
            reason=None,
        )

    graph = build_job_discovery_graph(
        fetch_fn=fetch,
        script_runner=_graph_fake_runner,
        wechat_fn=wechat_fn,
        candidates_dir=str(tmp_path),
        state_dir=str(tmp_path),
    ).compile()
    final = graph.invoke({"urls": [_WECHAT_URL]})
    per_url = final["per_url_results"][0]
    assert per_url["url"] == _WECHAT_URL
    assert per_url["status"] == "succeeded"
    assert per_url["channel"] == "A"
    assert per_url["error_code"] is None
    assert per_url["blocked_reason"] is None
    # candidates were persisted with a wechat page id (matches the dedup
    # glob) and merged into the per-run candidate set
    assert (tmp_path / "page_wechat_00.json").exists()
    assert {c["title"] for c in final["candidates"]} == {"微信职位"}


def test_graph_surfaces_needs_manual_review_and_blocked(tmp_path) -> None:
    outcomes = {
        "https://a.example.com/1": WechatResult(
            "https://a.example.com/1", "needs_manual_review", "C", [], None, False, "仅含联系方式"
        ),
        "https://b.example.com/2": WechatResult(
            "https://b.example.com/2", "blocked", None, [], None, False, "unsafe_public_url"
        ),
    }

    def fetch(urls: list[str]) -> list[dict]:
        return [{"url": u, "source_url": u, "status": "wechat_pending"} for u in urls]

    graph = build_job_discovery_graph(
        fetch_fn=fetch,
        script_runner=_graph_fake_runner,
        wechat_fn=lambda url: outcomes[url],
        candidates_dir=str(tmp_path),
        state_dir=str(tmp_path),
    ).compile()
    final = graph.invoke({"urls": list(outcomes)})
    by_url = {r["url"]: r for r in final["per_url_results"]}
    assert by_url["https://a.example.com/1"]["status"] == "needs_manual_review"
    assert by_url["https://a.example.com/1"]["error_code"] == "needs_manual_review"
    assert by_url["https://a.example.com/1"]["reason"] == "仅含联系方式"
    assert by_url["https://b.example.com/2"]["status"] == "blocked"
    assert by_url["https://b.example.com/2"]["error_code"] is None
    assert by_url["https://b.example.com/2"]["blocked_reason"] == "unsafe_public_url"
    assert final["candidates"] == []


def test_graph_wechat_slice_crash_is_recoverable(tmp_path) -> None:
    def fetch(urls: list[str]) -> list[dict]:
        return [{"url": u, "source_url": u, "status": "wechat_pending"} for u in urls]

    def wechat_fn(url: str) -> WechatResult:
        raise RuntimeError("boom")

    graph = build_job_discovery_graph(
        fetch_fn=fetch,
        script_runner=_graph_fake_runner,
        wechat_fn=wechat_fn,
        candidates_dir=str(tmp_path),
        state_dir=str(tmp_path),
    ).compile()
    final = graph.invoke({"urls": [_WECHAT_URL]})
    per_url = final["per_url_results"][0]
    assert per_url["status"] == "failed"
    assert per_url["reason"] == "wechat_slice_error"
    assert final["candidates"] == []


def test_graph_wechat_candidates_merge_with_page_candidates(tmp_path) -> None:
    page_text = "岗位：后端工程师\n岗位职责：负责后端服务开发\n任职要求：精通 Python"
    (tmp_path / "page_0.txt").write_text(page_text, encoding="utf-8")

    def fetch(urls: list[str]) -> list[dict]:
        return [
            {
                "url": "https://example.com/jobs",
                "source_url": "https://example.com/jobs",
                "status": "succeeded",
                "content_hash": "page-hash",
                "mode": "parallel-fetch",
                "page_files": [
                    {
                        "path": str(tmp_path / "page_0.txt"),
                        "content_hash": "page-hash",
                        "text_length": len(page_text),
                    }
                ],
                "visible_text": page_text,
            },
            {"url": _WECHAT_URL, "source_url": _WECHAT_URL, "status": "wechat_pending"},
        ]

    def wechat_fn(url: str) -> WechatResult:
        return WechatResult(
            url=url,
            status="succeeded",
            channel="B",
            candidates=[_candidate("微信职位")],
            application_channel_json=None,
            needs_deep_crawl=False,
            reason=None,
        )

    graph = build_job_discovery_graph(
        fetch_fn=fetch,
        script_runner=_graph_fake_runner,
        wechat_fn=wechat_fn,
        candidates_dir=str(tmp_path),
        state_dir=str(tmp_path),
    ).compile()
    final = graph.invoke({"urls": ["https://example.com/jobs", _WECHAT_URL]})
    titles = {c["title"] for c in final["candidates"]}
    assert "微信职位" in titles
    # extracted from the regular page (the real regex extractor's title for
    # "岗位：后端工程师" keeps the prefix; the dedup fake merges the actual
    # page_00.json content, so this is the real extractor output)
    assert "岗位：后端工程师" in titles
    assert (tmp_path / "page_00.json").exists()
    assert (tmp_path / "page_wechat_00.json").exists()


def test_graph_wechat_partial_success_candidates_flow(tmp_path) -> None:
    def fetch(urls: list[str]) -> list[dict]:
        return [{"url": u, "source_url": u, "status": "wechat_pending"} for u in urls]

    def wechat_fn(url: str) -> WechatResult:
        return WechatResult(
            url=url,
            status="partial_success",
            channel="B",
            candidates=[_candidate("弱OCR职位")],
            application_channel_json=None,
            needs_deep_crawl=False,
            reason=None,
        )

    graph = build_job_discovery_graph(
        fetch_fn=fetch,
        script_runner=_graph_fake_runner,
        wechat_fn=wechat_fn,
        candidates_dir=str(tmp_path),
        state_dir=str(tmp_path),
    ).compile()
    final = graph.invoke({"urls": [_WECHAT_URL]})
    assert final["per_url_results"][0]["status"] == "partial_success"
    assert {c["title"] for c in final["candidates"]} == {"弱OCR职位"}


def test_graph_default_wechat_seam_invokes_slice(monkeypatch, tmp_path) -> None:
    import backend.app.services.deepagents_runtime.tools.skill_graphs.job_discovery_graph as jdg

    captured: dict = {}

    def fake_slice(url, *, runner=None, out_dir=None, context=None, extract_fn=None,
                   llm_extractor=None, **kwargs) -> WechatResult:
        captured["url"] = url
        captured["out_dir"] = out_dir
        captured["context"] = context
        return WechatResult(
            url=url,
            status="needs_manual_review",
            channel="C",
            candidates=[],
            application_channel_json=None,
            needs_deep_crawl=False,
            reason="仅含联系方式",
        )

    monkeypatch.setattr(jdg, "run_wechat_slice", fake_slice)

    def fetch(urls: list[str]) -> list[dict]:
        return [{"url": u, "source_url": u, "status": "wechat_pending"} for u in urls]

    graph = build_job_discovery_graph(
        fetch_fn=fetch,
        script_runner=_graph_fake_runner,
        candidates_dir=str(tmp_path),
        state_dir=str(tmp_path),
    ).compile()
    final = graph.invoke({"urls": [_WECHAT_URL]})
    assert captured["url"] == _WECHAT_URL
    assert isinstance(captured["context"], ToolContext)
    assert final["per_url_results"][0]["error_code"] == "needs_manual_review"


def test_graph_default_wechat_seam_honors_llm_flag(monkeypatch, tmp_path) -> None:
    import backend.app.services.deepagents_runtime.tools.skill_graphs.job_discovery_graph as jdg

    captured: dict = {}
    marker = object()
    monkeypatch.setattr(jdg, "build_llm_extractor", lambda settings: marker)

    def fake_slice(url, *, runner=None, out_dir=None, context=None, extract_fn=None,
                   llm_extractor=None, **kwargs) -> WechatResult:
        captured["llm_extractor"] = llm_extractor
        return WechatResult(
            url=url,
            status="succeeded",
            channel="A",
            candidates=[],
            application_channel_json=None,
            needs_deep_crawl=False,
            reason=None,
        )

    monkeypatch.setattr(jdg, "run_wechat_slice", fake_slice)

    def fetch(urls: list[str]) -> list[dict]:
        return [{"url": u, "source_url": u, "status": "wechat_pending"} for u in urls]

    graph = build_job_discovery_graph(
        fetch_fn=fetch,
        script_runner=_graph_fake_runner,
        settings=settings_override(deepagents_llm_extraction_enabled=True),
        candidates_dir=str(tmp_path),
        state_dir=str(tmp_path),
    ).compile()
    graph.invoke({"urls": [_WECHAT_URL]})
    assert captured["llm_extractor"] is marker


def test_graph_wechat_non_success_candidates_not_persisted(tmp_path) -> None:
    # a slice result that carries candidates under a non-success status
    # (defensive: only succeeded/partial_success candidates are persisted
    # and merged into the per-run set)
    def fetch(urls: list[str]) -> list[dict]:
        return [{"url": u, "source_url": u, "status": "wechat_pending"} for u in urls]

    def wechat_fn(url: str) -> WechatResult:
        return WechatResult(
            url=url,
            status="skipped",
            channel="D",
            candidates=[_candidate("不应持久化")],
            application_channel_json=None,
            needs_deep_crawl=False,
            reason="推广",
        )

    graph = build_job_discovery_graph(
        fetch_fn=fetch,
        script_runner=_graph_fake_runner,
        wechat_fn=wechat_fn,
        candidates_dir=str(tmp_path),
        state_dir=str(tmp_path),
    ).compile()
    final = graph.invoke({"urls": [_WECHAT_URL]})
    assert not (tmp_path / "page_wechat_00.json").exists()
    assert final["candidates"] == []
    assert final["per_url_results"][0]["status"] == "skipped"
    assert final["per_url_results"][0]["reason"] == "推广"


def test_graph_wechat_entry_carries_evidence_projection(tmp_path) -> None:
    # the wechat per-URL entry surfaces content_hash + visible_text exactly
    # like a regular succeeded page, so the harness's evidence projection
    # treats both flows uniformly
    def fetch(urls: list[str]) -> list[dict]:
        return [{"url": u, "source_url": u, "status": "wechat_pending"} for u in urls]

    def wechat_fn(url: str) -> WechatResult:
        return WechatResult(
            url=url,
            status="succeeded",
            channel="B",
            candidates=[_candidate("微信职位")],
            application_channel_json=None,
            needs_deep_crawl=False,
            reason=None,
            visible_text="=== 图片1 OCR内容 ===\n招聘：后端工程师",
            content_hash="sha256_wechat_evidence",
        )

    graph = build_job_discovery_graph(
        fetch_fn=fetch,
        script_runner=_graph_fake_runner,
        wechat_fn=wechat_fn,
        candidates_dir=str(tmp_path),
        state_dir=str(tmp_path),
    ).compile()
    entry = graph.invoke({"urls": [_WECHAT_URL]})["per_url_results"][0]
    assert entry["content_hash"] == "sha256_wechat_evidence"
    assert entry["visible_text"] == "=== 图片1 OCR内容 ===\n招聘：后端工程师"


def test_graph_wechat_entry_without_text_has_empty_projection(tmp_path) -> None:
    # a needs_manual_review outcome carries no produced text: the entry
    # still carries both keys, empty (None / ""); the 7-positional fold
    # constructor also stays valid with the new defaulted fields
    def fetch(urls: list[str]) -> list[dict]:
        return [{"url": u, "source_url": u, "status": "wechat_pending"} for u in urls]

    def wechat_fn(url: str) -> WechatResult:
        return WechatResult(url, "needs_manual_review", "C", [], None, False, "仅含联系方式")

    graph = build_job_discovery_graph(
        fetch_fn=fetch,
        script_runner=_graph_fake_runner,
        wechat_fn=wechat_fn,
        candidates_dir=str(tmp_path),
        state_dir=str(tmp_path),
    ).compile()
    entry = graph.invoke({"urls": [_WECHAT_URL]})["per_url_results"][0]
    assert entry["content_hash"] is None
    assert entry["visible_text"] == ""


def test_graph_incremental_appends_needs_manual_review_errors(tmp_path) -> None:
    # Task 10 incremental mode (prior_metadata present): a needs_manual_review
    # slice result is handed off to the stable store — errors.jsonl at
    # <state_dir>/output/ accumulates the entry across runs
    def fetch(urls: list[str]) -> list[dict]:
        return [{"url": u, "source_url": u, "status": "wechat_pending"} for u in urls]

    def wechat_fn(url: str) -> WechatResult:
        return WechatResult(
            url, "needs_manual_review", "C", [], None, False, "仅含联系方式"
        )

    graph = build_job_discovery_graph(
        fetch_fn=fetch,
        script_runner=_graph_fake_runner,
        wechat_fn=wechat_fn,
        candidates_dir=str(tmp_path),
        state_dir=str(tmp_path),
    ).compile()
    final = graph.invoke(
        {
            "urls": [_WECHAT_URL],
            "prior_metadata": {
                "file_id": "f1",
                "sheet_id": "s1",
                "update_time": "2026-08-07",
            },
        }
    )
    assert final["per_url_results"][0]["status"] == "needs_manual_review"
    lines = (
        (tmp_path / "output" / "errors.jsonl")
        .read_text(encoding="utf-8")
        .strip()
        .splitlines()
    )
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["url"] == _WECHAT_URL
    assert entry["cause"] == "needs_manual_review"
    assert entry["status"] == "needs_manual_review"
    assert entry["reason"] == "仅含联系方式"
    assert entry["channel"] == "C"


# ------------------------------------------------------------ ReadGZH proxy


class _FakeReadgzhResponse:
    """Minimal requests.Response stand-in with raise_for_status.

    ``content`` mirrors the real response body bytes (UTF-8); pass it
    explicitly to simulate a proxy that decoded its text as latin-1.
    """

    def __init__(
        self,
        text: str = "",
        status_code: int = 200,
        content: bytes | None = None,
    ) -> None:
        self.text = text
        self.content = content if content is not None else text.encode("utf-8")
        self._status_code = status_code

    def raise_for_status(self) -> None:
        if self._status_code >= 400:
            raise requests.RequestException(
                f"HTTP {self._status_code} from ReadGZH proxy"
            )


#: A ReadGZH-style clean article HTML (text > 200 chars, no images).
_READGZH_ARTICLE_HTML = (
    "<html><head><title>某公司招聘</title></head><body>"
    "<h1>某公司 2026 校园招聘</h1>"
    "<p>岗位：AI 应用开发工程师。职责：负责大模型应用开发与落地，参与"
    "RAG/Agent 平台建设，保障系统稳定可靠，参与架构设计。要求：精通"
    "Python，熟悉大模型与分布式系统，有 RAG/Agent 项目经验者优先，"
    "3 年以上开发经验，本科及以上学历，计算机相关专业，良好的团队"
    "协作能力与沟通能力，能够独立解决问题。薪资面议，五险一金齐全。"
    "简历投递：hr@company.com。</p></body></html>"
)


def test_readgzh_fetch_without_key_returns_none(monkeypatch) -> None:
    monkeypatch.delenv("READGZH_API_KEY", raising=False)
    calls: list[str] = []

    def fake_get(url: str, **kwargs) -> _FakeReadgzhResponse:
        calls.append(url)
        raise AssertionError("no request without a key")

    monkeypatch.setattr(ws.requests, "get", fake_get)
    assert ws._readgzh_fetch_html(_WECHAT_URL) is None
    assert calls == []


def test_readgzh_fetch_http_error_returns_none(monkeypatch) -> None:
    monkeypatch.setenv("READGZH_API_KEY", "sk_test_key")
    monkeypatch.setattr(
        ws.requests,
        "get",
        lambda url, **kwargs: _FakeReadgzhResponse(status_code=429),
    )
    assert ws._readgzh_fetch_html(_WECHAT_URL) is None


def test_readgzh_fetch_too_short_returns_none(monkeypatch) -> None:
    monkeypatch.setenv("READGZH_API_KEY", "sk_test_key")
    monkeypatch.setattr(
        ws.requests,
        "get",
        lambda url, **kwargs: _FakeReadgzhResponse(text="<html></html>"),
    )
    assert ws._readgzh_fetch_html(_WECHAT_URL) is None


def test_readgzh_fetch_json_error_returns_none(monkeypatch) -> None:
    monkeypatch.setenv("READGZH_API_KEY", "sk_test_key")
    monkeypatch.setattr(
        ws.requests,
        "get",
        lambda url, **kwargs: _FakeReadgzhResponse(
            text=json.dumps(
                {
                    "success": False,
                    "code": 1001,
                    "message": "quota exhausted" + "x" * 250,
                }
            )
        ),
    )
    assert ws._readgzh_fetch_html(_WECHAT_URL) is None


def test_readgzh_fetch_malformed_json_returns_none(monkeypatch) -> None:
    monkeypatch.setenv("READGZH_API_KEY", "sk_test_key")
    monkeypatch.setattr(
        ws.requests,
        "get",
        lambda url, **kwargs: _FakeReadgzhResponse(text="{" + "x" * 250),
    )
    assert ws._readgzh_fetch_html(_WECHAT_URL) is None


def test_readgzh_fetch_json_success_returns_raw(monkeypatch) -> None:
    monkeypatch.setenv("READGZH_API_KEY", "sk_test_key")
    body = json.dumps({"success": True, "data": {"content": "x" * 250}})
    monkeypatch.setattr(
        ws.requests,
        "get",
        lambda url, **kwargs: _FakeReadgzhResponse(text=body),
    )
    assert ws._readgzh_fetch_html(_WECHAT_URL) == body


def test_readgzh_fetch_json_non_dict_returns_raw(monkeypatch) -> None:
    monkeypatch.setenv("READGZH_API_KEY", "sk_test_key")
    monkeypatch.setattr(
        ws.requests,
        "get",
        lambda url, **kwargs: _FakeReadgzhResponse(text=json.dumps(["a"] * 200)),
    )
    assert ws._readgzh_fetch_html(_WECHAT_URL) == json.dumps(["a"] * 200)


def test_readgzh_fetch_verification_wall_returns_none(monkeypatch) -> None:
    monkeypatch.setenv("READGZH_API_KEY", "sk_test_key")
    monkeypatch.setattr(
        ws.requests,
        "get",
        lambda url, **kwargs: _FakeReadgzhResponse(
            text="<html><body>环境异常 完成验证后即可继续访问" + "x" * 250 + "</body></html>"
        ),
    )
    assert ws._readgzh_fetch_html(_WECHAT_URL) is None


def test_readgzh_fetch_paywall_dashboard_returns_none(monkeypatch) -> None:
    monkeypatch.setenv("READGZH_API_KEY", "sk_test_key")
    monkeypatch.setattr(
        ws.requests,
        "get",
        lambda url, **kwargs: _FakeReadgzhResponse(
            text=(
                "<html><body>标题 原文链接 https://mp.weixin.qq.com/s/x "
                "Powered by ReadGZH 免费注册获取每天 30 积分 "
                "readgzh.site/dashboard" + "x" * 250 + "</body></html>"
            )
        ),
    )
    assert ws._readgzh_fetch_html(_WECHAT_URL) is None


def test_readgzh_fetch_paywall_upgrade_pitch_returns_none(monkeypatch) -> None:
    monkeypatch.setenv("READGZH_API_KEY", "sk_test_key")
    monkeypatch.setattr(
        ws.requests,
        "get",
        lambda url, **kwargs: _FakeReadgzhResponse(
            text=(
                "<html><body>标题 原文链接 https://mp.weixin.qq.com/s/x "
                "Powered by ReadGZH 升级套餐 Lite 9/月 Pro 39/月" + "x" * 250
                + "</body></html>"
            )
        ),
    )
    assert ws._readgzh_fetch_html(_WECHAT_URL) is None


def test_readgzh_fetch_paywall_footer_with_real_body_returns_raw(monkeypatch) -> None:
    """A footer-marked page with a real article body is not a quota wall -
    plan-upgraded accounts still attach the proxy footer to genuine pages."""
    monkeypatch.setenv("READGZH_API_KEY", "sk_test_key")
    body = (
        "<html><body><p>"
        + "某公司招聘 AI 应用开发工程师，负责大模型应用开发与落地，参与"
        "RAG/Agent 平台建设，保障系统稳定可靠，参与架构设计。" * 10
        + "</p></body></html>"
    )
    with_footer = (
        body + '<footer><a href="https://readgzh.site/dashboard">'
        "readgzh.site/dashboard 升级套餐</a></footer>"
    )
    monkeypatch.setattr(
        ws.requests,
        "get",
        lambda url, **kwargs: _FakeReadgzhResponse(text=with_footer),
    )
    assert ws._readgzh_fetch_html(_WECHAT_URL) == with_footer


def test_readgzh_fetch_decodes_utf8_body_not_latin1_text(monkeypatch) -> None:
    """The proxy serves text/plain without a charset; requests would decode
    the body as latin-1 and garble Chinese - the fetch must decode UTF-8
    from the raw content bytes instead of trusting ``response.text``."""
    monkeypatch.setenv("READGZH_API_KEY", "sk_test_key")
    chinese = "新能源AGV全球顶尖人才招募正式启动！" + "x" * 250
    mojibake = chinese.encode("utf-8").decode("latin-1")
    monkeypatch.setattr(
        ws.requests,
        "get",
        lambda url, **kwargs: _FakeReadgzhResponse(
            text=mojibake,
            content=chinese.encode("utf-8"),
        ),
    )
    result = ws._readgzh_fetch_html(_WECHAT_URL)
    assert result is not None
    assert "新能源" in result
    assert "éæ" not in result  # no latin-1 mojibake


def test_readgzh_fetch_success_returns_html(monkeypatch) -> None:
    monkeypatch.setenv("READGZH_API_KEY", "sk_test_key")
    captured: dict = {}

    def fake_get(url: str, **kwargs) -> _FakeReadgzhResponse:
        captured["url"] = url
        captured["kwargs"] = kwargs
        return _FakeReadgzhResponse(text=_READGZH_ARTICLE_HTML)

    monkeypatch.setattr(ws.requests, "get", fake_get)
    assert ws._readgzh_fetch_html(_WECHAT_URL) == _READGZH_ARTICLE_HTML
    assert captured["url"] == ws._READGZH_API_URL
    assert captured["kwargs"]["params"] == {"url": _WECHAT_URL}
    assert captured["kwargs"]["headers"]["Authorization"] == "Bearer sk_test_key"
    # the API key must never leak into the request URL / params
    assert "sk_test_key" not in json.dumps(captured["kwargs"]["params"])


def test_default_fetch_wechat_prefers_readgzh(monkeypatch) -> None:
    monkeypatch.setenv("READGZH_API_KEY", "sk_test_key")
    calls: list[str] = []

    def fake_get(url: str, **kwargs) -> _FakeReadgzhResponse:
        calls.append(url)
        return _FakeReadgzhResponse(text=_READGZH_ARTICLE_HTML)

    monkeypatch.setattr(ws.requests, "get", fake_get)
    assert ws._default_fetch_html(_WECHAT_URL) == _READGZH_ARTICLE_HTML
    assert calls == [ws._READGZH_API_URL]  # the WeChat URL itself is never GET


def test_default_fetch_wechat_falls_back_when_proxy_none(monkeypatch) -> None:
    monkeypatch.setenv("READGZH_API_KEY", "sk_test_key")
    monkeypatch.setattr(
        ws.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))
        ],
    )
    responses = [
        _FakeReadgzhResponse(text=json.dumps({"success": False, "code": 2})),
        _FakeResponse(text="direct wechat html"),
    ]
    calls: list[str] = []

    def fake_get(url: str, **kwargs) -> _FakeReadgzhResponse:
        calls.append(url)
        return responses.pop(0)

    monkeypatch.setattr(ws.requests, "get", fake_get)
    assert ws._default_fetch_html(_WECHAT_URL) == "direct wechat html"
    assert calls == [ws._READGZH_API_URL, _WECHAT_URL]


def test_default_fetch_wechat_without_key_skips_proxy(monkeypatch) -> None:
    monkeypatch.delenv("READGZH_API_KEY", raising=False)
    monkeypatch.setattr(
        ws.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))
        ],
    )
    calls: list[str] = []

    def fake_get(url: str, **kwargs) -> _FakeReadgzhResponse:
        calls.append(url)
        return _FakeResponse(text="direct wechat html")

    monkeypatch.setattr(ws.requests, "get", fake_get)
    assert ws._default_fetch_html(_WECHAT_URL) == "direct wechat html"
    assert calls == [_WECHAT_URL]  # no proxy request without a key


def test_run_wechat_slice_success_via_readgzh(monkeypatch, tmp_path) -> None:
    """End-to-end: ReadGZH HTML feeds L1 -> channel A -> extraction."""
    monkeypatch.setenv("READGZH_API_KEY", "sk_test_key")
    calls: list[str] = []

    def fake_get(url: str, **kwargs) -> _FakeReadgzhResponse:
        calls.append(url)
        return _FakeReadgzhResponse(text=_READGZH_ARTICLE_HTML)

    monkeypatch.setattr(ws.requests, "get", fake_get)
    seen: dict = {}
    result = run_wechat_slice(
        _WECHAT_URL,
        out_dir=str(tmp_path),
        context=ToolContext(user_id="tester", run_id="readgzh-e2e"),
        extract_fn=_fake_extract(seen),
    )
    assert result.status == "succeeded"
    assert result.channel == "A"
    assert len(result.candidates) == 1
    assert result.candidates[0].title == "后端工程师"
    assert calls == [ws._READGZH_API_URL]
