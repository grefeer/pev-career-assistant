"""Real public-page evidence tool used by the PEV job-discovery Skill."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.app.services.career_skills.job_discovery import (
    ExtractObservedJobDetailsInput,
    FetchPublicJobPageInput,
    PublicJobFetchError,
    SearchPublicJobPagesInput,
    extract_observed_job_details,
    fetch_public_job_page,
    search_public_job_pages,
    _assert_public_url,
    _direct_bing_result_url,
    _extract_jd_section,
    _find_observed_evidence,
    _infer_official_page_locations,
    _infer_official_page_title,
    _infer_recruitment_types,
)
from backend.app.services.agent_runtime.tool_context import ToolContext


def test_fetch_public_job_page_returns_hashable_visible_evidence(monkeypatch) -> None:
    """Executor receives source-backed text, not a model-generated JD claim."""
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery._assert_public_url",
        lambda url: None,
    )
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery.requests.get",
        lambda *args, **kwargs: SimpleNamespace(
            text="<html><title>AI Agent 开发工程师</title><body><h1>AI Agent 开发工程师</h1><p>职责：构建智能体。</p></body></html>",
            encoding="utf-8",
            apparent_encoding="utf-8",
            raise_for_status=lambda: None,
        ),
    )

    result = fetch_public_job_page(
        ToolContext(user_id="user-a", run_id="run-a"),
        FetchPublicJobPageInput(url="https://jobs.example/ai-agent"),
    )

    assert result.source_url == "https://jobs.example/ai-agent"
    assert result.artifact_id.startswith("observed:")
    assert result.title == "AI Agent 开发工程师"
    assert "职责：构建智能体。" in result.visible_text
    assert len(result.content_hash) == 64


def test_job_discovery_input_normalizers_reject_blank_values() -> None:
    with pytest.raises(ValueError):
        FetchPublicJobPageInput(url=" ")
    with pytest.raises(ValueError):
        SearchPublicJobPagesInput(query="   ")


def test_search_public_job_pages_returns_only_safe_direct_result_urls(monkeypatch) -> None:
    """Natural-language discovery receives public result links, never a browser redirect or private URL."""
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery.requests.get",
        lambda *args, **kwargs: SimpleNamespace(
            text="""
            <html><body>
              <li class=\"b_algo\"><h2><a href=\"https://www.bing.com/ck/a?u=a1aHR0cHM6Ly9jYXJlZXJzLmV4YW1wbGUvYWdlbnQ=\">Agent 应用开发工程师</a></h2><p>负责 AI Agent 应用。</p></li>
              <li class=\"b_algo\"><h2><a href=\"http://127.0.0.1/private\">private</a></h2><p>must not escape</p></li>
            </body></html>
            """,
            encoding="utf-8",
            apparent_encoding="utf-8",
            raise_for_status=lambda: None,
        ),
    )

    def assert_only_public(url: str) -> None:
        if "127.0.0.1" in url:
            raise PublicJobFetchError("unsafe_public_url")

    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery._assert_public_url",
        assert_only_public,
    )

    result = search_public_job_pages(
        ToolContext(user_id="user-a", run_id="run-a"),
        SearchPublicJobPagesInput(query="AI Agent 应用开发 官方招聘", max_results=5),
    )

    assert result.query == "AI Agent 应用开发 官方招聘"
    assert result.source_url.startswith("https://www.bing.com/search?")
    assert [item.model_dump() for item in result.results] == [{
        "title": "Agent 应用开发工程师",
        "url": "https://careers.example/agent",
        "snippet": "负责 AI Agent 应用。",
    }]
    assert len(result.content_hash) == 64


def test_search_public_job_pages_filters_safe_links_that_are_not_job_results(monkeypatch) -> None:
    """A generic article is not usable evidence merely because its URL is public."""
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery.requests.get",
        lambda *args, **kwargs: SimpleNamespace(
            text="""
            <html><body>
              <li class=\"b_algo\"><h2><a href=\"https://example.com/ai-agent-guide\">AI Agent 技术指南</a></h2><p>介绍智能体技术趋势。</p></li>
            </body></html>
            """,
            encoding="utf-8",
            apparent_encoding="utf-8",
            raise_for_status=lambda: None,
        ),
    )
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery._assert_public_url",
        lambda _url: None,
    )

    result = search_public_job_pages(
        ToolContext(user_id="user-a", run_id="run-a"),
        SearchPublicJobPagesInput(query="AI Agent 应用开发 官方招聘", max_results=5),
    )

    assert result.results == []


def test_search_public_job_pages_uses_a_public_360_fallback_when_bing_has_no_job_result(monkeypatch) -> None:
    """A provider fallback preserves direct provenance instead of inventing URLs."""
    responses = iter([
        SimpleNamespace(
            text="<html><body></body></html>", encoding="utf-8",
            apparent_encoding="utf-8", raise_for_status=lambda: None,
        ),
        SimpleNamespace(
            text="""
            <html><body><a data-mdurl="https://careers.example/jobs/agent">AI Agent 开发工程师招聘</a></body></html>
            """, encoding="utf-8", apparent_encoding="utf-8",
            raise_for_status=lambda: None,
        ),
    ])
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery.requests.get",
        lambda *args, **kwargs: next(responses),
    )
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery._assert_public_url",
        lambda _url: None,
    )

    result = search_public_job_pages(
        ToolContext(user_id="user-a", run_id="run-a"),
        SearchPublicJobPagesInput(query="AI Agent 应用开发 官方招聘", max_results=5),
    )

    assert result.source_url.startswith("https://www.so.com/s?")
    assert [item.model_dump() for item in result.results] == [{
        "title": "AI Agent 开发工程师招聘",
        "url": "https://careers.example/jobs/agent",
        "snippet": None,
    }]


def test_fetch_public_job_page_rejects_loopback_before_network_access() -> None:
    """An Agent cannot use a public-web tool to probe private infrastructure."""
    with pytest.raises(PublicJobFetchError, match="unsafe_public_url"):
        fetch_public_job_page(
            ToolContext(user_id="user-a", run_id="run-a"),
            FetchPublicJobPageInput(url="http://127.0.0.1:8000/private"),
        )


def test_fetch_public_job_page_rejects_a_short_login_or_soft_block_page(monkeypatch) -> None:
    """An accessible login shell is not usable job evidence."""
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery._assert_public_url",
        lambda _url: None,
    )
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery.requests.get",
        lambda *args, **kwargs: SimpleNamespace(
            text="<html><title>登录</title><body>请先登录后查看</body></html>",
            encoding="utf-8", apparent_encoding="utf-8",
            raise_for_status=lambda: None,
        ),
    )

    with pytest.raises(PublicJobFetchError, match="public_page_content_insufficient"):
        fetch_public_job_page(
            ToolContext(user_id="user-a", run_id="run-a"),
            FetchPublicJobPageInput(url="https://jobs.example/login"),
        )


def test_public_fetch_handles_network_encoding_and_empty_page_failures(monkeypatch) -> None:
    monkeypatch.setattr("backend.app.services.career_skills.job_discovery._assert_public_url", lambda _url: None)
    import requests

    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery.requests.get",
        lambda *args, **kwargs: (_ for _ in ()).throw(requests.RequestException("down")),
    )
    with pytest.raises(PublicJobFetchError, match="public_fetch_failed"):
        fetch_public_job_page(ToolContext(user_id="u", run_id="r"), FetchPublicJobPageInput(url="https://jobs.example"))

    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery.requests.get",
        lambda *args, **kwargs: SimpleNamespace(
            text="<html><body></body></html>", encoding="latin-1", apparent_encoding="utf-8",
            raise_for_status=lambda: None,
        ),
    )
    with pytest.raises(PublicJobFetchError, match="empty_public_page"):
        fetch_public_job_page(ToolContext(user_id="u", run_id="r"), FetchPublicJobPageInput(url="https://jobs.example"))


def test_search_and_extraction_reject_missing_or_malformed_public_evidence(monkeypatch) -> None:
    import requests

    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery.requests.get",
        lambda *args, **kwargs: (_ for _ in ()).throw(requests.RequestException("down")),
    )
    with pytest.raises(PublicJobFetchError, match="public_search_failed"):
        search_public_job_pages(
            ToolContext(user_id="u", run_id="r"), SearchPublicJobPagesInput(query="AI Agent")
        )
    with pytest.raises(PublicJobFetchError, match="observed_evidence_not_found"):
        extract_observed_job_details(
            ToolContext(user_id="u", run_id="r", metadata={}),
            ExtractObservedJobDetailsInput(artifact_id="missing"),
        )
    with pytest.raises(PublicJobFetchError, match="observed_evidence_incomplete"):
        extract_observed_job_details(
            ToolContext(user_id="u", run_id="r", metadata={"observed_public_evidence": [{"artifact_id": "bad"}]}),
            ExtractObservedJobDetailsInput(artifact_id="bad"),
        )


def test_job_discovery_helpers_only_accept_safe_bing_urls_and_known_recruitment_paths(monkeypatch) -> None:
    with pytest.raises(PublicJobFetchError, match="unsafe_public_url"):
        _assert_public_url("ftp://jobs.example/file")
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery.socket.getaddrinfo",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("dns")),
    )
    with pytest.raises(PublicJobFetchError, match="unresolvable"):
        _assert_public_url("https://jobs.example")
    assert _direct_bing_result_url("https://www.bing.com/not-a-redirect") is None
    assert _direct_bing_result_url("https://www.bing.com/ck/a?u=bad") is None
    assert _direct_bing_result_url("https://example.com/jobs/1") == "https://example.com/jobs/1"
    assert _infer_recruitment_types("https://talent.example/GRADUATE/1", ["fallback"]) == ["campus"]
    assert _infer_recruitment_types("https://talent.example/INTERN/1", ["fallback"]) == ["internship"]
    assert _infer_recruitment_types("https://talent.example/other", ["fallback"]) == ["fallback"]
    assert _extract_jd_section("没有标签", labels=("岗位职责",)) == ""


def test_job_discovery_helper_fallbacks_do_not_infer_missing_header_values() -> None:
    assert _find_observed_evidence(ToolContext(user_id="u", run_id="r", metadata={"observed_public_evidence": []}), "missing") is None
    assert _infer_official_page_title("首页\n申请职位\n普通文本") is None
    assert _infer_official_page_locations("标题\n岗位职责", None) == []
    assert _infer_official_page_locations("标题\n岗位职责", "不存在") == []
    assert _infer_official_page_locations("标题\n上海市\n岗位职责", "标题") == ["上海市"]
    assert _infer_official_page_locations("标题\n岗位职责\n北京市", "标题") == []
    assert _direct_bing_result_url("https://www.bing.com/ck/a?u=a1____") is None


def test_search_skips_redirects_and_unsafe_results_and_honors_result_limit(monkeypatch) -> None:
    html = """
    <li class='b_algo'><h2><a href='https://www.bing.com/not-a-redirect'>ignored</a></h2></li>
    <li class='b_algo'><h2><a href='https://unsafe.example/jobs/agent'>unsafe job</a></h2></li>
    <li class='b_algo'><h2><a href='https://careers.example/jobs/agent'>safe job</a></h2></li>
    <li class='b_algo'><h2><a href='https://careers.example/jobs/second'>second job</a></h2></li>
    """
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery.requests.get",
        lambda *args, **kwargs: SimpleNamespace(text=html, encoding="utf-8", apparent_encoding="utf-8", raise_for_status=lambda: None),
    )
    def only_safe(url: str) -> None:
        if url.startswith("https://unsafe"):
            raise PublicJobFetchError("unsafe_public_url")
    monkeypatch.setattr("backend.app.services.career_skills.job_discovery._assert_public_url", only_safe)

    result = search_public_job_pages(
        ToolContext(user_id="u", run_id="r"), SearchPublicJobPagesInput(query="Agent 招聘", max_results=1)
    )

    assert [item.url for item in result.results] == ["https://careers.example/jobs/agent"]


def test_extract_input_accepts_the_ephemeral_id_emitted_by_public_page_fetch() -> None:
    """One Executor step can pass the exact fetch observation into detail extraction."""
    payload = ExtractObservedJobDetailsInput(artifact_id="observed:" + "a" * 64)

    assert payload.artifact_id == "observed:" + "a" * 64


def test_extract_observed_job_details_returns_structured_fields_only_from_captured_evidence() -> None:
    """Detailed JD output must be derived from the selected immutable page evidence."""
    context = ToolContext(
        user_id="user-a",
        run_id="run-a",
        metadata={"observed_public_evidence": [{
            "artifact_id": "artifact-ai-agent",
            "source_url": "https://jobs.example/ai-agent",
            "content_hash": "a" * 64,
            "title": "招聘详情",
            "visible_text": """
