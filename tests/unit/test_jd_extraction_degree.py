"""B3 (FindJobs port): degree whitelist + priority structured extraction.

Covers the new ``NormalizedJobCandidate.min_degree`` / ``priority`` fields
and the deterministic regex extraction behind them (see
docs/findjobs-optimization-plan.zh-CN.md §5.3).  All fixtures are
deterministic Chinese/English JD text; no LLM/DB/network dependency.
"""

from __future__ import annotations

from backend.app.services.job_discovery.tools.jd_extraction import (
    _extract_min_degree,
    _extract_priority,
    extract_jd_candidates,
)

#: Every whitelist keyword, one fixture each (B3-1: 白名单逐词 fixture).
_DEGREE_FIXTURES = [
    ("岗位要求：本科及以上学历", "本科"),
    ("任职要求：硕士及以上，3 年经验", "硕士"),
    ("博士学历优先", "博士"),
    ("大专或以上", "大专"),
    ("学历不限，欢迎投递", "不限"),
    ("不限学历，接受转行", "不限"),
]


def test_degree_whitelist_each_keyword() -> None:
    """B3-1: every whitelist keyword normalizes to its tier."""
    for text, expected in _DEGREE_FIXTURES:
        assert _extract_min_degree(text) == expected, text


def test_degree_most_specific_first() -> None:
    """``学历不限`` beats the bare ``不限`` and degree tier wins inside prose."""
    assert _extract_min_degree("本科学历不限专业") == "不限"
    assert _extract_min_degree("博士及以上学历，同时要求硕士期间有论文") == "博士"


def test_degree_missing_returns_none() -> None:
    """No degree mention -> None, never fabricated (B3-2 safe default)."""
    assert _extract_min_degree("负责后端开发，熟悉 Python") is None
    assert _extract_min_degree("") is None


def test_priority_must_preferred_unknown() -> None:
    """B3: must/preferred/unknown three states."""
    assert _extract_priority("必须具备本科以上学历") == "must"
    assert _extract_priority("具备 Java 经验优先") == "preferred"
    assert _extract_priority("加分项：有开源项目") == "preferred"
    assert _extract_priority("负责系统设计") == "unknown"
    assert _extract_priority("") == "unknown"


def test_priority_must_beats_preferred() -> None:
    """Both signals in one JD -> must wins."""
    assert _extract_priority("必须具备本科以上学历，硕士优先") == "must"


def test_extract_candidate_carries_new_fields() -> None:
    """``extract_jd_candidates`` propagates min_degree + priority to results."""
    text = (
        "职位名称：算法工程师\n"
        "任职要求：必须具备本科及以上学历，熟悉 Python；精通机器学习优先。"
    )
    candidates = extract_jd_candidates(text, "https://example.com/job/1")
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.min_degree == "本科"
    assert candidate.priority == "must"  # 必须 beats 优先 within the same JD


def test_extract_candidate_defaults_when_absent() -> None:
    """B3-2: a JD without degree/priority text keeps the safe defaults."""
    text = "职位名称：客服专员\n工作内容：接听电话。"
    candidates = extract_jd_candidates(text, "https://example.com/job/2")
    assert len(candidates) == 1
    assert candidates[0].min_degree is None
    assert candidates[0].priority == "unknown"


def test_unstructured_fallback_also_extracts_degree() -> None:
    """The last-resort unstructured path fills the same new fields."""
    text = (
        "我们是一家AI公司，正在招聘机器学习工程师。"
        "任职要求：硕士及以上学历，有推荐系统经验优先。"
        "简历投递至 jobs@example.com"
    )
    candidates = extract_jd_candidates(text, "https://example.com/job/3")
    assert candidates
    assert candidates[0].min_degree == "硕士"
    assert candidates[0].priority == "preferred"
