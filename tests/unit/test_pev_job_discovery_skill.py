"""Real public-page evidence tool used by the PEV job-discovery Skill."""

from __future__ import annotations

import hashlib
import sys
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from backend.app.services.career_skills.job_discovery import (
    ExtractObservedJobDetailsInput,
    ExtractObservedJobDetailsBatchInput,
    FetchPublicJobPageInput,
    FetchPublicJobPageOutput,
    FetchPublicJobPagesInput,
    PublicJobFetchError,
    SearchPublicJobPagesInput,
    enable_playwright_fallback,
    extract_observed_job_details,
    extract_observed_job_details_batch,
    fetch_public_job_page,
    fetch_public_job_pages,
    search_public_job_pages,
    _assert_public_url,
    _BingSearchResultParser,
    _SoSearchResultParser,
    _direct_bing_result_url,
    _extract_jd_section,
    _find_observed_evidence,
    _infer_official_page_locations,
    _infer_official_page_title,
    _infer_recruitment_types,
    _is_public_url,
    _render_with_playwright,
    _fetch_validated,
    _MAX_PUBLIC_REDIRECTS,
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
            text=(
                "<html><title>AI Agent 开发工程师</title><body>"
                "<script>secret</script><style>.x{}</style>"
                "<h1>AI Agent 开发工程师</h1>"
                "<p>职责：构建智能体。" + "岗位职责与任职要求补充说明。" * 12 + "</p></body></html>"
            ),
            encoding="utf-8",
            apparent_encoding="utf-8",
            raise_for_status=lambda: None,
            is_redirect=False,
            headers={},
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
    with pytest.raises(ValueError, match="duplicates"):
        FetchPublicJobPagesInput(urls=["https://jobs.example/a", "https://jobs.example/a"])
    with pytest.raises(ValueError, match="unique"):
        ExtractObservedJobDetailsBatchInput(artifact_ids=["observed:a", "observed:a"])


def test_batch_fetch_preserves_successful_pages_and_explicit_per_url_failures(monkeypatch) -> None:
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery.fetch_public_job_page",
        lambda _context, payload: (
            (_ for _ in ()).throw(PublicJobFetchError("public_fetch_failed"))
            if payload.url.endswith("bad")
            else FetchPublicJobPageOutput(
                artifact_id=f"observed:{payload.url[-1]}", source_url=payload.url,
                title="AI Agent 开发", visible_text="岗位职责和任职要求。",
                content_hash=payload.url[-1] * 64,
            )
        ),
    )

    result = fetch_public_job_pages(
        ToolContext(user_id="u", run_id="r"),
        FetchPublicJobPagesInput(
            urls=["https://jobs.example/a", "https://jobs.example/bad"]
        ),
    )

    assert [page.source_url for page in result.pages] == ["https://jobs.example/a"]
    assert [failure.model_dump() for failure in result.failures] == [{
        "source_url": "https://jobs.example/bad",
        "error_code": "public_fetch_failed",
    }]


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
            <html><body>
              <a data-mdurl="https://careers.example/jobs/agent">AI Agent 开发工程师招聘</a>
              <a data-mdurl="https://careers.example/jobs/second">第二个岗位</a>
            </body></html>
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
        SearchPublicJobPagesInput(query="AI Agent 应用开发 官方招聘", max_results=1),
    )

    assert result.source_url.startswith("https://www.so.com/s?")
    assert [item.model_dump() for item in result.results] == [{
        "title": "AI Agent 开发工程师招聘",
        "url": "https://careers.example/jobs/agent",
        "snippet": None,
    }]


def test_search_qualifies_the_query_with_recruiting_site_operators(monkeypatch) -> None:
    """P2/B4: the provider query is biased toward recruiting domains (unless the
    agent already steers with site:), while the reported query stays verbatim."""
    requested: list[str] = []

    def fake_get(url: str, *args, **kwargs) -> SimpleNamespace:
        requested.append(url)
        return SimpleNamespace(
            text="""
            <html><body>
              <li class="b_algo"><h2><a href="https://careers.example/jobs/agent">AI Agent 开发工程师</a></h2><p>招聘详情。</p></li>
            </body></html>
            """,
            encoding="utf-8", apparent_encoding="utf-8", raise_for_status=lambda: None,
        )

    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery.requests.get", fake_get
    )
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery._assert_public_url",
        lambda _url: None,
    )

    result = search_public_job_pages(
        ToolContext(user_id="u", run_id="r"),
        SearchPublicJobPagesInput(query="Java 后端开发工程师 公开 JD", max_results=5),
    )

    sent_query = parse_qs(urlsplit(requested[0]).query)["q"][0]
    assert "site:liepin.com" in sent_query
    assert "site:jobs.bytedance.com" in sent_query
    assert "site:juejin.cn" in sent_query
    assert result.query == "Java 后端开发工程师 公开 JD"


