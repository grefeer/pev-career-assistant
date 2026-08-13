"""Round-1 candidate A: channel-reliability fixes for job_discovery.

Covers the four deterministic tool-layer mechanisms:
- A1: bounded relaunch-once in ``_render_with_playwright`` (dead shared
  browser must not poison every later render).
- A2: requests-path HTML link collection + card-list expansion (RC-B).
- A3: head-positioned JD-marker gate (RC-C).
- A4: one transient retry in ``_fetch_validated``.
"""

from __future__ import annotations

import sys
import threading
from types import SimpleNamespace

import pytest

from backend.app.services.career_skills import job_discovery as jd
from backend.app.services.career_skills.job_discovery import (
    FetchPublicJobPageOutput,
    PublicJobFetchError,
    _fetch_one_with_expansion,
    _fetch_public_page_requests,
    _fetch_public_page_requests_with_html,
    _fetch_validated,
    _render_with_playwright,
    _expand_from_list_links,
)
from backend.app.services.agent_runtime.tool_context import ToolContext


# --------------------------------------------------------------------------
# Minimal playwright fakes (mirror the helpers in test_pev_job_discovery_skill)
# --------------------------------------------------------------------------


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
        goto_result: object | None = None,
        goto_error: Exception | None = None,
        goto_block: threading.Event | None = None,
    ) -> None:
        self._body = body
        self._title = title
        self._goto_result = goto_result
        self._goto_error = goto_error
        self._goto_block = goto_block
        self.handler = None
        self.closed = False

    def route(self, pattern: str, handler) -> None:
        self.handler = handler

    def goto(self, url: str, **kwargs):
        if self._goto_block is not None:
            self._goto_block.wait()  # a wedged driver never returns
        if self._goto_error is not None:
            raise self._goto_error
        return self._goto_result

    def wait_for_timeout(self, ms: int) -> None:
        pass

    def inner_text(self, selector: str) -> str:
        return self._body

    def title(self) -> str:
        return self._title

    def close(self) -> None:
        self.closed = True


class _FakeBrowser:
    def __init__(self, page, *, close_error: Exception | None = None) -> None:
        self._page = page
        self._close_error = close_error
        self.closed = False

    def new_page(self):
        return self._page

    def close(self) -> None:
        self.closed = True
        if self._close_error is not None:
            raise self._close_error


class _FakeChromium:
    def __init__(self, owner) -> None:
        self._owner = owner

    def launch(self, headless=True):
        self._owner.launch_kwargs = {"headless": headless}
        self._owner.launch_count += 1
        if not self._owner._browsers:
            raise RuntimeError("no browser")
        return self._owner._browsers.pop(0)


class _FakePlaywright:
    def __init__(
        self,
        browser=None,
        *,
        start_error: Exception | None = None,
        stop_error: Exception | None = None,
    ) -> None:
        self._browsers = [browser] if browser is not None else []
        self._start_error = start_error
        self._stop_error = stop_error
        self.stopped = False
        self.launch_kwargs = None
        self.launch_count = 0

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
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery._PLAYWRIGHT_FETCH_IMPL", None
    )


# --------------------------------------------------------------------------
# A1: bounded relaunch-once in _render_with_playwright
# --------------------------------------------------------------------------


def test_render_with_playwright_relaunches_once_after_generic_crash(monkeypatch) -> None:
    """A generic mid-render failure (CDP disconnect / OOM) tears down the dead
    runtime and retries once; a live relaunched browser serves the render."""
    good_page = _FakePage(
        body="岗位职责：渲染成功的内容。", title="岗位", goto_result=SimpleNamespace()
    )
    dead_page = _FakePage(goto_error=RuntimeError("target closed (cdp disconnect)"))
    pw = _FakePlaywright(_FakeBrowser(dead_page))
    pw._browsers.append(_FakeBrowser(good_page))
    _install_fake_playwright(monkeypatch, pw)

    body, title = _render_with_playwright("https://jobs.example.com/job/1")

    assert (body, title) == ("岗位职责：渲染成功的内容。", "岗位")
    assert pw.launch_count == 2  # original launch + exactly one relaunch
    assert pw.stopped is True  # the dead runtime was torn down
    assert good_page.closed is True


