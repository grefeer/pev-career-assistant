"""A2 (FindJobs port): closed-set skill tag validation.

Covers ``data/skill_tags.json`` + ``tools/skill_validator.py`` (see
docs/findjobs-optimization-plan.zh-CN.md §4.2): a reviewed closed set of
<=80 tags, deterministic and LLM-free at runtime, illegal tags never leak,
low-information labels filtered, and a below-minimum fallback that only
replays tags literally named in the JD text.  The only file dependency is
the checked-in ``skill_tags.json``; no LLM/DB/network.
"""

from __future__ import annotations

import inspect
import json

import pytest

from backend.app.services.job_discovery.tools import skill_validator as sv

_CLOSED_SET = sv.load_skill_tags()


@pytest.fixture(autouse=True)
def _clear_caches() -> None:
    """The lru_caches must never observe a monkeypatched path twice."""
    yield
    sv.load_skill_tags.cache_clear()
    sv._closed_map.cache_clear()


def test_closed_set_size_and_reviewed_metadata() -> None:
    """A2-1: <=80 reviewed tags, non-empty, unique, human-review metadata."""
    assert 1 <= len(_CLOSED_SET) <= 80
    assert len(set(_CLOSED_SET)) == len(_CLOSED_SET)
    raw = json.loads(
        sv._SKILL_TAGS_PATH.read_text(encoding="utf-8")
    )
    for key in ("version", "reviewed_at", "reviewer", "tags"):
        assert key in raw


def test_closed_set_contains_no_low_information_labels() -> None:
    """Data hygiene: {AI, 技术, 数学, 计算机, ...} never enter the set."""
    for tag in sv._LOW_INFORMATION_SKILLS:
        assert tag not in _CLOSED_SET, tag


def test_module_is_runtime_llm_free() -> None:
    """A2-2: the validator is deterministic; no model/network import."""
    source = inspect.getsource(sv)
    assert "ChatOpenAI" not in source
    assert "langchain" not in source
    assert "requests" not in source


def test_alias_remapping_to_canonical_spelling() -> None:
    """Curated aliases resolve to the closed-set spelling."""
    for raw, expected in (
        ("python", "Python"),
        ("PYTHON3", "Python"),
        ("cpp", "C++"),
        ("nlp", "NLP"),
        ("llm", "大模型"),
        ("rag", "RAG"),
        ("agent", "Agent"),
        ("推荐算法", "推荐系统"),
    ):
        assert sv.normalize_skill(raw) == expected, raw


def test_unknown_tag_dropped() -> None:
    """Tags outside the closed set and its aliases are never invented."""
    for raw in ("Fortran", "Photoshop", "Kotlin", "Excel", "随机数"):
        assert sv.normalize_skill(raw) is None, raw


def test_blank_tag_dropped() -> None:
    assert sv.normalize_skill("") is None
    assert sv.normalize_skill("   ") is None


def test_english_membership_case_insensitive() -> None:
    """A closed-set English tag matches case-insensitively."""
    assert sv.normalize_skill("python") == "Python"
    assert sv.normalize_skill("PYTHON") == "Python"
    assert sv.normalize_skill("mysql") == "MySQL"


def test_low_information_filtered() -> None:
    """{AI, 技术, 数学, 计算机, ...} survive membership but are filtered."""
    tags = sv.validate_skills(
        ["AI", "人工智能", "技术", "数学", "计算机", "Python", "开发"],
        fallback_text="需要 AI 技术背景",
        min_tags=3,
    )
    for tag in sv._LOW_INFORMATION_SKILLS:
        assert tag not in tags
    assert "Python" in tags


def test_validate_dedupe_keeps_first_order() -> None:
    """Duplicates (alias or case variants) collapse to one canonical tag."""
    assert sv.validate_skills(["Python", "PYTHON", "python", "C++", "cpp"]) == [
        "Python",
        "C++",
    ]


def test_below_min_fallback_from_jd_text() -> None:
    """min_tags unmet -> closed-set tags literally named in the JD text."""
    tags = sv.validate_skills(
        ["Python"],
        fallback_text="熟悉 Java 和 MySQL，有分布式系统经验",
        min_tags=3,
    )
    # Fallback appends in closed-set order, deduped.
    assert tags == ["Python", "Java", "分布式系统", "MySQL"]


def test_fallback_english_case_insensitive() -> None:
    tags = sv.validate_skills(
        [],
        fallback_text="proficient in python, redis and kubernetes",
        min_tags=3,
    )
    assert tags == ["Python", "Redis", "Kubernetes"]


def test_fallback_never_invents() -> None:
    """A text naming no closed-set tag yields no fallback entries."""
    tags = sv.validate_skills(
        ["Python"],
        fallback_text="岗位职责：负责日常行政事务，协调会议安排。",
        min_tags=5,
    )
    assert tags == ["Python"]


def test_fallback_absent_text_returns_as_is() -> None:
    assert sv.validate_skills(["Python"], min_tags=3) == ["Python"]


def test_non_list_input_coerced_to_empty() -> None:
    assert sv.validate_skills("Python") == []
    assert sv.validate_skills(None) == []
    assert sv.validate_skills(42) == []


def test_non_string_items_skipped() -> None:
    assert sv.validate_skills([1, None, {"skill": "Python"}, ["Python"]]) == []


def test_illegal_tags_never_leak_property() -> None:
    """Invariant sweep: any dirty input -> subset of the closed set, no
    low-information tag, no duplicates (property-style deterministic table).
    """
    dirty_inputs: list[object] = [
        "Python",
        ["Python", "PYTHON", "python"],
        [" C++ ", "cpp", "C++"],
        ["Fortran", "Photoshop", "Rust", "不存在的技能"],
        ["AI", "技术", "数学", "计算机", "开发", "工程师"],
        ["", "   ", None, 42, {"s": "x"}, ["Python"]],
        ["推荐算法", "深度学习框架", "大语言模型(llm)"],
        [tag.lower() for tag in _CLOSED_SET if tag.isascii()],
    ]
    for raw in dirty_inputs:
        tags = sv.validate_skills(
            raw,
            fallback_text="需要精通 各种 技能 以 完成 工作",  # no closed-set hit
            min_tags=3,
        )
        assert isinstance(tags, list)
        assert len(set(tags)) == len(tags)
        for tag in tags:
            assert tag in set(_CLOSED_SET)
            assert tag not in sv._LOW_INFORMATION_SKILLS


@pytest.mark.parametrize(
    "bad",
    [
        ["Python"],  # not a dict
        {"tags": "Python"},  # tags not a list
        {"tags": []},  # empty list
        {"tags": ["Python", ""]},  # blank entry
        {"tags": ["Python"] * 81},  # over the 80-entry ceiling
    ],
)
def test_load_rejects_bad_files(tmp_path, monkeypatch, bad) -> None:
    """A2-1: a malformed or oversized data file raises, never half-loads."""
    data_file = tmp_path / "skill_tags.json"
    data_file.write_text(json.dumps(bad), encoding="utf-8")
    monkeypatch.setattr(sv, "_SKILL_TAGS_PATH", data_file)
    with pytest.raises(ValueError):
        sv.load_skill_tags()