def test_search_keeps_an_agent_supplied_site_operator_verbatim(monkeypatch) -> None:
    """An existing site: steering is never clobbered by the default operators."""
    requested: list[str] = []

    def fake_get(url: str, *args, **kwargs) -> SimpleNamespace:
        requested.append(url)
        return SimpleNamespace(
            text="<html><body></body></html>", encoding="utf-8",
            apparent_encoding="utf-8", raise_for_status=lambda: None,
        )

    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery.requests.get", fake_get
    )

    result = search_public_job_pages(
        ToolContext(user_id="u", run_id="r"),
        SearchPublicJobPagesInput(query="Java 岗位 site:liepin.com", max_results=5),
    )

    sent_query = parse_qs(urlsplit(requested[0]).query)["q"][0]
    assert "site:liepin.com" in sent_query
    assert "site:iguopin.com" not in sent_query
    assert result.results == []


def test_search_drops_text_only_matches_on_unknown_hosts(monkeypatch) -> None:
    """P2/B4: a tutorial/encyclopedia page that merely mentions 招聘/岗位 in its
    title is not discovery evidence when its host is not a recruiting domain."""
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery.requests.get",
        lambda *args, **kwargs: SimpleNamespace(
            text="""
            <html><body>
              <li class="b_algo"><h2><a href="https://tutorial.example/ai-agent-guide">Java 后端开发工程师 面试教程</a></h2><p>涵盖常见面试题与答案。</p></li>
            </body></html>
            """,
            encoding="utf-8", apparent_encoding="utf-8", raise_for_status=lambda: None,
        ),
    )
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery._assert_public_url",
        lambda _url: None,
    )

    result = search_public_job_pages(
        ToolContext(user_id="u", run_id="r"),
        SearchPublicJobPagesInput(query="Java 后端开发工程师 公开 JD", max_results=5),
    )

    assert result.results == []


def test_search_keeps_unknown_host_with_a_job_shaped_url_and_whitelisted_host_text_match(monkeypatch) -> None:
    """P2/B4: an unlisted host still passes on a job-shaped URL path; whitelisted
    recruiting hosts keep the loose text-signal check."""
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery.requests.get",
        lambda *args, **kwargs: SimpleNamespace(
            text="""
            <html><body>
              <li class="b_algo"><h2><a href="https://company-a.example/jobs/backend-engineer">后端开发</a></h2><p>负责服务端。</p></li>
              <li class="b_algo"><h2><a href="https://www.liepin.com/guide">Java 后端开发工程师 求职指南</a></h2><p>求职建议。</p></li>
            </body></html>
            """,
            encoding="utf-8", apparent_encoding="utf-8", raise_for_status=lambda: None,
        ),
    )
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery._assert_public_url",
        lambda _url: None,
    )

    result = search_public_job_pages(
        ToolContext(user_id="u", run_id="r"),
        SearchPublicJobPagesInput(query="Java 后端开发工程师 公开 JD", max_results=5),
    )

    assert [item.url for item in result.results] == [
        "https://company-a.example/jobs/backend-engineer",
        "https://www.liepin.com/guide",
    ]


def test_search_whitelist_exact_host_match_and_double_negative_drop(monkeypatch) -> None:
    """P2/B4 edge branches: an exact whitelist host (no subdomain) passes on a
    text signal; a whitelisted host with neither a job-shaped URL nor JD wording
    is still dropped."""
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery.requests.get",
        lambda *args, **kwargs: SimpleNamespace(
            text="""
            <html><body>
              <li class="b_algo"><h2><a href="https://liepin.com/intro">Java 后端开发工程师 内推</a></h2><p>内推信息。</p></li>
              <li class="b_algo"><h2><a href="https://www.liepin.com/plain-page">无相关内容</a></h2><p>。</p></li>
            </body></html>
            """,
            encoding="utf-8", apparent_encoding="utf-8", raise_for_status=lambda: None,
        ),
    )
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery._assert_public_url",
        lambda _url: None,
    )

    result = search_public_job_pages(
        ToolContext(user_id="u", run_id="r"),
        SearchPublicJobPagesInput(query="Java 后端开发工程师 公开 JD", max_results=5),
    )

    assert [item.url for item in result.results] == ["https://liepin.com/intro"]


def test_search_keeps_juejin_pins_and_drops_non_job_posts(monkeypatch) -> None:
    """Q143/R032/R033 regression: 稀土掘金 招聘帖 pins (/pin/<id>) carry no
    job-shaped URL token, so juejin.cn must stay whitelisted and pass on the
    text signal; a juejin post without job wording is still dropped."""
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery.requests.get",
        lambda *args, **kwargs: SimpleNamespace(
            text="""
            <html><body>
              <li class="b_algo"><h2><a href="https://juejin.cn/pin/6931214116753244174">【招聘】前端开发工程师（2年经验）</a></h2><p>坐标杭州，双休。</p></li>
              <li class="b_algo"><h2><a href="https://juejin.cn/post/7290000000000000000">前端进阶学习笔记</a></h2><p>记录学习心得。</p></li>
            </body></html>
            """,
            encoding="utf-8", apparent_encoding="utf-8", raise_for_status=lambda: None,
        ),
    )
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery._assert_public_url",
        lambda _url: None,
    )

    result = search_public_job_pages(
        ToolContext(user_id="u", run_id="r"),
        SearchPublicJobPagesInput(query="稀土掘金 前端开发工程师 招聘", max_results=5),
    )

    assert [item.url for item in result.results] == [
        "https://juejin.cn/pin/6931214116753244174"
    ]


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
            is_redirect=False,
            headers={},
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
            is_redirect=False,
            headers={},
        ),
    )
    with pytest.raises(PublicJobFetchError, match="empty_public_page"):
        fetch_public_job_page(ToolContext(user_id="u", run_id="r"), FetchPublicJobPageInput(url="https://jobs.example"))