def test_render_with_playwright_second_generic_failure_is_final(monkeypatch) -> None:
    """When the relaunched browser also fails, the stable failure code is
    raised -- no unbounded relaunch loop."""
    pw = _FakePlaywright(
        _FakeBrowser(_FakePage(goto_error=RuntimeError("boom"))),
        stop_error=RuntimeError("stop boom"),
    )
    _install_fake_playwright(monkeypatch, pw)

    with pytest.raises(PublicJobFetchError, match="public_fetch_failed"):
        _render_with_playwright("https://jobs.example.com/job/1")

    assert pw.launch_count == 2
    assert pw.stopped is True


def test_render_with_playwright_never_retries_public_job_fetch_error(monkeypatch) -> None:
    """A raised PublicJobFetchError (deliberate rejection path) is final."""
    pw = _FakePlaywright(_FakeBrowser(_FakePage(goto_result=None)))
    _install_fake_playwright(monkeypatch, pw)

    with pytest.raises(PublicJobFetchError, match="public_fetch_failed"):
        _render_with_playwright("https://jobs.example.com/job/1")

    assert pw.launch_count == 1  # no relaunch for a PublicJobFetchError
    assert pw.stopped is False


def test_render_with_playwright_teardown_failures_do_not_block_relaunch(monkeypatch) -> None:
    """Even when closing the dead browser or stopping playwright raises, the
    single relaunch still happens (never masks the fetch outcome)."""
    dead = _FakeBrowser(
        _FakePage(goto_error=RuntimeError("boom")), close_error=RuntimeError("close boom")
    )
    good = _FakeBrowser(
        _FakePage(body="岗位职责：ok", title="t", goto_result=SimpleNamespace())
    )
    pw = _FakePlaywright(dead, stop_error=RuntimeError("stop boom"))
    pw._browsers.append(good)
    _install_fake_playwright(monkeypatch, pw)

    body, _title = _render_with_playwright("https://jobs.example.com/job/1")

    assert body == "岗位职责：ok"
    assert pw.launch_count == 2
    assert dead.closed is True  # close() was attempted even though it raised


def test_render_with_playwright_reuses_live_runtime_without_launch(monkeypatch) -> None:
    """A healthy cached runtime is reused as-is: no relaunch on success."""
    good = _FakeBrowser(
        _FakePage(body="岗位职责：ok", title="t", goto_result=SimpleNamespace())
    )
    pw = _FakePlaywright(good)
    _install_fake_playwright(monkeypatch, pw)
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery._PLAYWRIGHT_RUNTIME",
        (pw, good),
    )

    body, title = _render_with_playwright("https://jobs.example.com/job/1")

    assert (body, title) == ("岗位职责：ok", "t")
    assert pw.launch_count == 0


def test_render_with_playwright_watchdog_abandons_wedged_render(monkeypatch) -> None:
    """A render that never returns (dead CDP / driver spin) fails bounded.

    The call aborts at the watchdog deadline instead of hanging the whole
    eval process; the wedged runtime is orphaned (never torn down from a
    foreign thread -- that teardown call is itself the hang), so the next
    render starts from a fresh launch.
    """
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery._RENDER_TIMEOUT_S", 0.2
    )
    blocked = threading.Event()
    pw = _FakePlaywright(browser=_FakeBrowser(_FakePage(goto_block=blocked)))
    _install_fake_playwright(monkeypatch, pw)

    with pytest.raises(PublicJobFetchError) as exc_info:
        _render_with_playwright("https://jobs.example.com/job/1")

    assert exc_info.value.code == "public_fetch_failed"
    assert jd._PLAYWRIGHT_RUNTIME is None  # orphaned, not torn down
    assert pw.stopped is False  # teardown never ran on the foreign thread
    blocked.set()  # release the daemon worker so the suite exits cleanly


# --------------------------------------------------------------------------
# A2: requests-path HTML link collection + expansion
# --------------------------------------------------------------------------


