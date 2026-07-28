from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


_PATH = Path(__file__).resolve().parents[2] / "skill" / "job-discovery" / "scripts" / "coverage_gate.py"
_SPEC = importlib.util.spec_from_file_location("skill_coverage_gate", _PATH)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)


def test_coverage_pass_requires_pages_terminal_and_every_body() -> None:
    result = _MODULE.evaluate_coverage(
        page_files=["page_01.txt"], terminal_evidence="last_page_disabled", expected_count=1,
        candidates=[{"title": "算法", "apply_url": "https://jobs.example/a", "responsibilities": "开发"}],
    )
    assert result["coverage_verified"] is True


def test_coverage_rejects_missing_body_duplicate_and_missing_terminal() -> None:
    result = _MODULE.evaluate_coverage(
        page_files=["page_01.txt"], terminal_evidence=None,
        candidates=[
            {"title": "算法", "apply_url": "https://jobs.example/a"},
            {"title": "算法", "apply_url": "https://jobs.example/a", "requirements": "本科"},
        ],
    )
    assert result["coverage_verified"] is False
    assert set(result["reasons"]) == {"missing_terminal_evidence", "missing_jd_body", "duplicate_candidate_identity"}


def test_coverage_keeps_url_less_openings_in_different_departments_distinct() -> None:
    result = _MODULE.evaluate_coverage(
        page_files=["page_01.txt"], terminal_evidence="last_page_disabled",
        candidates=[
            {"title": "算法工程师", "department": "自动驾驶", "locations": ["北京"], "responsibilities": "开发"},
            {"title": "算法工程师", "department": "机器人", "locations": ["北京"], "responsibilities": "开发"},
        ],
    )

    assert result["coverage_verified"] is True
