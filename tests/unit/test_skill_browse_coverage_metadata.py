from __future__ import annotations

import importlib.util
from pathlib import Path


_SCRIPT = Path(__file__).resolve().parents[2] / "skill" / "job-discovery" / "scripts" / "browse.py"
_SPEC = importlib.util.spec_from_file_location("skill_browse", _SCRIPT)
assert _SPEC and _SPEC.loader
_BROWSE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_BROWSE)


def test_declared_page_count_proves_finite_range_when_under_limit() -> None:
    fetch_count, declared_count = _BROWSE._compute_total_pages("1 / 16", 10, 20)

    assert (fetch_count, declared_count) == (16, 16)


def test_declared_page_count_marks_safety_cap_as_incomplete() -> None:
    fetch_count, declared_count = _BROWSE._compute_total_pages("1 / 35", 10, 20)

    assert (fetch_count, declared_count) == (20, 35)


def test_unknown_page_count_has_no_synthetic_terminal_proof() -> None:
    fetch_count, declared_count = _BROWSE._compute_total_pages(None, None, 20)

    assert (fetch_count, declared_count) == (20, None)


def test_body_count_recognizes_chinese_result_total() -> None:
    assert _BROWSE._scan_body_count("已选 0 条件 | 20 结果 | 清除") == "20"


def test_jd_detail_evidence_distinguishes_listings_from_real_details() -> None:
    assert _BROWSE._has_jd_detail_evidence("【2027秋招】算法工程师\n上海") is False
    assert _BROWSE._has_jd_detail_evidence("职位描述\n负责算法开发") is True


def test_interact_follows_a_single_homepage_to_list_transition() -> None:
    assert _BROWSE._navigated_list_url(
        start_url="https://jobs.example/#/home",
        interact_text="=== JOB 1 (https://jobs.example/#/jobs/) ===\n列表",
        cards_found=1,
    ) == "https://jobs.example/#/jobs/"


def test_detail_url_helper_deduplicates_hash_links_and_respects_limit() -> None:
    class Page:
        def evaluate(self, script):
            return ["#/job/a", "#/job/a", "#/job/b", "#/jobs/"]

    assert _BROWSE._detail_urls_from_current_page(
        Page(), "https://jobs.example/#/jobs/", 1,
    ) == ["https://jobs.example/#/job/a"]
    assert _BROWSE._navigated_list_url(
        start_url="https://jobs.example/#/home",
        interact_text="=== JOB 1 (https://jobs.example/#/jobs/) ===\n列表",
        cards_found=2,
    ) == "https://jobs.example/#/jobs/"


def test_parallel_fetch_source_prioritizes_generic_detail_evidence_for_card_spas() -> None:
    source = _SCRIPT.read_text(encoding="utf-8")

    fallback = source[source.index("if detect is None:"):source.index("# ---- Compute page URLs")]
    assert "detail_result = browse_interact_mode" in fallback
    assert "if detail_result.get(\"jd_detail_evidence\")" in fallback
    assert "interact_fallback_no_detect" in fallback


def test_only_plain_list_mode_may_use_the_url_cache() -> None:
    source = _SCRIPT.read_text(encoding="utf-8")

    assert 'if args.mode != "list":\n        cache_mode = "off"' in source
    assert 'if ch and args.mode == "list":' in source


def test_card_interaction_idle_wait_is_bounded_for_long_lived_spa_connections() -> None:
    assert _BROWSE._card_interaction_idle_timeout_ms(800) == 1500
    assert _BROWSE._card_interaction_idle_timeout_ms(3000) == 1500
    assert _BROWSE._card_interaction_idle_timeout_ms(100) == 500


def test_category_expansion_is_only_a_recovery_for_an_empty_listing() -> None:
    assert _BROWSE._should_expand_categories(0) is True
    assert _BROWSE._should_expand_categories(1) is False


def test_overlapping_parent_card_and_title_link_are_deduplicated() -> None:
    assert _BROWSE._boxes_substantially_overlap(
        {"x": 10, "y": 10, "width": 120, "height": 24},
        (0, 0, 160, 80),
    ) is True
    assert _BROWSE._boxes_substantially_overlap(
        {"x": 200, "y": 10, "width": 20, "height": 20},
        (0, 0, 160, 80),
    ) is False


def test_interact_source_advances_generic_paginated_listings() -> None:
    source = _SCRIPT.read_text(encoding="utf-8")

    section = source[source.index("def browse_interact_mode"):source.index("def _navigated_list_url")]
    assert "while found < max_cards:" in section
    assert "next_btn = _find_next_page_button(page)" in section
    assert 'label_prefix="JOB"' in section


def test_detail_navigation_uses_bounded_domcontentloaded_return() -> None:
    source = _SCRIPT.read_text(encoding="utf-8")

    assert 'page.go_back(wait_until="domcontentloaded", timeout=2500)' in source


def test_scroll_loading_reuses_the_bounded_idle_wait() -> None:
    source = _SCRIPT.read_text(encoding="utf-8")
    section = source[source.index("def _scroll_to_load"):source.index("# ---------------------------------------------------------------------------\n# Text extraction")]

    assert 'timeout=_card_interaction_idle_timeout_ms(wait_ms)' in section


def test_public_json_collector_keeps_only_title_and_jd_shaped_records() -> None:
    collector = _BROWSE.PublicJobEvidenceCollector()
    collector.feed_payload({
        "result": {
            "total": "2",
            "list": [
                {"id": "a", "name": "算法工程师", "jobDuty": "负责" * 30, "workLocation": "上海"},
                {"id": "b", "name": "只有标题"},
            ],
        },
    })

    assert collector.expected_count == 2
    assert collector.records == [{
        "id": "a", "title": "算法工程师", "department": None,
        "location": "上海", "responsibilities": "负责" * 30,
    }]
    assert "=== PUBLIC JOB 1 ===" in collector.evidence_text()