def test_html_link_collector_mirrors_rendered_link_filters(monkeypatch) -> None:
    """Raw-HTML anchors pass the same gates as the rendered-DOM collector:
    public URL, same host, job-shaped path, urljoin resolution, dedupe, and
    no anchors inside script/style blocks."""
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery._is_public_url",
        # no trailing slash on purpose: the cross-host lookalike below passes
        # the public check so the same-host filter must reject it
        lambda url: url.startswith("https://jobs.example.com"),
    )
    html = (
        "<html><head><title>招聘列表</title></head><body>"
        '<h1>热门职位</h1>'
        '<a href="/position/1">职位A</a>'
        '<a href="https://jobs.example.com/job/2">职位B</a>'
        '<a href="/about">关于我们</a>'
        '<a href="https://jobs.example.com.evil.example/job/3">跨站仿冒</a>'
        '<a href="javascript:void(0)">占位</a>'
        '<a href="http://192.168.1.5/job/4">内网</a>'
        '<a href="/position/1">重复</a>'
        "<a>无href</a>"
        '<a href=""></a>'
        "<script>var x = '<a href=\"/position/9\">脚本内</a>';</script>"
        "</body></html>"
    )
    collector = jd._HtmlLinkCollector("https://jobs.example.com/careers")
    collector.feed(html)
    assert collector.links == [
        "https://jobs.example.com/position/1",
        "https://jobs.example.com/job/2",
    ]


def _card_list_html(urls: list[str]) -> str:
    anchors = "".join(f'<a href="{url}">{i}号职位</a>' for i, url in enumerate(urls))
    return (
        "<html><head><title>校招职位列表</title></head><body>"
        + "卡片列表 " * 60
        + anchors
        + "</body></html>"
    )


def _jd_page_html(title: str) -> str:
    return (
        "<html><head><title>岗位JD</title></head><body>"
        f"{title} 岗位职责：负责研发交付；岗位要求：3 年经验。"
        + "补充说明。" * 40
        + "</body></html>"
    )


def test_fetch_one_with_expansion_expands_card_list_on_requests_path(monkeypatch) -> None:
    """A server-rendered card list (liepin-style) now expands via requests:
    raw HTML anchors are collected and detail pages deep-fetched without a
    browser (RC-B)."""
    list_url = "https://www.liepin.com/careers"
    detail_urls = [
        "https://www.liepin.com/position/1",
        "https://www.liepin.com/position/2",
    ]
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery._is_public_url",
        lambda url: url.startswith("https://www.liepin.com/"),
    )

    def fake_get(url, *args, **kwargs):  # noqa: ANN001
        if url == list_url:
            html = _card_list_html(detail_urls)
        else:
            html = _jd_page_html(url.split("/")[-1])
        return SimpleNamespace(
            text=html,
            encoding="utf-8",
            apparent_encoding="utf-8",
            raise_for_status=lambda: None,
            is_redirect=False,
        )

    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery.requests.get", fake_get
    )

    pages = _fetch_one_with_expansion(
        ToolContext(user_id="u", run_id="r"), list_url
    )

    assert [page.source_url for page in pages] == [list_url, *detail_urls]
    assert pages[0].title == "校招职位列表"
    assert all("岗位职责" in page.visible_text for page in pages[1:])


def test_fetch_one_with_expansion_requests_path_skips_jd_pages(monkeypatch) -> None:
    """A detail page fetched via requests is terminal evidence: even with
    job-shaped links present, a JD marker in the body head blocks expansion."""
    list_url = "https://www.liepin.com/position/1"
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery._is_public_url",
        lambda url: url.startswith("https://www.liepin.com/"),
    )
    html = (
        "<html><body>"
        + _jd_page_html("前端工程师")
        + '<a href="https://www.liepin.com/position/2">相关职位</a>'
        + '<a href="https://www.liepin.com/position/3">相关职位</a>'
        + "</body></html>"
    )
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery.requests.get",
        lambda *args, **kwargs: SimpleNamespace(
            text=html,
            encoding="utf-8",
            apparent_encoding="utf-8",
            raise_for_status=lambda: None,
            is_redirect=False,
        ),
    )

    pages = _fetch_one_with_expansion(
        ToolContext(user_id="u", run_id="r"), list_url
    )

    assert [page.source_url for page in pages] == [list_url]