def test_fetch_public_job_page_follows_redirect_to_a_revalidated_public_target(monkeypatch) -> None:
    """A public->public redirect is re-validated per hop, then the final page is returned."""
    validated: list[str] = []
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery._assert_public_url",
        lambda url: validated.append(url),
    )

    def fake_get(url, *args, **kwargs):
        if url == "https://jobs.example/redir":
            return SimpleNamespace(
                text="", encoding="utf-8", apparent_encoding="utf-8",
                raise_for_status=lambda: None,
                is_redirect=True, status_code=302,
                headers={"Location": "https://jobs.example/agent"},
            )
        return SimpleNamespace(
            text=(
                "<html><title>AI Agent 开发</title><body><p>岗位职责和任职要求详情描述。"
                + "任职要求补充说明详情。" * 16
                + "</p></body></html>"
            ),
            encoding="utf-8", apparent_encoding="utf-8",
            raise_for_status=lambda: None,
            is_redirect=False, status_code=200, headers={},
        )

    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery.requests.get", fake_get,
    )

    result = fetch_public_job_page(
        ToolContext(user_id="user-a", run_id="run-a"),
        FetchPublicJobPageInput(url="https://jobs.example/redir"),
    )

    # The initial URL and the redirect target are both re-validated.
    assert validated == ["https://jobs.example/redir", "https://jobs.example/agent"]
    assert result.source_url == "https://jobs.example/redir"
    assert "岗位职责" in result.visible_text


def test_fetch_public_job_page_rejects_redirect_to_a_private_target(monkeypatch) -> None:
    """A public page that 302-redirects to a private address must not be followed."""
    def assert_public(url: str) -> None:
        if "127.0.0.1" in url:
            raise PublicJobFetchError("unsafe_public_url")

    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery._assert_public_url", assert_public,
    )
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery.requests.get",
        lambda *args, **kwargs: SimpleNamespace(
            text="", encoding="utf-8", apparent_encoding="utf-8",
            raise_for_status=lambda: None,
            is_redirect=True, status_code=302,
            headers={"Location": "http://127.0.0.1:8000/admin"},
        ),
    )

    with pytest.raises(PublicJobFetchError, match="unsafe_public_url"):
        fetch_public_job_page(
            ToolContext(user_id="user-a", run_id="run-a"),
            FetchPublicJobPageInput(url="https://jobs.example/redir"),
        )


def test_fetch_validated_returns_redirect_response_when_location_header_missing(monkeypatch) -> None:
    """A 3xx without a Location header cannot be followed; it is returned as-is."""
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery.requests.get",
        lambda *args, **kwargs: SimpleNamespace(
            text="", encoding="utf-8", apparent_encoding="utf-8",
            raise_for_status=lambda: None,
            is_redirect=True, status_code=302, headers={},
        ),
    )

    response = _fetch_validated("https://jobs.example/orphan")

    assert response.is_redirect is True


def test_fetch_validated_rejects_redirect_loops_as_unsafe(monkeypatch) -> None:
    """A redirect chain longer than the hop budget is treated as unsafe."""
    calls = {"count": 0}

    def fake_get(url, *args, **kwargs):
        calls["count"] += 1
        return SimpleNamespace(
            text="", encoding="utf-8", apparent_encoding="utf-8",
            raise_for_status=lambda: None,
            is_redirect=True, status_code=302,
            headers={"Location": "https://jobs.example/loop"},
        )

    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery.requests.get", fake_get,
    )
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery._assert_public_url", lambda _url: None,
    )

    with pytest.raises(PublicJobFetchError, match="unsafe_public_url"):
        _fetch_validated("https://jobs.example/start")

    assert calls["count"] == _MAX_PUBLIC_REDIRECTS + 1


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


def test_search_reports_a_fallback_provider_failure_without_fabricating_results(monkeypatch) -> None:
    import requests

    responses = iter([
        SimpleNamespace(
            text="<html><body></body></html>", encoding="utf-8",
            apparent_encoding="utf-8", raise_for_status=lambda: None,
        ),
        requests.RequestException("360 down"),
    ])

    def request_or_raise(*_args, **_kwargs):
        response = next(responses)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery.requests.get", request_or_raise
    )

    with pytest.raises(PublicJobFetchError, match="public_search_failed"):
        search_public_job_pages(
            ToolContext(user_id="u", run_id="r"), SearchPublicJobPagesInput(query="AI Agent")
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
    assert _direct_bing_result_url("https://www.bing.com/ck/a?u=a1ZnRwOi8vZXhhbXBsZS5jb20=") is None


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
    <li class='b_algo'><h2><a href='https://careers.example/jobs/empty'>   </a></h2></li>
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
        "skills": [],
        "min_degree": None,
        "priority": "unknown",
        "taxonomy": ["开发", "研发工程师"],
        "strength": {
            "base_score": 0,
            "evidence": [{"evidence": "熟悉 Python", "label": "明确技能栈", "weight": 2}],
            "score": 2,
            "tier": "low",
        },
    }]
    batch = extract_observed_job_details_batch(
        context, ExtractObservedJobDetailsBatchInput(artifact_ids=["artifact-ai-agent"])
    )
    assert batch.details == [result]


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


