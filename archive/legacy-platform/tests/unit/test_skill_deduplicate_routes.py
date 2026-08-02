from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


_SCRIPT = Path(__file__).resolve().parents[2] / "skill" / "job-discovery" / "scripts" / "deduplicate.py"
_SPEC = importlib.util.spec_from_file_location("skill_deduplicate", _SCRIPT)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)


def test_shared_jd_template_with_distinct_apply_urls_is_not_collapsed() -> None:
    shared = {"company_name": "示例公司", "title": "算法工程师", "responsibilities": "同一岗位职责", "requirements": "本科", "locations": ["上海"]}
    first = {**shared, "apply_url": "https://jobs.example/a"}
    second = {**shared, "apply_url": "https://jobs.example/b"}

    assert _MODULE._cluster_by_title_substring([first, second]) == [[first], [second]]


def test_same_apply_url_title_variant_still_merges() -> None:
    shared = {"company_name": "示例公司", "responsibilities": "同一岗位职责", "requirements": "本科", "locations": ["上海"], "apply_url": "https://jobs.example/a"}
    first = {**shared, "title": "算法工程师"}
    second = {**shared, "title": "算法工程师-校招"}

    assert _MODULE._cluster_by_title_substring([first, second]) == [[first, second]]


def test_shared_listing_url_is_cleared_without_losing_distinct_jobs() -> None:
    candidates = [
        {"title": "算法工程师", "apply_url": "https://jobs.example/list"},
        {"title": "产品经理", "apply_url": "https://jobs.example/list"},
    ]

    assert _MODULE._clear_shared_listing_apply_urls(candidates) == 2
    assert [candidate["apply_url"] for candidate in candidates] == ["", ""]
    assert all("SHARED_LISTING_URL_CLEARED" in candidate["normalization_warnings"] for candidate in candidates)