def test_list_expansion_prioritizes_detail_routes_over_navigation(monkeypatch) -> None:
    """Navigation links must not consume the bounded detail-fetch budget."""
    list_url = "https://career.hebut.edu.cn/correcruit/index.html?p=1"
    navigation_urls = [
        "https://career.hebut.edu.cn/correcruit/index.html",
        "https://career.hebut.edu.cn/recruitment/index.html",
        "https://career.hebut.edu.cn/basicrecruit/index.html",
    ]
    detail_urls = [
        "https://career.hebut.edu.cn/correcruit/content/id/79131.html",
        "https://career.hebut.edu.cn/correcruit/content/id/79130.html",
        "https://career.hebut.edu.cn/correcruit/content/id/79121.html",
    ]
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery.requests.get",
        lambda url, *args, **kwargs: SimpleNamespace(
            text=_jd_page_html(url.rsplit("/", 1)[-1]),
            encoding="utf-8",
            apparent_encoding="utf-8",
            raise_for_status=lambda: None,
            is_redirect=False,
        ),
    )

    pages = _expand_from_list_links(
        list_url,
        [*navigation_urls, *detail_urls],
        "卡片列表 " * 60,
    )

    assert [page.source_url for page in pages[: len(detail_urls)]] == detail_urls


def test_iguopin_list_detail_probe_reports_anonymous_access_denial(monkeypatch) -> None:
    page = FetchPublicJobPageOutput(
        artifact_id="list-1",
        source_url="https://www.iguopin.com/job/list?keyword=Java",
        title="招聘信息-国聘",
        visible_text="职位卡片 " * 80,
        content_hash="a" * 64,
    )
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery._assert_public_url",
        lambda _url: None,
    )
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery.requests.post",
        lambda *args, **kwargs: SimpleNamespace(
            status_code=200,
            json=lambda: {"data": [{"job_id": "job-1"}]},
        ),
    )
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery.requests.get",
        lambda *args, **kwargs: SimpleNamespace(
            status_code=200,
            json=lambda: {"code": 403},
        ),
    )

    error = jd._probe_iguopin_detail_access(page.source_url, page)

    assert error is not None
    assert error.code == "access_denied"


def test_hebut_detail_fetch_expands_recent_public_campus_indexes(monkeypatch) -> None:
    """A supplied campus JD can expose sibling recent postings safely."""
    detail_url = "https://career.hebut.edu.cn/correcruit/content/id/78016.html"
    index_urls = {
        "https://career.hebut.edu.cn/correcruit/index.html?p=1",
        "https://career.hebut.edu.cn/correcruit/index.html?p=2",
    }
    sibling_urls = [
        "https://career.hebut.edu.cn/correcruit/content/id/79111.html",
        "https://career.hebut.edu.cn/correcruit/content/id/79113.html",
    ]
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery._assert_public_url",
        lambda _url: None,
    )
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery._is_public_url",
        lambda url: url.startswith("https://career.hebut.edu.cn/"),
    )

    def fake_get(url, *args, **kwargs):  # noqa: ANN001
        if url == detail_url:
            html = _jd_page_html("中粮集团校园招聘")
        elif url in index_urls:
            links = "".join(f'<a href="{item}">岗位</a>' for item in sibling_urls)
            html = "<html><body>校园职位列表 " + ("卡片 " * 60) + links + "</body></html>"
        else:
            html = _jd_page_html(url.rsplit("/", 1)[-1])
        return SimpleNamespace(
            text=html,
            encoding="utf-8",
            apparent_encoding="utf-8",
            raise_for_status=lambda: None,
            is_redirect=False,
        )

    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery.requests.get", fake_get
    )
    result = jd.fetch_public_job_pages(
        ToolContext(user_id="u", run_id="r"),
        jd.FetchPublicJobPagesInput(urls=[detail_url]),
    )

    sources = [page.source_url for page in result.pages]
    assert sources[0] == detail_url
    assert sibling_urls[0] in sources
    assert sibling_urls[1] in sources


def test_fetch_public_page_requests_with_html_keeps_backward_compat(monkeypatch) -> None:
    """The with-html variant returns (evidence, raw_html); the single-value
    wrapper keeps working for existing callers."""
    html = "<html><title>岗位A</title><body>" + "岗位职责内容。" * 40 + "</body></html>"
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery.requests.get",
        lambda *args, **kwargs: SimpleNamespace(
            text=html,
            encoding="utf-8",
            apparent_encoding="utf-8",
            raise_for_status=lambda: None,
            is_redirect=False,
        ),
    )

    page, raw = _fetch_public_page_requests_with_html("https://jobs.example.com/job/1")
    wrapper_page = _fetch_public_page_requests("https://jobs.example.com/job/1")

    assert raw == html
    assert page.source_url == "https://jobs.example.com/job/1"
    assert wrapper_page.artifact_id == page.artifact_id
    assert wrapper_page.content_hash == page.content_hash