def test_extract_observed_job_details_keeps_per_card_sections_on_multi_candidate_pages() -> None:
    """A card listing must not have the page's first section copied to every card."""
    context = ToolContext(
        user_id="user-a",
        run_id="run-a",
        metadata={"observed_public_evidence": [{
            "artifact_id": "artifact-list",
            "source_url": "https://nio.jobs.feishu.cn/campus/list",
            "content_hash": "d" * 64,
            "title": "校招职位",
            "visible_text": """
座舱Agent Harness算法工程师
北京、上海校招正式职位 ID：A33756
岗位职责：负责座舱 Agent harness 的设计。
Agent编排平台工程师
上海校招正式职位 ID：A33757
工作职责：负责 Agent 编排平台。
""",
        }]},
    )

    result = extract_observed_job_details(
        context,
        ExtractObservedJobDetailsInput(artifact_id="artifact-list"),
    )

    assert [c.title for c in result.candidates] == [
        "座舱Agent Harness算法工程师",
        "Agent编排平台工程师",
    ]
    # Each candidate keeps the section from its own card, never the page's
    # first 岗位职责 match (which would otherwise bleed onto both).
    assert result.candidates[0].responsibilities == "负责座舱 Agent harness 的设计。"
    assert result.candidates[1].responsibilities == "负责 Agent 编排平台。"


def test_extract_observed_job_details_keeps_candidate_section_when_page_section_misses() -> None:
    """Labels only the segment extractor knows (职责描述/requirements) survive."""
    context = ToolContext(
        user_id="user-a",
        run_id="run-a",
        metadata={"observed_public_evidence": [{
            "artifact_id": "artifact-alt",
            "source_url": "https://jobs.example/alt",
            "content_hash": "e" * 64,
            "title": "招聘详情",
            "visible_text": """
岗位名称：AI Agent 开发工程师
公司：示例科技
职责描述：
负责 Agent 工具链开发。
requirements:
熟悉 Python 与 Agent 框架。
""",
        }]},
    )

    result = extract_observed_job_details(
        context,
        ExtractObservedJobDetailsInput(artifact_id="artifact-alt"),
    )

    candidate = result.candidates[0]
    assert candidate.responsibilities == "负责 Agent 工具链开发。"
    assert candidate.requirements == "熟悉 Python 与 Agent 框架。"


def test_extract_observed_job_details_enriches_feishu_style_sections() -> None:
    """Feishu detail pages label responsibilities as 你将负责 / 岗位定位."""
    context = ToolContext(
        user_id="user-a",
        run_id="run-a",
        metadata={"observed_public_evidence": [{
            "artifact_id": "artifact-feishu-detail",
            "source_url": "https://nio.jobs.feishu.cn/detail/1",
            "content_hash": "f" * 64,
            "title": "职位详情",
            "visible_text": """
座舱Agent Harness算法工程师
你将负责：
- 探索 agent harness 在座舱中的应用
- 落地工具调用与编排
岗位定位：座舱内智能体平台
岗位要求：熟悉 Python
""",
        }]},
    )

    result = extract_observed_job_details(
        context,
        ExtractObservedJobDetailsInput(artifact_id="artifact-feishu-detail"),
    )

    candidate = result.candidates[0]
    assert "探索 agent harness" in candidate.responsibilities
    assert "落地工具调用与编排" in candidate.responsibilities
    assert candidate.requirements == "熟悉 Python"


def test_bing_result_parser_ignores_non_content_start_tags_inside_a_result() -> None:
    """A <div> inside <li> hits no h2/p/a-in-heading branch and is ignored."""
    parser = _BingSearchResultParser()
    parser.feed('<li class="b_algo"><div>ignored</div></li>')
    assert parser.results == []


def test_bing_result_parser_skips_anchor_in_heading_without_href() -> None:
    """An <a> inside <h2> without href captures no url and yields no result."""
    parser = _BingSearchResultParser()
    parser.feed('<li class="b_algo"><h2><a>title no href</a></h2><p>snip</p></li>')
    assert parser.results == []


def test_bing_result_parser_drops_data_outside_heading_and_snippet() -> None:
    """Loose text before <h2> is neither title nor snippet and is dropped."""
    parser = _BingSearchResultParser()
    parser.feed(
        '<li class="b_algo">loose text<h2><a href="https://x.example">t</a></h2><p>s</p></li>'
    )
    assert [r["url"] for r in parser.results] == ["https://x.example"]


def test_so_search_parser_skips_whitespace_only_and_empty_anchor_text() -> None:
    """Whitespace-only and empty anchor text produce no result entry."""
    whitespace = _SoSearchResultParser()
    whitespace.feed('<a data-mdurl="https://x.example">   </a>')
    assert whitespace.results == []

    empty = _SoSearchResultParser()
    empty.feed('<a data-mdurl="https://x.example"></a>')
    assert empty.results == []


