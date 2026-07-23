"""Unit tests for the title-only extraction false-positive filter.

Covers the three generic filters in ``_is_plausible_job_title``:
  - structural-separator (pipe) titles (banners / news headlines)
  - bare category words (title is exactly a suffix, e.g. ``管培生``)
  - sidebar category tabs that repeat across paginated list pages

These reproduce the pdd over-extraction (25 -> 22) without hardcoding any
site / count / page: the filters are title- and page-structure based.
"""
# ruff: noqa: E402
from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from backend.app.services.job_discovery.deepagents_runner import (
    _extract_and_verify_candidates_from_evidence,
    _is_plausible_job_title,
    _page_text_normalized_line_sets,
)
from backend.app.services.job_discovery.schemas import NormalizedJobCandidate


def _cand(title: str, *, body: bool = False) -> NormalizedJobCandidate:
    return NormalizedJobCandidate(
        title=title,
        apply_url="https://example.com",
        responsibilities="做事情" if body else "",
        requirements="会技能" if body else "",
        locations=[],
        recruitment_types=[],
        industries=[],
        evidence_refs=[],
    )


class TestPipeAndBareFilters:
    def test_rejects_pipe_banner(self):
        assert not _is_plausible_job_title(
            _cand("拼多多集团-PDD | 2027届校招提前批启动！这些岗"), [])

    def test_rejects_ascii_comma_fragment(self):
        # A leading/middle ASCII comma signals a comma-separated list fragment
        # scraped from a rendered tag/filter row (e.g. ``", 实习生"``), not a
        # single job title. Real Chinese campus titles use no ASCII commas.
        assert not _is_plausible_job_title(_cand(", 实习生"), [])
        assert not _is_plausible_job_title(_cand("应届, 实习生"), [])

    def test_rejects_bare_generic_category(self):
        # Bare GENERIC category words (经理/运营) with no qualifier -> a
        # section header, not a job listing.
        assert not _is_plausible_job_title(_cand("经理"), [])
        assert not _is_plausible_job_title(_cand("运营"), [])
        assert not _is_plausible_job_title(_cand("主管"), [])

    def test_keeps_bare_specific_role_title(self):
        # Bare SPECIFIC role titles are legitimate standalone jobs - e.g.
        # deeproute lists 【2027秋招】产品经理, whose leading tag strips to the
        # bare title 产品经理. These must NOT be dropped as "bare category
        # words"; a genuine sidebar tab is still caught by the repeats filter
        # (rule 3) on multi-page captures.
        assert _is_plausible_job_title(_cand("产品经理"), [])
        assert _is_plausible_job_title(_cand("项目经理"), [])
        assert _is_plausible_job_title(_cand("工程师"), [])
        assert _is_plausible_job_title(_cand("管培生"), [])

    def test_keeps_qualified_titles(self):
        assert _is_plausible_job_title(_cand("产品管培生"), [])
        assert _is_plausible_job_title(_cand("服务端研发工程师"), [])
        assert _is_plausible_job_title(_cand("区域业务管培生"), [])  # one page only

    def test_keeps_full_jd_even_if_title_on_many_pages(self):
        # A full-JD candidate has a body that proves it is a real listing;
        # cross-linking from several list pages must not drop it.
        line_sets = [{"产品管培生"}, {"产品管培生"}, {"产品管培生"}]
        assert _is_plausible_job_title(_cand("产品管培生", body=True), line_sets)


