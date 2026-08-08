"""B1 (FindJobs port): job strength signals.

Covers ``tools/job_strength.py`` (see docs/findjobs-optimization-plan.zh-CN.md
§5.1): the five weighted signals (years / skill stack / degree / numbered
duties / bonus), tier mapping with base scores, verbatim evidence, the
serializable dict form, and the enrichment of ``NormalizedJobCandidate`` at
the extraction output.  All fixtures are deterministic text; no
LLM/DB/network dependency.
"""

from __future__ import annotations

from backend.app.services.job_discovery.tools.jd_extraction import (
    extract_jd_candidates,
)
from backend.app.services.job_discovery.tools.job_strength import (
    analyze_job_strength,
)


def test_years_signal_arabic_numerals() -> None:
    result = analyze_job_strength("任职要求：3年以上相关工作经验，负责系统设计。")
    assert [s.label for s in result.signals] == ["明确年限要求"]
    assert result.signals[0].evidence == "3年以上相关工作经验"  # verbatim


def test_years_signal_chinese_numerals_and_kind() -> None:
    result = analyze_job_strength("五年及以上工作经验优先")
    assert "明确年限要求" in [s.label for s in result.signals]
    assert result.signals[0].evidence == "五年及以上工作经验"


def test_years_missing_no_signal() -> None:
    result = analyze_job_strength("负责后端开发，参与需求评审。")
    assert "明确年限要求" not in [s.label for s in result.signals]


def test_skill_stack_signal_english_tag() -> None:
    result = analyze_job_strength("熟悉 Python、Java，参与过微服务改造。")
    signal = next(s for s in result.signals if s.label == "明确技能栈")
    assert signal.evidence == "熟悉 Python"


def test_skill_stack_signal_chinese_tag() -> None:
    result = analyze_job_strength("精通机器学习，掌握深度学习框架。")
    assert "明确技能栈" in [s.label for s in result.signals]


def test_skill_stack_no_verb_no_signal() -> None:
    """A skill name without 熟悉/精通/掌握 is not a stack signal."""
    result = analyze_job_strength("技术方向为 Python 后端。")
    assert "明确技能栈" not in [s.label for s in result.signals]


def test_degree_signal() -> None:
    result = analyze_job_strength("要求本科及以上学历。")
    signal = next(s for s in result.signals if s.label == "明确学历")
    assert signal.evidence == "本科及以上"


def test_degree_unlimited_is_not_a_bar() -> None:
    result = analyze_job_strength("学历不限，欢迎投递。")
    assert "明确学历" not in [s.label for s in result.signals]


def test_bonus_signal() -> None:
    result = analyze_job_strength("有开源项目经验者加分。")
    signal = next(s for s in result.signals if s.label == "明确加分项")
    assert signal.evidence == "加分"


def test_duty_list_three_plus_items() -> None:
    text = (
        "岗位职责：\n"
        "1. 负责核心模块设计\n"
        "2. 参与代码评审与质量改进\n"
        "3. 指导初级工程师\n"
        "4. 推进技术方案落地\n"
    )
    result = analyze_job_strength(text)
    signal = next(s for s in result.signals if s.label == "明确职责清单")
    assert "负责核心模块设计" in signal.evidence
    assert "参与代码评审与质量改进" in signal.evidence


def test_duty_list_below_three_no_signal() -> None:
    result = analyze_job_strength("岗位职责：\n1. 负责设计\n2. 参与评审\n")
    assert "明确职责清单" not in [s.label for s in result.signals]


def test_full_signal_jd_is_high() -> None:
    text = (
        "岗位职责：\n"
        "1. 负责推荐系统核心算法设计与实现\n"
        "2. 参与大规模分布式训练系统的性能优化\n"
        "3. 主导技术方案评审与落地\n"
        "任职要求：\n"
        "必须具备本科及以上学历，5年以上相关工作经验；\n"
        "熟悉 Python、Java，精通机器学习；有顶会论文加分。"
    )
    result = analyze_job_strength(text)
    assert result.score == 7
    assert result.tier == "high"
    assert result.base_score == 10


def test_medium_tier_threshold() -> None:
    """Years (2) + skill stack (2) = 4 -> medium."""
    result = analyze_job_strength("3年以上经验，熟悉 Python。")
    assert result.score == 4
    assert result.tier == "medium"
    assert result.base_score == 5


def test_low_tier_default() -> None:
    """Degree only (1) -> low, base_score 0."""
    result = analyze_job_strength("要求硕士学历。")
    assert result.score == 1
    assert result.tier == "low"
    assert result.base_score == 0


def test_empty_text_is_low() -> None:
    result = analyze_job_strength("")
    assert result.score == 0
    assert result.tier == "low"
    assert result.signals == []


def test_english_jd_without_signals_is_low() -> None:
    result = analyze_job_strength(
        "We are hiring backend engineers. Apply at careers.example.com"
    )
    assert result.score == 0
    assert result.tier == "low"


def test_to_dict_shape() -> None:
    result = analyze_job_strength("3年以上经验，熟悉 Python。")
    data = result.to_dict()
    assert data == {
        "score": 4,
        "tier": "medium",
        "base_score": 5,
        "evidence": [
            {"label": "明确年限要求", "weight": 2, "evidence": "3年以上经验"},
            {"label": "明确技能栈", "weight": 2, "evidence": "熟悉 Python"},
        ],
    }


def test_deterministic_same_input_same_output() -> None:
    text = "岗位职责：\n1. 负责设计\n2. 参与评审\n3. 指导新人\n任职要求：硕士学历，熟悉 MySQL。"
    assert analyze_job_strength(text) == analyze_job_strength(text)


def test_extract_candidate_carries_strength() -> None:
    """B1: the extraction output is enriched with the strength dict."""
    text = (
        "职位名称：算法工程师\n"
        "岗位职责：\n"
        "1. 负责推荐算法迭代\n"
        "2. 参与特征工程\n"
        "3. 跟进线上效果\n"
        "任职要求：3年以上经验，熟悉 Python。"
    )
    candidates = extract_jd_candidates(text, "https://example.com/job/1")
    assert len(candidates) == 1
    strength = candidates[0].strength
    assert strength is not None
    assert strength["score"] >= 3
    assert strength["tier"] in ("high", "medium", "low")
    assert all(
        {"label", "weight", "evidence"} <= set(item) for item in strength["evidence"]
    )


def test_extract_candidate_strength_defaults_when_empty() -> None:
    """An unenriched path (bare defaults) keeps strength None."""
    from backend.app.services.job_discovery.schemas import NormalizedJobCandidate

    candidate = NormalizedJobCandidate()
    assert candidate.strength is None
