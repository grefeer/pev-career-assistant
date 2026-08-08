"""C2 (FindJobs port): optional multi-JD skill-gap aggregation for
build-preparation-plan (docs/findjobs-optimization-plan.zh-CN.md §6.2).

``build_preparation_plan`` gains three optional input fields
(additional_target_artifact_ids / resume_skills / gap_limit).  When extra JD
ids are given, the output carries ``skill_gaps``: closed-set demanded skills
counted across the JDs (a JD counts a skill at most once), minus skills the
resume already names, ranked by job_count desc then skill name asc, capped
at gap_limit.  The single-JD path (no extra ids) stays byte-identical to the
pre-C2 output on every pre-existing field.  No LLM/DB/network: pure
deterministic aggregation over public-JD evidence.
"""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.career_skills.career_planning import (
    BuildPreparationPlanInput,
    build_preparation_plan,
)

_AGENT_JD = {
    "artifact_id": "jd-target",
    "source_url": "https://jobs.example/agent",
    "content_hash": "a" * 64,
    "title": "AI Agent 开发工程师",
    "visible_text": "岗位要求 Python、RAG 与 Agent、大模型 能力。",
}
_REC_JD = {
    "artifact_id": "jd-rec",
    "source_url": "https://jobs.example/rec",
    "content_hash": "b" * 64,
    "title": "推荐算法工程师",
    "visible_text": "负责推荐系统 CTR 预估，精通 Java 与 MySQL，熟悉 MySQL 主从同步。",
}
_PLATFORM_JD = {
    "artifact_id": "jd-platform",
    "source_url": "https://jobs.example/platform",
    "content_hash": "c" * 64,
    "title": "平台开发工程师",
    "visible_text": "需要 Python 与 MySQL，熟悉分布式系统。",
}


def _context(*evidence: object) -> ToolContext:
    return ToolContext(
        user_id="user-a",
        run_id="run-a",
        metadata={"observed_public_evidence": list(evidence)},
    )


def test_multi_jd_top_n_gaps_with_counts_dedup_and_resume_exclusion() -> None:
    """C2-1: N JD inputs -> top-N gaps + occurrence counts.

    Python is demanded by 2 JDs but owned by the resume (normalized via the
    alias/closed-set path) so it is not a gap; Java likewise.  MySQL named
    twice inside one JD counts once (within-JD dedup) plus once in the other
    -> job_count 2.  Blank / unknown resume skills are inert.
    """
    result = build_preparation_plan(
        _context(_AGENT_JD, _REC_JD, _PLATFORM_JD),
        BuildPreparationPlanInput(
            target_artifact_id="jd-target",
            focus_keywords=["RAG"],
            additional_target_artifact_ids=["jd-rec", "jd-platform"],
            resume_skills=["python", "Java", " ", "英语"],
            gap_limit=5,
        ),
    )

    assert [gap.model_dump() for gap in result.skill_gaps] == [
        {"skill": "MySQL", "job_count": 2},
        {"skill": "Agent", "job_count": 1},
        {"skill": "RAG", "job_count": 1},
        {"skill": "分布式系统", "job_count": 1},
        {"skill": "大模型", "job_count": 1},
    ]
    assert result.target_artifact_id == "jd-target"


def test_single_jd_output_byte_identical_to_legacy() -> None:
    """C2-2: without extra ids every pre-existing field is byte-identical to
    today's output; skill_gaps stays empty (aggregation never runs)."""
    result = build_preparation_plan(
        _context(_AGENT_JD),
        BuildPreparationPlanInput(
            target_artifact_id="jd-target",
            focus_keywords=["Python", "RAG", "Kubernetes"],
            target_date=date(2026, 8, 9),
        ),
    )

    assert result.skill_gaps == []
    assert result.model_dump(exclude={"skill_gaps"}) == {
        "target_artifact_id": "jd-target",
        "source_url": "https://jobs.example/agent",
        "jd_topics": ["python", "rag"],
        "actions": [
            "为 Python、RAG 各准备一个可量化的项目案例，并标明你的具体贡献。",
            "围绕 JD 中的 Python、RAG 做一次 30 分钟技术讲解演练，准备架构取舍与故障排查追问。",
        ],
        "schedule_assumption": "使用用户指定的目标日期。",
        "plan_items": [
            {
                "topic": "python",
                "priority": "P0",
                "time_budget_hours": 3,
                "due_date": date(2026, 8, 9),
                "completion_criteria": "准备一个 Python 相关项目案例，说明你的具体贡献和可核验结果。",
                "review_checkpoint": "完成后用 JD 的 Python 要求复盘：案例是否覆盖职责、取舍和追问。",
            },
            {
                "topic": "rag",
                "priority": "P1",
                "time_budget_hours": 3,
                "due_date": date(2026, 8, 9),
                "completion_criteria": "准备一个 RAG 相关项目案例，说明你的具体贡献和可核验结果。",
                "review_checkpoint": "完成后用 JD 的 RAG 要求复盘：案例是否覆盖职责、取舍和追问。",
            },
        ],
    }


