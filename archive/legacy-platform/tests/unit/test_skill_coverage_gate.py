from __future__ import annotations

import importlib.util
import hashlib
import json
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


def test_coverage_keeps_same_title_url_less_openings_with_distinct_jds() -> None:
    result = _MODULE.evaluate_coverage(
        page_files=["page_01.txt"], terminal_evidence="end",
        candidates=[
            {"title": "计划培训生", "responsibilities": "负责产销平衡"},
            {"title": "计划培训生", "responsibilities": "负责订单管理"},
        ],
    )
    assert result["coverage_verified"] is True


def test_coverage_does_not_collapse_distinct_titles_with_shared_listing_url() -> None:
    result = _MODULE.evaluate_coverage(
        page_files=["page_01.txt"], terminal_evidence="last_page_disabled",
        candidates=[
            {"title": "算法工程师", "apply_url": "https://jobs.example/list", "responsibilities": "开发"},
            {"title": "产品经理", "apply_url": "https://jobs.example/list", "responsibilities": "规划"},
        ],
    )

    assert result["coverage_verified"] is True
    assert result["unique_listing_count"] == 2


def test_manifest_coverage_rejects_a_missing_page_even_with_terminal_signal(tmp_path: Path) -> None:
    candidates = tmp_path / "candidates.json"
    candidates.write_text(
        '[{"title":"算法工程师","responsibilities":"开发"}]', encoding="utf-8",
    )
    manifest = tmp_path / "browse.json"
    manifest.write_text(
        '{"page_files":["output/evidence/pages/page_01.txt"],'
        '"terminal_evidence":"end"}', encoding="utf-8",
    )

    result = _MODULE.evaluate_manifest_coverage(
        candidates_path=candidates, manifest_path=manifest, skill_root=tmp_path,
    )

    assert result["coverage_verified"] is False
    assert "manifest_page_missing" in result["reasons"]


def test_manifest_coverage_requires_candidate_reference_to_observed_page(tmp_path: Path) -> None:
    page = tmp_path / "output" / "evidence" / "pages" / "page_01.txt"
    page.parent.mkdir(parents=True)
    page.write_text("source", encoding="utf-8")
    candidates = tmp_path / "candidates.json"
    candidates.write_text(json.dumps([{
        "title": "算法工程师", "responsibilities": "开发",
        "evidence_refs": [{"content_hash": "forged"}],
    }]), encoding="utf-8")
    manifest = tmp_path / "browse.json"
    manifest.write_text(json.dumps({
        "page_files": ["output/evidence/pages/page_01.txt"],
        "terminal_evidence": "end", "listing_count": 1,
    }), encoding="utf-8")

    result = _MODULE.evaluate_manifest_coverage(
        candidates_path=candidates, manifest_path=manifest, skill_root=tmp_path,
    )

    assert result["coverage_verified"] is False
    assert "candidate_evidence_not_in_manifest" in result["reasons"]

    candidates.write_text(json.dumps([{
        "title": "算法工程师", "responsibilities": "开发",
        "evidence_refs": [{"content_hash": hashlib.sha256(page.read_bytes()).hexdigest()}],
    }]), encoding="utf-8")
    assert _MODULE.evaluate_manifest_coverage(
        candidates_path=candidates, manifest_path=manifest, skill_root=tmp_path,
    )["coverage_verified"] is True


def test_manifest_coverage_rejects_declared_pages_not_collected(tmp_path: Path) -> None:
    page = tmp_path / "output" / "evidence" / "pages" / "page_01.txt"
    page.parent.mkdir(parents=True)
    page.write_text("source", encoding="utf-8")
    candidates = tmp_path / "candidates.json"
    candidates.write_text("[]", encoding="utf-8")
    manifest = tmp_path / "browse.json"
    manifest.write_text(json.dumps({
        "page_files": ["output/evidence/pages/page_01.txt"], "terminal_evidence": "end",
        "declared_total_pages": 44, "pages_collected": 20,
    }), encoding="utf-8")
    result = _MODULE.evaluate_manifest_coverage(
        candidates_path=candidates, manifest_path=manifest, skill_root=tmp_path,
    )
    assert "browse_truncated_by_max_pages" in result["reasons"]