岗位名称：AI Agent 开发工程师
公司：示例科技
岗位职责：负责 RAG、Agent 平台和工具调用能力开发。
任职要求：熟悉 Python、LLM 和工程化部署。
工作地点：北京
""",
        }]},
    )

    result = extract_observed_job_details(
        context,
        ExtractObservedJobDetailsInput(artifact_id="artifact-ai-agent"),
    )

    assert result.source_artifact_id == "artifact-ai-agent"
    assert [candidate.model_dump() for candidate in result.candidates] == [{
        "title": "AI Agent 开发工程师",
        "company_name": "示例科技",
        "locations": ["北京"],
        "responsibilities": "负责 RAG、Agent 平台和工具调用能力开发。",
        "requirements": "熟悉 Python、LLM 和工程化部署。",
        "recruitment_types": [],
        "apply_url": "https://jobs.example/ai-agent",
        "deadline_text": None,
        "confidence": 1.0,
        "evidence_refs": [{
            "artifact_id": "artifact-ai-agent",
            "source_url": "https://jobs.example/ai-agent",
            "content_hash": "a" * 64,
        }],
        "normalization_warnings": [],
    }]


def test_extract_observed_job_details_handles_official_page_without_labeled_title() -> None:
    """A navigation button must not replace the true title on common official career pages."""
    context = ToolContext(
        user_id="user-a",
        run_id="run-a",
        metadata={"observed_public_evidence": [{
            "artifact_id": "artifact-official",
            "source_url": "https://jobs.example/official",
            "content_hash": "b" * 64,
            "title": "官方招聘",
            "visible_text": """