def test_assert_public_url_accepts_a_globally_routable_host(monkeypatch) -> None:
    """A public hostname resolving to a global IP passes without raising."""
    import socket as _socket

    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery.socket.getaddrinfo",
        lambda *args, **kwargs: [
            (_socket.AF_INET, _socket.SOCK_STREAM, 0, "", ("1.1.1.1", 0)),
        ],
    )
    _assert_public_url("https://jobs.example")  # must not raise


def test_search_fallback_keeps_all_plausible_results_when_under_the_limit(monkeypatch) -> None:
    """The fallback loop continues past the first result when the cap is not yet hit."""
    responses = iter([
        SimpleNamespace(
            text="<html><body></body></html>", encoding="utf-8",
            apparent_encoding="utf-8", raise_for_status=lambda: None,
        ),
        SimpleNamespace(
            text="""
            <html><body>
              <a data-mdurl="https://careers.example/jobs/agent">AI Agent 开发工程师招聘</a>
              <a data-mdurl="https://careers.example/jobs/second">第二个岗位招聘</a>
            </body></html>
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
        ToolContext(user_id="u", run_id="r"),
        SearchPublicJobPagesInput(query="AI Agent 应用开发 官方招聘", max_results=5),
    )
    assert [item.url for item in result.results] == [
        "https://careers.example/jobs/agent",
        "https://careers.example/jobs/second",
    ]


def test_find_observed_evidence_skips_non_matching_items_before_a_match() -> None:
    """A non-matching evidence item is skipped (loop continues) before the match."""
    other = {"artifact_id": "other", "content_hash": "b" * 64}
    target = {"artifact_id": "jd", "content_hash": "a" * 64}
    context = ToolContext(
        user_id="u", run_id="r", metadata={"observed_public_evidence": [other, target]}
    )
    assert _find_observed_evidence(context, "jd") is target
    assert _find_observed_evidence(context, f"observed:{'a' * 64}") is target
    assert _find_observed_evidence(context, "missing") is None


def test_infer_official_page_locations_returns_empty_when_no_city_line_follows_title() -> None:
    """A non-city, non-responsibilities line after the title is skipped and the loop ends empty."""
    assert _infer_official_page_locations("标题\nabc", "标题") == []


# ---------------------------------------------------------------- playwright fallback

def _no_op_public_url(url: str) -> None:
    pass


def _sentinel_fallback(*args, **kwargs):
    raise AssertionError("playwright fallback must not run")


class _FakeRoute:
    def __init__(self) -> None:
        self.continued = False
        self.aborted = False

    def continue_(self) -> None:
        self.continued = True

    def abort(self) -> None:
        self.aborted = True


class _FakePage:
    def __init__(
        self,
        *,
        body: str = "",
        title: str | None = None,
        goto_result: object | None = ...,
        goto_error: Exception | None = None,
        inner_text_error: Exception | None = None,
        body_sequence: list[str] | None = None,
    ) -> None:
        self._body = body
        self._title = title
        self._goto_result = goto_result
        self._goto_error = goto_error
        self._inner_text_error = inner_text_error
        #: Successive ``inner_text`` values simulating a late-rendering SPA;
        #: once exhausted the last value repeats.
        self._body_sequence = list(body_sequence) if body_sequence else None
        self.handler = None
        self.closed = False

    def route(self, pattern: str, handler) -> None:
        self.handler = handler

    def goto(self, url: str, **kwargs):
        if self._goto_error is not None:
            raise self._goto_error
        return self._goto_result

    def wait_for_timeout(self, ms: int) -> None:
        pass

    def inner_text(self, selector: str) -> str:
        if self._inner_text_error is not None:
            raise self._inner_text_error
        if self._body_sequence:
            if len(self._body_sequence) > 1:
                return self._body_sequence.pop(0)
            return self._body_sequence[0]
        return self._body

    def title(self) -> str:
        return self._title

    def close(self) -> None:
        self.closed = True


class _FakeBrowser:
    def __init__(self, page) -> None:
        self._page = page

    def new_page(self):
        return self._page


class _FakeChromium:
    def __init__(self, owner) -> None:
        self._owner = owner

    def launch(self, headless=True):
        self._owner.launch_kwargs = {"headless": headless}
        if self._owner._launch_error is not None:
            raise self._owner._launch_error
        if self._owner._browser is None:
            raise RuntimeError("no browser")
        return self._owner._browser


class _FakePlaywright:
    def __init__(
        self,
        browser=None,
        *,
        start_error: Exception | None = None,
        launch_error: Exception | None = None,
        stop_error: Exception | None = None,
    ) -> None:
        self._browser = browser
        self._start_error = start_error
        self._launch_error = launch_error
        self._stop_error = stop_error
        self.stopped = False
        self.launch_kwargs = None

    def start(self):
        if self._start_error is not None:
            raise self._start_error
        return self

    @property
    def chromium(self):
        return _FakeChromium(self)

    def stop(self) -> None:
        self.stopped = True
        if self._stop_error is not None:
            raise self._stop_error


def _install_fake_playwright(monkeypatch, pw) -> None:
    monkeypatch.setitem(
        sys.modules, "playwright.sync_api", SimpleNamespace(sync_playwright=lambda: pw)
    )
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery._PLAYWRIGHT_RUNTIME", None
    )


def test_enable_playwright_fallback_toggles_the_module_flag(monkeypatch) -> None:
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery._PLAYWRIGHT_FALLBACK_ENABLED", False
    )
    enable_playwright_fallback(True)
    assert (
        __import__(
            "backend.app.services.career_skills.job_discovery", fromlist=["x"]
        )._PLAYWRIGHT_FALLBACK_ENABLED
        is True
    )
    enable_playwright_fallback(False)
    assert (
        __import__(
            "backend.app.services.career_skills.job_discovery", fromlist=["x"]
        )._PLAYWRIGHT_FALLBACK_ENABLED
        is False
    )


def test_fetch_falls_back_to_rendered_text_when_requests_fails(monkeypatch) -> None:
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery._assert_public_url",
        _no_op_public_url,
    )
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery.requests.get",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            __import__("requests").ConnectionError("network down")
        ),
    )
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery._PLAYWRIGHT_FALLBACK_ENABLED", True
    )
    rendered = "渲染后的岗位正文\n工作职责：构建智能体。\n" + "任职要求补充说明。" * 16
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery._PLAYWRIGHT_FETCH_IMPL",
        lambda url: (rendered, "AI Agent 开发工程师"),
    )

    result = fetch_public_job_page(
        ToolContext(user_id="user-a", run_id="run-a"),
        FetchPublicJobPageInput(url="https://jobs.example/spa"),
    )

    assert result.visible_text == rendered
    assert result.title == "AI Agent 开发工程师"
    expected_hash = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
    assert result.content_hash == expected_hash
    assert result.artifact_id == f"observed:{expected_hash}"


def test_fetch_falls_back_on_empty_shell_and_login_wall(monkeypatch) -> None:
    for shell_text in ("<html></html>", "<html><body>登录</body></html>"):
        monkeypatch.setattr(
            "backend.app.services.career_skills.job_discovery._assert_public_url",
            _no_op_public_url,
        )
        monkeypatch.setattr(
            "backend.app.services.career_skills.job_discovery.requests.get",
            lambda *args, **kwargs: SimpleNamespace(
                text=shell_text,
                encoding="utf-8",
                apparent_encoding="utf-8",
                raise_for_status=lambda: None,
                is_redirect=False,
            ),
        )
        monkeypatch.setattr(
            "backend.app.services.career_skills.job_discovery._PLAYWRIGHT_FALLBACK_ENABLED", True
        )
        rendered = "SPA 渲染后的岗位列表…\n" + "后端开发工程师 3 年经验 招聘中。" * 12
        monkeypatch.setattr(
            "backend.app.services.career_skills.job_discovery._PLAYWRIGHT_FETCH_IMPL",
            lambda url: (rendered, "招聘"),
        )
        result = fetch_public_job_page(
            ToolContext(user_id="u", run_id="r"),
            FetchPublicJobPageInput(url="https://jobs.example/spa"),
        )
        assert result.visible_text == rendered


def test_fetch_rejects_short_shell_text_without_login_markers(monkeypatch) -> None:
    """A short SPA boot stub is not usable job evidence even without login markers."""
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery._assert_public_url",
        _no_op_public_url,
    )
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery.requests.get",
        lambda *args, **kwargs: SimpleNamespace(
            text="<html><title>腾讯招聘</title><body>首页</body></html>",
            encoding="utf-8", apparent_encoding="utf-8",
            raise_for_status=lambda: None,
            is_redirect=False,
        ),
    )
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery._PLAYWRIGHT_FALLBACK_ENABLED", False
    )
    with pytest.raises(PublicJobFetchError, match="public_page_content_insufficient"):
        fetch_public_job_page(
            ToolContext(user_id="u", run_id="r"),
            FetchPublicJobPageInput(url="https://careers.example.com/"),
        )


def test_fetch_falls_back_when_requests_yields_a_short_shell(monkeypatch) -> None:
    """A marker-free short shell still triggers the renderer; long rendered text wins."""
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery._assert_public_url",
        _no_op_public_url,
    )
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery.requests.get",
        lambda *args, **kwargs: SimpleNamespace(
            text="<html><title>搜索</title><body></body></html>",
            encoding="utf-8", apparent_encoding="utf-8",
            raise_for_status=lambda: None,
            is_redirect=False,
        ),
    )
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery._PLAYWRIGHT_FALLBACK_ENABLED", True
    )
    rendered = "搜索 | 腾讯招聘\n" + "混元3D AIGC产品经理 深圳 职位详情。" * 12
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery._PLAYWRIGHT_FETCH_IMPL",
        lambda url: (rendered, "搜索 | 腾讯招聘"),
    )
    result = fetch_public_job_page(
        ToolContext(user_id="u", run_id="r"),
        FetchPublicJobPageInput(url="https://careers.example.com/search.html"),
    )
    assert result.visible_text == rendered


def test_fetch_rejects_short_rendered_text_as_insufficient(monkeypatch) -> None:
    """'Not Found' stubs and verification walls surfaced by the renderer are not evidence."""
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery._assert_public_url",
        _no_op_public_url,
    )
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery.requests.get",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            __import__("requests").ConnectionError("network down")
        ),
    )
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery._PLAYWRIGHT_FALLBACK_ENABLED", True
    )
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery._PLAYWRIGHT_FETCH_IMPL",
        lambda url: ("Not Found", None),
    )
    with pytest.raises(PublicJobFetchError, match="public_page_content_insufficient"):
        fetch_public_job_page(
            ToolContext(user_id="u", run_id="r"),
            FetchPublicJobPageInput(url="https://jobs.example/position"),
        )


def test_fetch_never_falls_back_when_flag_is_off(monkeypatch) -> None:
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery._assert_public_url",
        _no_op_public_url,
    )
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery.requests.get",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            __import__("requests").ConnectionError("network down")
        ),
    )
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery._PLAYWRIGHT_FALLBACK_ENABLED", False
    )
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery._PLAYWRIGHT_FETCH_IMPL", _sentinel_fallback
    )
    with pytest.raises(PublicJobFetchError, match="public_fetch_failed"):
        fetch_public_job_page(
            ToolContext(user_id="u", run_id="r"),
            FetchPublicJobPageInput(url="https://jobs.example/spa"),
        )


def test_fetch_never_falls_back_on_unsafe_url(monkeypatch) -> None:
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery._PLAYWRIGHT_FALLBACK_ENABLED", True
    )
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery._PLAYWRIGHT_FETCH_IMPL", _sentinel_fallback
    )
    with pytest.raises(PublicJobFetchError, match="unsafe_public_url"):
        fetch_public_job_page(
            ToolContext(user_id="u", run_id="r"),
            FetchPublicJobPageInput(url="ftp://jobs.example/x"),
        )


def test_fetch_fallback_propagates_render_failure_and_blank_render(monkeypatch) -> None:
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery._assert_public_url",
        _no_op_public_url,
    )
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery.requests.get",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            __import__("requests").ConnectionError("network down")
        ),
    )
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery._PLAYWRIGHT_FALLBACK_ENABLED", True
    )
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery._PLAYWRIGHT_FETCH_IMPL",
        lambda url: (_ for _ in ()).throw(PublicJobFetchError("public_fetch_failed")),
    )
    with pytest.raises(PublicJobFetchError, match="public_fetch_failed"):
        fetch_public_job_page(
            ToolContext(user_id="u", run_id="r"),
            FetchPublicJobPageInput(url="https://jobs.example/spa"),
        )
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery._PLAYWRIGHT_FETCH_IMPL",
        lambda url: ("   ", None),
    )
    with pytest.raises(PublicJobFetchError, match="empty_public_page"):
        fetch_public_job_page(
            ToolContext(user_id="u", run_id="r"),
            FetchPublicJobPageInput(url="https://jobs.example/spa"),
        )


def test_render_with_playwright_uses_the_injected_seam(monkeypatch) -> None:
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery._PLAYWRIGHT_FETCH_IMPL",
        lambda url: ("seam text", "seam title"),
    )
    body, title = _render_with_playwright("https://jobs.example/x")
    assert (body, title) == ("seam text", "seam title")


def test_render_with_playwright_fails_closed_when_playwright_is_missing(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery._PLAYWRIGHT_FETCH_IMPL", None
    )
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery._PLAYWRIGHT_RUNTIME", None
    )
    with pytest.raises(PublicJobFetchError, match="public_fetch_failed"):
        _render_with_playwright("https://jobs.example/x")


def test_render_with_playwright_fails_closed_on_launch_failure(monkeypatch) -> None:
    pw = _FakePlaywright(None, start_error=RuntimeError("no browser binary"))
    _install_fake_playwright(monkeypatch, pw)
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery._PLAYWRIGHT_FETCH_IMPL", None
    )
    with pytest.raises(PublicJobFetchError, match="public_fetch_failed"):
        _render_with_playwright("https://jobs.example/x")

    pw2 = _FakePlaywright(None, launch_error=RuntimeError("launch boom"))
    _install_fake_playwright(monkeypatch, pw2)
    with pytest.raises(PublicJobFetchError, match="public_fetch_failed"):
        _render_with_playwright("https://jobs.example/x")
    assert pw2.stopped is True  # started playwright must be torn down

    pw3 = _FakePlaywright(
        None,
        launch_error=RuntimeError("launch boom"),
        stop_error=RuntimeError("stop boom"),
    )
    _install_fake_playwright(monkeypatch, pw3)
    with pytest.raises(PublicJobFetchError, match="public_fetch_failed"):
        _render_with_playwright("https://jobs.example/x")
    assert pw3.stopped is True  # a failing teardown never masks the fetch error


def test_render_with_playwright_fails_closed_on_goto_and_body_errors(monkeypatch) -> None:
    page = _FakePage(goto_result=None)
    _install_fake_playwright(monkeypatch, _FakePlaywright(_FakeBrowser(page)))
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery._PLAYWRIGHT_FETCH_IMPL", None
    )
    with pytest.raises(PublicJobFetchError, match="public_fetch_failed"):
        _render_with_playwright("https://jobs.example/x")
    assert page.closed is True

    page = _FakePage(goto_error=RuntimeError("nav failed"))
    _install_fake_playwright(monkeypatch, _FakePlaywright(_FakeBrowser(page)))
    with pytest.raises(PublicJobFetchError, match="public_fetch_failed"):
        _render_with_playwright("https://jobs.example/x")
    assert page.closed is True

    page = _FakePage(
        body="x", goto_result=SimpleNamespace(), inner_text_error=RuntimeError("boom")
    )
    _install_fake_playwright(monkeypatch, _FakePlaywright(_FakeBrowser(page)))
    with pytest.raises(PublicJobFetchError, match="public_fetch_failed"):
        _render_with_playwright("https://jobs.example/x")
    assert page.closed is True


def test_render_with_playwright_renders_and_guards_every_request(monkeypatch) -> None:
    page = _FakePage(
        body="渲染后的岗位正文\n工作职责：构建智能体。",
        title="AI Agent 开发工程师",
        goto_result=SimpleNamespace(),
    )
    pw = _FakePlaywright(_FakeBrowser(page))
    _install_fake_playwright(monkeypatch, pw)
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery._PLAYWRIGHT_FETCH_IMPL", None
    )

    body, title = _render_with_playwright("https://jobs.example/jobs/1")

    assert (body, title) == ("渲染后的岗位正文\n工作职责：构建智能体。", "AI Agent 开发工程师")
    assert page.closed is True
    assert pw.launch_kwargs == {"headless": True}
    assert page.handler is not None

    def fake_is_public(url: str) -> bool:
        if url == "https://broken.example/x":
            raise ValueError("dns")
        return url.startswith("https://jobs.example")

    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery._is_public_url", fake_is_public
    )
    public_route, private_route, error_route = _FakeRoute(), _FakeRoute(), _FakeRoute()
    page.handler(public_route, SimpleNamespace(url="https://jobs.example/app.js"))
    page.handler(private_route, SimpleNamespace(url="http://169.254.169.254/latest/meta-data"))
    page.handler(error_route, SimpleNamespace(url="https://broken.example/x"))
    assert public_route.continued and not public_route.aborted
    assert private_route.aborted and not private_route.continued
    assert error_route.aborted


def test_render_with_playwright_polls_until_a_late_spa_stops_growing(monkeypatch) -> None:
    """A portal that paints its job list long after domcontentloaded is polled
    until the body text stabilizes above the usable threshold, not returned as
    an empty shell."""
    long_text = "岗位职责：" + "AI Agent 开发工程师。" * 60
    page = _FakePage(
        body_sequence=["", "加载中…", long_text],
        title="蔚来校招",
        goto_result=SimpleNamespace(),
    )
    pw = _FakePlaywright(_FakeBrowser(page))
    _install_fake_playwright(monkeypatch, pw)
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery._PLAYWRIGHT_FETCH_IMPL", None
    )

    body, title = _render_with_playwright("https://nio.jobs.feishu.cn/campus/")

    assert body == long_text
    assert title == "蔚来校招"
    assert page.closed is True


def test_render_with_playwright_reuses_an_already_launched_runtime(monkeypatch) -> None:
    # A non-None runtime means the browser already exists: no second launch, and
    # a broken page object still degrades to a stable failure code.
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery._PLAYWRIGHT_RUNTIME",
        (object(), object()),
    )
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery._PLAYWRIGHT_FETCH_IMPL", None
    )
    with pytest.raises(PublicJobFetchError, match="public_fetch_failed"):
        _render_with_playwright("https://jobs.example/x")


def test_is_public_url_rejects_schemes_credentials_private_and_unresolvable(monkeypatch) -> None:
    assert _is_public_url("ftp://jobs.example/x") is False
    assert _is_public_url("https://user:pass@jobs.example/x") is False
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery.socket.getaddrinfo",
        lambda *a, **k: (_ for _ in ()).throw(OSError("dns")),
    )
    assert _is_public_url("https://jobs.example/x") is False
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery.socket.getaddrinfo",
        lambda *a, **k: [(None, None, None, None, ("10.0.0.1", 0))],
    )
    assert _is_public_url("https://jobs.example/x") is False
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery.socket.getaddrinfo",
        lambda *a, **k: [(None, None, None, None, ("1.2.3.4", 0))],
    )
    assert _is_public_url("https://jobs.example/x") is True


def test_fetch_batch_value_none_without_error_is_skipped(monkeypatch) -> None:
    """A work item that returns neither a value nor an error is dropped."""
    from backend.app.services.job_discovery.tools.batch_progress import BatchResult
    import backend.app.services.career_skills.job_discovery as jd_module

    monkeypatch.setattr(
        jd_module,
        "run_parallel_with_progress",
        lambda *a, **k: [BatchResult(index=0, item="https://jobs.example/x", value=None)],
    )
    result = jd_module.fetch_public_job_pages(
        ToolContext(user_id="u", run_id="r"),
        FetchPublicJobPagesInput(urls=["https://jobs.example/x"]),
    )
    assert result.pages == []
    assert result.failures == []
