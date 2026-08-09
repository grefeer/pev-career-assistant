"""PEV URL site-classification tool (classify-job-url) unit tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import backend.app.services.career_skills.classify_url as classify_module
from backend.app.services.career_skills.classify_url import (
    ClassifiedJobUrl,
    ClassifyJobUrlInput,
    ClassifyJobUrlOutput,
    classify_job_url,
    _visible_text_length,
)
from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.career_skills.job_discovery import PublicJobFetchError


def _response(content: bytes, status_code: int = 200) -> SimpleNamespace:
    return SimpleNamespace(status_code=status_code, content=content)


def _classify(url: str) -> ClassifiedJobUrl:
    return classify_job_url(
        ToolContext(user_id="u", run_id="r"),
        ClassifyJobUrlInput(urls=[url]),
    ).results[0]


def test_wechat_host_needs_no_network(monkeypatch) -> None:
    def _boom(*args, **kwargs):
        raise AssertionError("wechat classification must not probe the network")

    monkeypatch.setattr(classify_module, "_fetch_validated", _boom)
    result = _classify("https://mp.weixin.qq.com/s/abc123")
    assert result.site_class == "wechat"
    assert result.evidence_signal == "host=mp.weixin.qq.com"
    assert _classify("https://readgzh.com/p/1").site_class == "wechat"


def test_adapter_host_signal(monkeypatch) -> None:
    monkeypatch.setattr(classify_module, "_adapter_company_for_url", lambda url: "moka")
    result = _classify("https://app.mokahr.com/position/list")
    assert result.site_class == "adapter"
    assert result.evidence_signal == "adapter=moka"


def test_probe_fetch_error_is_blocked(monkeypatch) -> None:
    def _raise(url):
        raise PublicJobFetchError("public_host_unresolvable")

    monkeypatch.setattr(classify_module, "_fetch_validated", _raise)
    result = _classify("https://jobs.example/x")
    assert result.site_class == "blocked"
    assert result.evidence_signal == "public_host_unresolvable"


def test_non_200_probe_is_blocked(monkeypatch) -> None:
    monkeypatch.setattr(
        classify_module, "_fetch_validated", lambda url: _response(b"<html>gone</html>", 404)
    )
    result = _classify("https://jobs.example/missing")
    assert result.site_class == "blocked"
    assert result.evidence_signal == "http_404"


def test_anti_bot_markers_are_blocked(monkeypatch) -> None:
    monkeypatch.setattr(
        classify_module,
        "_fetch_validated",
        lambda url: _response("<html><title>安全验证</title><body>滑块验证</body></html>".encode("utf-8")),
    )
    result = _classify("https://jobs.example/gated")
    assert result.site_class == "blocked"
    assert result.evidence_signal == "anti_bot_markers"


def test_jd_section_markers_are_static(monkeypatch) -> None:
    monkeypatch.setattr(
        classify_module,
        "_fetch_validated",
        lambda url: _response("<html><h1>岗位名称：算法工程师</h1><p>岗位职责：写代码</p></html>".encode("utf-8")),
    )
    result = _classify("https://jobs.example/jd")
    assert result.site_class == "static"
    assert result.evidence_signal == "jd_section_markers"


def test_visible_text_makes_static(monkeypatch) -> None:
    body = "<html><body>" + "职位介绍" + ("内容" * 120) + "</body></html>"
    monkeypatch.setattr(
        classify_module, "_fetch_validated", lambda url: _response(body.encode("utf-8"))
    )
    result = _classify("https://jobs.example/texty")
    assert result.site_class == "static"
    assert result.evidence_signal == "visible_text"


def test_bare_js_shell_is_spa(monkeypatch) -> None:
    monkeypatch.setattr(
        classify_module,
        "_fetch_validated",
        lambda url: _response(
            b"<!doctype html><div id=app></div><script src=/assets/app.js></script>"
        ),
    )
    result = _classify("https://jobs.example/spa")
    assert result.site_class == "spa"
    assert result.evidence_signal == "js_bundle_only"


def test_visible_text_length_strips_scripts_and_tags() -> None:
    html = "<html><script>var x = '岗位';</script><style>.x{}</style><p>职位 职责</p></html>"
    assert _visible_text_length(html) == 4


def test_classify_input_rejects_blank_and_duplicate_urls() -> None:
    with pytest.raises(ValueError):
        ClassifyJobUrlInput(urls=["", "https://jobs.example/x"])
    with pytest.raises(ValueError, match="unique"):
        ClassifyJobUrlInput(urls=["https://jobs.example/x", "https://jobs.example/x"])


def test_classify_models_roundtrip() -> None:
    out = ClassifyJobUrlOutput(
        results=[
            {"url": "https://jobs.example/x", "site_class": "spa", "evidence_signal": "s"}
        ]
    )
    assert out.results[0].site_class == "spa"