class TestSidebarRepeatsFilter:
    def _sidebar_sets(self):
        # Two paginated list pages whose sidebar re-renders the same category
        # tabs (管培生, 区域业务管培生) on every page, plus distinct real jobs.
        page1 = (
            "技术专场\n管培生\n区域业务管培生\n其他项目\n"
            "内容审核专员（法律专项）\n职能\n河北雄安新区\n2026届\n"
            "产品管培生（上海）\n运营\n上海\n2027届\n"
        )
        page2 = (
            "技术专场\n管培生\n区域业务管培生\n其他项目\n"
            "数据分析师\n技术\n上海\n2027届\n"
        )
        return _page_text_normalized_line_sets([
            {"evidence_type": "page_text", "text_excerpt": page1},
            {"evidence_type": "page_text", "text_excerpt": page2},
        ])

    def test_rejects_sidebar_tab_repeating_across_pages(self):
        sets = self._sidebar_sets()
        assert not _is_plausible_job_title(_cand("管培生"), sets)
        assert not _is_plausible_job_title(_cand("区域业务管培生"), sets)

    def test_keeps_real_job_on_one_page(self):
        sets = self._sidebar_sets()
        assert _is_plausible_job_title(_cand("内容审核专员"), sets)
        assert _is_plausible_job_title(_cand("产品管培生"), sets)
        assert _is_plausible_job_title(_cand("数据分析师"), sets)

    def test_no_filter_when_only_one_page_text(self):
        # With a single page_text capture the sidebar signal is unreliable;
        # the repeats filter is skipped (only pipe + bare-generic filters apply).
        sets = [{"运营"}]
        # 运营 is a bare GENERIC category word -> still rejected.
        assert not _is_plausible_job_title(_cand("运营"), sets)
        # 产品经理 is a bare SPECIFIC role title -> kept (legitimate standalone
        # job, e.g. deeproute's 【2027秋招】产品经理 after prefix strip).
        assert _is_plausible_job_title(_cand("产品经理"), sets)
        # 区域业务管培生 is qualified and on one page -> kept.
        assert _is_plausible_job_title(_cand("区域业务管培生"), sets)


class TestIntegrationExtraction:
    def test_pdd_like_page_drops_false_positives(self):
        """A pdd-style list page (sidebar tabs + jobs) yields only real jobs."""
        page1 = (
            "校园招聘\n2026届常规批次\n技术专场\n管培生\n区域业务管培生\n"
            "其他项目\n人才专项\n云弧计划\n职位类别\n设计\n职能\n运营\n"
            "内容审核专员（法律专项）\n职能\n河北雄安新区\n2026届\n2026-07-19\n"
            "用户体验运营管培（多语种优势-上海）\n运营\n上海\n2027届\n"
            "服务端研发工程师\n技术\n上海\n2027届\n"
            "算法工程师\n技术\n上海\n2027届\n"
            "拼多多集团-PDD | 2027届校招提前批启动！这些岗位现在就能投\n"
        )
        page2 = (
            "技术专场\n管培生\n区域业务管培生\n其他项目\n"
            "产品管培生（上海）\n运营\n上海\n2027届\n"
            "市场管培生（上海）\n运营\n上海\n2027届\n"
        )
        evidence = [
            {"evidence_type": "page_text", "text_excerpt": page1,
             "url": "https://x.io/p1", "content_hash": "h1"},
            {"evidence_type": "page_text", "text_excerpt": page2,
             "url": "https://x.io/p2", "content_hash": "h2"},
        ]
        cands, _ = _extract_and_verify_candidates_from_evidence(evidence, "https://x.io")
        titles = {c.get("title") if isinstance(c, dict) else getattr(c, "title", None)
                  for c in cands}
        # False positives gone.
        assert "管培生" not in titles
        assert "区域业务管培生" not in titles
        assert not any(t and "|" in t for t in titles)
        # Real jobs kept.
        assert "内容审核专员" in titles
        assert "服务端研发工程师" in titles
        assert "算法工程师" in titles
        assert "产品管培生" in titles
        assert "市场管培生" in titles