# --------------------------------------------------------------------------
# A3: head-positioned JD-marker gate
# --------------------------------------------------------------------------


def test_expand_from_list_links_ignores_footer_marker_beyond_head_scan(monkeypatch) -> None:
    """A JD marker living only in footer SEO text (past the 2000-char head
    scan) no longer blocks a card shell from expanding (RC-C)."""
    body = "卡片列表 " * 450 + "岗位职责：这是页面底部的 SEO 文案" + "补充说明。" * 30
    detail_urls = [
        "https://jobs.example.com/position/1",
        "https://jobs.example.com/position/2",
    ]
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery.requests.get",
        lambda url, *args, **kwargs: SimpleNamespace(
            text=_jd_page_html(url.split("/")[-1]),
            encoding="utf-8",
            apparent_encoding="utf-8",
            raise_for_status=lambda: None,
            is_redirect=False,
        ),
    )

    pages = _expand_from_list_links(
        "https://jobs.example.com/careers", detail_urls, body
    )

    assert [page.source_url for page in pages] == detail_urls


def test_expand_from_list_links_blocks_when_marker_in_head_scan(monkeypatch) -> None:
    """A marker within the first 2000 chars still means the page is itself a
    JD page and must not expand."""
    body = "岗位职责：负责交付。" + "补充说明。" * 40
    assert (
        _expand_from_list_links(
            "https://jobs.example.com/careers",
            ["https://jobs.example.com/position/1", "https://jobs.example.com/position/2"],
            body,
        )
        == []
    )


# --------------------------------------------------------------------------
# A4: one transient retry in _fetch_validated
# --------------------------------------------------------------------------


def test_fetch_validated_retries_transient_failure_once_then_succeeds(monkeypatch) -> None:
    """A single timeout/connection failure is retried once and the response
    is returned on the second attempt."""
    import requests

    calls = {"count": 0}

    def fake_get(url, *args, **kwargs):  # noqa: ANN001
        calls["count"] += 1
        if calls["count"] == 1:
            raise requests.ConnectionError("connection reset by peer")
        return SimpleNamespace(
            text="<html>ok</html>",
            encoding="utf-8",
            apparent_encoding="utf-8",
            raise_for_status=lambda: None,
            is_redirect=False,
        )

    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery.requests.get", fake_get
    )

    response = _fetch_validated("https://jobs.example.com/x")

    assert response.text == "<html>ok</html>"
    assert calls["count"] == 2


def test_fetch_validated_propagates_after_two_transient_failures(monkeypatch) -> None:
    """Two consecutive transient failures propagate the original transport
    error -- no more than one retry."""
    import requests

    calls = {"count": 0}

    def fake_get(url, *args, **kwargs):  # noqa: ANN001
        calls["count"] += 1
        raise requests.Timeout("timed out")

    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery.requests.get", fake_get
    )

    with pytest.raises(requests.Timeout):
        _fetch_validated("https://jobs.example.com/x")
    assert calls["count"] == 2


def test_fetch_validated_does_not_retry_redirect_loop_rejection(monkeypatch) -> None:
    """A PublicJobFetchError (unsafe_public_url / blocked) is never retried:
    the redirect walk runs exactly once."""
    calls = {"count": 0}

    def fake_get(url, *args, **kwargs):  # noqa: ANN001
        calls["count"] += 1
        return SimpleNamespace(
            text="",
            encoding="utf-8",
            apparent_encoding="utf-8",
            raise_for_status=lambda: None,
            is_redirect=True,
            status_code=302,
            headers={"Location": "https://jobs.example.com/loop"},
        )

    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery.requests.get", fake_get
    )
    monkeypatch.setattr(
        "backend.app.services.career_skills.job_discovery._assert_public_url",
        lambda _url: None,
    )

    with pytest.raises(PublicJobFetchError, match="unsafe_public_url"):
        _fetch_validated("https://jobs.example.com/start")
    assert calls["count"] == jd._MAX_PUBLIC_REDIRECTS + 1