def test_backward_compat_when_optional_fields_are_omitted() -> None:
    """Defaulting: the new input fields default to inert values."""
    payload = BuildPreparationPlanInput(
        target_artifact_id="jd-target",
        focus_keywords=["RAG"],
    )
    assert payload.additional_target_artifact_ids == []
    assert payload.resume_skills == []
    assert payload.gap_limit == 5
    result = build_preparation_plan(_context(_AGENT_JD), payload)
    assert result.skill_gaps == []


def test_tie_order_is_deterministic_skill_ascending() -> None:
    """Equal counts rank by skill name ascending (C before Go)."""
    result = build_preparation_plan(
        _context(
            {
                "artifact_id": "jd-c",
                "source_url": "https://jobs.example/c",
                "visible_text": "熟悉 C 语言，会写底层模块。",
            },
            {
                "artifact_id": "jd-go",
                "source_url": "https://jobs.example/go",
                "visible_text": "熟悉 Go 语言，写网络服务。",
            },
        ),
        BuildPreparationPlanInput(
            target_artifact_id="jd-c",
            focus_keywords=["C"],
            additional_target_artifact_ids=["jd-go"],
        ),
    )

    assert [gap.model_dump() for gap in result.skill_gaps] == [
        {"skill": "C", "job_count": 1},
        {"skill": "Go", "job_count": 1},
    ]


def test_gap_limit_truncates_ranked_list() -> None:
    """Without a resume, Python counts 2 too; the resume moves it below the
    cut so the top-2 becomes MySQL + the first 1-count skill."""
    result = build_preparation_plan(
        _context(_AGENT_JD, _REC_JD, _PLATFORM_JD),
        BuildPreparationPlanInput(
            target_artifact_id="jd-target",
            focus_keywords=["RAG"],
            additional_target_artifact_ids=["jd-rec", "jd-platform"],
            resume_skills=["python"],
            gap_limit=2,
        ),
    )

    assert [gap.model_dump() for gap in result.skill_gaps] == [
        {"skill": "MySQL", "job_count": 2},
        {"skill": "Agent", "job_count": 1},
    ]


def test_missing_extra_evidence_is_skipped() -> None:
    """A lost extra JD silently contributes nothing; survivors still count."""
    result = build_preparation_plan(
        _context(_AGENT_JD, _PLATFORM_JD),
        BuildPreparationPlanInput(
            target_artifact_id="jd-target",
            focus_keywords=["RAG"],
            additional_target_artifact_ids=["jd-missing", "jd-platform"],
            gap_limit=20,
        ),
    )

    assert {gap.skill for gap in result.skill_gaps} == {
        "Python",
        "RAG",
        "Agent",
        "大模型",
        "MySQL",
        "分布式系统",
    }


def test_non_string_or_empty_visible_text_skipped() -> None:
    """A JD without usable text never contributes a gap."""
    broken = {
        "artifact_id": "jd-broken",
        "source_url": "https://jobs.example/broken",
        "visible_text": 123,  # not a str -> skipped
    }
    result = build_preparation_plan(
        _context(_AGENT_JD, broken),
        BuildPreparationPlanInput(
            target_artifact_id="jd-target",
            focus_keywords=["RAG"],
            additional_target_artifact_ids=["jd-broken"],
        ),
    )
    assert [gap.model_dump() for gap in result.skill_gaps] == [
        {"skill": "Agent", "job_count": 1},
        {"skill": "Python", "job_count": 1},
        {"skill": "RAG", "job_count": 1},
        {"skill": "大模型", "job_count": 1},
    ]

    empty = {
        "artifact_id": "jd-empty",
        "source_url": "https://jobs.example/empty",
        "visible_text": "",
    }
    result = build_preparation_plan(
        _context(empty),
        BuildPreparationPlanInput(
            target_artifact_id="jd-empty",
            focus_keywords=["RAG"],
            additional_target_artifact_ids=["jd-platform"],
        ),
    )
    assert result.skill_gaps == []


def test_gap_limit_and_list_caps_are_validated() -> None:
    with pytest.raises(ValidationError):
        BuildPreparationPlanInput(
            target_artifact_id="jd-target",
            focus_keywords=["RAG"],
            gap_limit=0,
        )
    with pytest.raises(ValidationError):
        BuildPreparationPlanInput(
            target_artifact_id="jd-target",
            focus_keywords=["RAG"],
            resume_skills=["s"] * 51,
        )
    with pytest.raises(ValidationError):
        BuildPreparationPlanInput(
            target_artifact_id="jd-target",
            focus_keywords=["RAG"],
            additional_target_artifact_ids=[f"jd-{i}" for i in range(21)],
        )