class TestTrailingBracketAndRoleWordSuffixes:
    """Regression tests for the trailing 【...】 tag strip and the 运营/制作
    role-word suffixes - the two fixes that closed pdd's 18->22 count gap
    (titles whose bare form ends in a bracket tag or a role word not in the
    original suffix list). No site/count/page is hardcoded.
    """

    def test_trailing_bracket_tag_is_stripped(self):
        """A title with a trailing 【...】 cohort/campaign tag is detected via
        its bare-form suffix (``...工程师【2027届云弧计划】`` -> 工程师)."""
        # Two paginated pages so the repeating sidebar tabs (管培生) are also
        # dropped by the repeats filter (rule 3); the real jobs appear once.
        page1 = (
            "管培生\n区域业务管培生\n"
            "AI Infra研发工程师【2027届云弧计划】\n人才专项\n技术\n上海\n2027届\n"
        )
        page2 = (
            "管培生\n区域业务管培生\n"
            "大模型算法工程师【2027届云弧计划】\n人才专项\n技术\n上海\n2027届\n"
        )
        evidence = [
            {"evidence_type": "page_text", "text_excerpt": page1,
             "url": "https://x.io/p1", "content_hash": "h1"},
            {"evidence_type": "page_text", "text_excerpt": page2,
             "url": "https://x.io/p2", "content_hash": "h2"},
        ]
        cands, _ = _extract_and_verify_candidates_from_evidence(evidence, "https://x.io")
        titles = {c.get("title") if isinstance(c, dict) else getattr(c, "title", None)
                  for c in cands}
        # The bracket tag is stripped, the bare title is kept.
        assert "AI Infra研发工程师" in titles
        assert "大模型算法工程师" in titles
        # The full tag-bearing form must NOT also appear (would be a dup).
        assert not any(t and "【" in t for t in titles)

    def test_role_word_suffixes_yunying_and_zhizuo(self):
        """Qualified titles ending in 运营 / 制作 are real jobs and must be
        extracted, while the bare sidebar word 运营 stays filtered."""
        page = (
            "职位类别\n设计\n职能\n运营\n"  # bare 运营 sidebar tab
            "商家运营（多语种优势-上海）\n语言\n上海\n2027届\n"
            "视频创意制作（上海）\n视觉类\n上海\n2027届\n"
        )
        evidence = [
            {"evidence_type": "page_text", "text_excerpt": page,
             "url": "https://x.io/p1", "content_hash": "h1"},
        ]
        cands, _ = _extract_and_verify_candidates_from_evidence(evidence, "https://x.io")
        titles = {c.get("title") if isinstance(c, dict) else getattr(c, "title", None)
                  for c in cands}
        # Qualified role-word titles are kept.
        assert "商家运营" in titles
        assert "视频创意制作" in titles
        # Bare sidebar 运营 is NOT a job.
        assert "运营" not in titles

    def test_deeproute_leading_prefix_bare_specific_role(self):
        """Regression: deeproute lists jobs with a LEADING 【2027秋招】 prefix.
        After the prefix is stripped, titles like 产品经理 / 项目经理 are BARE
        specific role titles - they must NOT be dropped by the bare-category
        filter (which now only rejects bare GENERIC words like 运营/经理)."""
        page = (
            "校园招聘\n在招职位\n21 结果\n"  # 21 结果 starts with a digit -> dropped
            "【2027秋招】产品经理\n发布于 2026-07-06\n上海市\n"
            "【2027秋招】项目经理\n发布于 2026-07-06\n上海市\n"
            "【2027秋招】数据挖掘工程师\n发布于 2026-07-06\n上海市\n"
        )
        evidence = [
            {"evidence_type": "page_text", "text_excerpt": page,
             "url": "https://x.io/p1", "content_hash": "h1"},
        ]
        cands, _ = _extract_and_verify_candidates_from_evidence(evidence, "https://x.io")
        titles = {c.get("title") if isinstance(c, dict) else getattr(c, "title", None)
                  for c in cands}
        # Leading 【...】 stripped; bare specific role titles survive.
        assert "产品经理" in titles
        assert "项目经理" in titles
        assert "数据挖掘工程师" in titles
        # The prefix-bearing form must not also appear (would be a dup).
        assert not any(t and "【" in t for t in titles)
