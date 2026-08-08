"""B2 (FindJobs port): two-level job taxonomy classification.

Covers ``data/job_taxonomy.json`` + ``tools/taxonomy.py`` (see
docs/findjobs-optimization-plan.zh-CN.md §5.2): a reviewed two-level tree
of >=15 level-1 categories, deterministic keyword scoring, runtime with
zero LLM calls, and the enrichment of ``NormalizedJobCandidate.taxonomy``.
The tree is seeded from the archived ``_ROLE_FAMILY_MARKERS`` (9 families,
git ea0a70b) - every seed marker must remain reachable through the
keywords.  All fixtures are deterministic; no LLM/DB/network.
"""

from __future__ import annotations

import inspect
import json

import pytest

from backend.app.services.job_discovery.tools import taxonomy as tx

_INDEX = tx.load_taxonomy()


@pytest.fixture(autouse=True)
def _clear_caches() -> None:
    """The lru_caches must never observe a monkeypatched path twice."""
    yield
    tx.load_taxonomy.cache_clear()
    tx._keyword_patterns.cache_clear()


def test_tree_coverage_and_review_metadata() -> None:
    """B2-1: >=15 level-1 categories, two levels, human-review metadata."""
    assert len(_INDEX.level1) >= 15
    assert len(_INDEX.entries) >= len(_INDEX.level1)
    raw = json.loads(tx._TAXONOMY_PATH.read_text(encoding="utf-8"))
    for key in ("version", "reviewed_at", "reviewer", "level1"):
        assert key in raw
    for entry in _INDEX.entries:
        assert entry.keywords  # every level-2 entry has keywords


def test_archive_seed_markers_reachable() -> None:
    """Every archived _ROLE_FAMILY_MARKERS seed remains a keyword somewhere."""
    seed_markers = {
        "product": ("产品", "pm", "product manager"),
        "dev": ("开发", "研发", "engineer", "程序员", "developer"),
        "design": ("设计", "ued", "ui"),
        "algo": ("算法",),
        "data": ("数据", "analyst", "数据工程", "数据科学"),
        "ops": ("运营", "operation"),
        "test": ("测试", "qa", "质量保障"),
        "security": ("安全", "security"),
        "research": ("研究", "研究员", "research", "scientist"),
    }
    all_keywords = {kw for entry in _INDEX.entries for kw in entry.keywords}
    for family, markers in seed_markers.items():
        for marker in markers:
            assert marker in all_keywords, f"{family} seed {marker!r} missing"


def test_module_is_runtime_llm_free() -> None:
    """B2-1: deterministic classification; no model/network import."""
    source = inspect.getsource(tx)
    assert "ChatOpenAI" not in source
    assert "langchain" not in source
    assert "requests" not in source


def test_classify_single_hit() -> None:
    assert tx.classify_text("负责推荐系统 CTR 预估") == ("算法", "推荐算法")


def test_classify_english_case_insensitive() -> None:
    assert tx.classify_text("We hire a PM for product strategy") == ("产品", "产品经理")
    assert tx.classify_text("looking for a Senior DEVELOPER") == ("开发", "研发工程师")


def test_classify_best_of_multiple_hits() -> None:
    """The level-2 entry with the most distinct keyword hits wins."""
    text = "职位名称：数据分析师\n职责：搭建数据仓库，负责 ETL 流程，产出分析报表"
    assert tx.classify_text(text) == ("数据", "数据分析")  # 3 hits vs 2


def test_classify_tie_first_in_file_wins() -> None:
    """Equal hits -> the first entry in file order (deterministic)."""
    # "分析师" (数据) and "算法" (算法) each hit once -> 算法 is earlier in
    # file order (产品/开发/前端/客户端/算法/数据/...).
    text = "既做过分析师，也做过算法"
    assert tx.classify_text(text) == ("算法", "机器学习算法")


def test_classify_word_boundary_english() -> None:
    """A single-letter keyword never fires inside a longer word."""
    text = "covering all engineering domains at scale"  # "cv" is not a word here
    assert tx.classify_text(text) == ("", "")


def test_classify_no_hit_returns_empty() -> None:
    assert tx.classify_text("无关文本，无任何岗位关键词") == ("", "")
    assert tx.classify_text("") == ("", "")


def test_classify_deterministic_same_input() -> None:
    text = "算法工程师，负责推荐系统 CTR 预估"
    assert tx.classify_text(text) == tx.classify_text(text)


def test_extract_candidate_carries_taxonomy() -> None:
    """B2: the extraction output carries [level1, level2]."""
    from backend.app.services.job_discovery.tools.jd_extraction import (
        extract_jd_candidates,
    )

    candidates = extract_jd_candidates(
        "职位名称：推荐算法工程师\n岗位职责：负责推荐系统 CTR 预估。",
        "https://example.com/job/1",
    )
    assert candidates
    assert candidates[0].taxonomy == ["算法", "推荐算法"]


def test_taxonomy_tags_enrichment_forms() -> None:
    """Enrichment form: [level1, level2] on hit, [] when unclassified."""
    assert tx.taxonomy_tags("负责推荐系统 CTR 预估") == ["算法", "推荐算法"]
    assert tx.taxonomy_tags("无关岗位文本") == []


def test_extract_candidate_taxonomy_empty_when_unclassified() -> None:
    from backend.app.services.job_discovery.tools.jd_extraction import (
        extract_jd_candidates,
    )

    candidates = extract_jd_candidates(
        "职位名称：客服专员\n工作内容：接听用户来电。",
        "https://example.com/job/2",
    )
    assert candidates
    assert candidates[0].taxonomy == []


@pytest.mark.parametrize(
    "bad",
    [
        ["算法"],  # not a dict
        {},  # no level1 key
        {"level1": []},  # empty level1
        {"level1": ["算法"]},  # category not an object
        {"level1": [{"level2": []}]},  # category without name
        {"level1": [{"name": "算法"}]},  # level1 without level2
        {"level1": [{"name": "算法", "level2": ["推荐算法"]}]},  # entry not an object
        {"level1": [{"name": "算法", "level2": [{"keywords": ["推荐"]}]}]},  # no name
        {"level1": [{"name": "算法", "level2": [{"name": "推荐算法"}]}]},  # no keywords
        {"level1": [{"name": "算法", "level2": [{"name": "推荐算法", "keywords": []}]}]},  # empty
        {"level1": [{"name": "算法", "level2": [{"name": "推荐算法", "keywords": ["推荐", ""]}]}]},  # blank
    ],
)
def test_load_rejects_bad_files(tmp_path, monkeypatch, bad) -> None:
    """A malformed tree raises, never half-loads."""
    data_file = tmp_path / "job_taxonomy.json"
    data_file.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(tx, "_TAXONOMY_PATH", data_file)
    with pytest.raises(ValueError):
        tx.load_taxonomy()