首页
职位
2027AIDU-智能体算法工程师(J99969)
北京市
技术
工作职责：
负责 AI Agent 的设计与研发。
职责要求：
熟悉 Python、RAG 和 Agent 开发框架。
申请职位
""",
        }]},
    )

    result = extract_observed_job_details(
        context,
        ExtractObservedJobDetailsInput(artifact_id="artifact-official"),
    )

    candidate = result.candidates[0]
    assert candidate.title == "2027AIDU-智能体算法工程师(J99969)"
    assert candidate.locations == ["北京市"]
    assert candidate.responsibilities == "负责 AI Agent 的设计与研发。"
    assert candidate.requirements == "熟悉 Python、RAG 和 Agent 开发框架。"


def test_extract_observed_job_details_derives_social_type_and_clears_resolved_location_warning() -> None:
    """Source path and recovered location must correct, not contradict, legacy heuristics."""
    context = ToolContext(
        user_id="user-a",
        run_id="run-a",
        metadata={"observed_public_evidence": [{
            "artifact_id": "artifact-social",
            "source_url": "https://talent.example/jobs/detail/SOCIAL/abc",
            "content_hash": "c" * 64,
            "title": "官方招聘",
            "visible_text": """
Agent研发工程师
北京市
工作职责：负责 Agent 系统研发。
职责要求：熟悉 Python。
申请职位
""",
        }]},
    )

    result = extract_observed_job_details(
        context,
        ExtractObservedJobDetailsInput(artifact_id="artifact-social"),
    )

    candidate = result.candidates[0]
    assert candidate.recruitment_types == ["social"]
    assert "No location information found" not in candidate.normalization_warnings
