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
