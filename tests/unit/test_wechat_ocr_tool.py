"""PEV WeChat/OCR tool (fetch-wechat-article) unit tests."""

from __future__ import annotations

from functools import partial
from types import SimpleNamespace

import pytest

import backend.app.services.career_skills.wechat as wechat_module
from backend.app.services.career_skills.job_discovery import ExtractedJobDetails
from backend.app.services.career_skills.wechat import (
    FetchWechatArticleInput,
    FetchWechatArticleOutput,
    enable_wechat_ocr,
    fetch_wechat_article,
)
from backend.app.services.agent_runtime.tool_context import ToolContext


def _fake_slice(
    *,
    status: str = "succeeded",
    visible_text: str = "",
    content_hash: str | None = None,
    reason: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        url="https://mp.weixin.qq.com/s/abc123",
        status=status,
        channel="email" if status == "succeeded" else None,
        candidates=[ExtractedJobDetails(
            title="算法工程师",
            company_name=None,
            locations=[],
            responsibilities="岗位职责：做模型。",
            requirements="",
            recruitment_types=[],
            apply_url=None,
            deadline_text=None,
            confidence=1.0,
            evidence_refs=[],
            normalization_warnings=[],
        )]
        if status == "succeeded"
        else [],
        ocr_text="",
        needs_deep_crawl=False,
        reason=reason,
        visible_text=visible_text,
        content_hash=content_hash,
    )


def test_ocr_disabled_returns_needs_manual_review_without_network(monkeypatch) -> None:
    monkeypatch.setattr(wechat_module, "_WECHAT_OCR_ENABLED", False)

    def _boom(*args, **kwargs):
        raise AssertionError("slice must not run when OCR is disabled")

    monkeypatch.setattr(
        "skill.job_discovery.runtime.wechat_slice.run_wechat_slice",
        _boom,
    )
    result = fetch_wechat_article(
        ToolContext(user_id="u", run_id="r"),
        FetchWechatArticleInput(url="https://mp.weixin.qq.com/s/abc123"),
    )
    assert result.status == "needs_manual_review"
    assert result.reason == "ocr_disabled"
    assert result.candidates == []
    assert result.artifact_id is None
    assert result.visible_text == ""


def test_ocr_success_carries_page_evidence(monkeypatch) -> None:
    monkeypatch.setattr(wechat_module, "_WECHAT_OCR_ENABLED", True)
    import hashlib

    text = "=== 文章正文 ===\n职位：算法工程师\n=== 图片1 OCR内容 ===\n任职要求：硕士"
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    fake = _fake_slice(visible_text=text, content_hash=digest)
    monkeypatch.setattr(
        "skill.job_discovery.runtime.wechat_slice.run_wechat_slice",
        lambda *args, **kwargs: fake,
    )

    result = fetch_wechat_article(
        ToolContext(user_id="u", run_id="r"),
        FetchWechatArticleInput(url="https://mp.weixin.qq.com/s/abc123"),
    )

    assert result.status == "succeeded"
    assert result.channel == "email"
    assert len(result.candidates) == 1
    assert result.candidates[0].title == "算法工程师"
    assert result.visible_text == text
    assert result.artifact_id == f"observed:{digest}"
    assert result.source_url == "https://mp.weixin.qq.com/s/abc123"
    assert result.content_hash == digest


def test_ocr_non_success_statuses_pass_through_without_evidence(monkeypatch) -> None:
    monkeypatch.setattr(wechat_module, "_WECHAT_OCR_ENABLED", True)
    for status, reason in (
        ("needs_manual_review", "no_channel"),
        ("blocked", "unsafe_public_url"),
        ("skipped", "non_job"),
        ("failed", "slice_crash"),
        ("partial_success", "weak_ocr"),
    ):
        fake = _fake_slice(status=status, reason=reason)
        monkeypatch.setattr(
            "skill.job_discovery.runtime.wechat_slice.run_wechat_slice",
            partial(lambda f, *args, **kwargs: f, fake),
        )
        result = fetch_wechat_article(
            ToolContext(user_id="u", run_id="r"),
            FetchWechatArticleInput(url="https://mp.weixin.qq.com/s/abc123"),
        )
        assert result.status == status
        assert result.reason == reason
        assert result.artifact_id is None
        assert result.content_hash is None
        assert result.visible_text == ""


def test_ocr_disabled_uses_default_out_dir_only_when_enabled(monkeypatch) -> None:
    monkeypatch.setattr(wechat_module, "_WECHAT_OCR_ENABLED", True)
    fake = _fake_slice()
    captured: dict[str, object] = {}

    def _capture(url, **kwargs):
        captured["out_dir"] = kwargs.get("out_dir")
        return fake

    monkeypatch.setattr(
        "skill.job_discovery.runtime.wechat_slice.run_wechat_slice",
        _capture,
    )
    result = fetch_wechat_article(
        ToolContext(user_id="u", run_id="r"),
        FetchWechatArticleInput(url="https://mp.weixin.qq.com/s/abc123"),
    )
    assert captured["out_dir"] == wechat_module._WECHAT_OUT_DIR_DEFAULT
    assert result.status == "succeeded"

    result = fetch_wechat_article(
        ToolContext(user_id="u", run_id="r"),
        FetchWechatArticleInput(
            url="https://mp.weixin.qq.com/s/abc123", out_dir="C:/tmp/ocr-out"
        ),
    )
    assert captured["out_dir"] == "C:/tmp/ocr-out"


def test_enable_wechat_ocr_toggles_module_gate(monkeypatch) -> None:
    monkeypatch.setattr(wechat_module, "_WECHAT_OCR_ENABLED", False)
    enable_wechat_ocr(True)
    assert wechat_module._WECHAT_OCR_ENABLED is True
    enable_wechat_ocr(False)
    assert wechat_module._WECHAT_OCR_ENABLED is False


def test_wechat_input_normalizer_rejects_blank_url() -> None:
    with pytest.raises(ValueError):
        FetchWechatArticleInput(url="   ")
    assert FetchWechatArticleInput(url=" https://mp.weixin.qq.com/s/x ").url == (
        "https://mp.weixin.qq.com/s/x"
    )


def test_wechat_output_model_roundtrip() -> None:
    out = FetchWechatArticleOutput(
        url="https://mp.weixin.qq.com/s/x",
        status="succeeded",
        channel="career_url",
        candidates=[],
        ocr_text="text",
        needs_deep_crawl=True,
        reason=None,
        artifact_id="observed:abc",
        source_url="https://mp.weixin.qq.com/s/x",
        content_hash="abc",
        visible_text="text",
    )
    assert out.artifact_id == "observed:abc"
    assert out.needs_deep_crawl is True
