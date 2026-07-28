"""Contract tests for the Worker/PEV ten-URL manual evaluator."""
from __future__ import annotations

import importlib.util
from pathlib import Path


_RUNNER = Path(__file__).resolve().parents[1] / "manual" / "run_worker_ten_url_eval.py"


def _load_runner():
    spec = importlib.util.spec_from_file_location("worker_ten_url_eval", _RUNNER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_worker_eval_keeps_pev_and_legacy_buckets_separate() -> None:
    runner = _load_runner()

    assert set(runner.PEV_SITES) == {
        "deeproute", "pdd", "feishu-xiaopeng", "inovance", "xiaohongshu",
        "bytedance", "xiaomi",
    }
    assert runner._bucket_for({"coverage_verified": True}) == "pev_pass"
    assert runner._bucket_for({"coverage_verified": False, "execution_path": "legacy_path_c"}) == "legacy"
    assert runner._bucket_for({"coverage_verified": False, "execution_path": "path_a_adapter"}) == "pev_fail"


def test_worker_eval_timeout_row_is_not_misreported_as_legacy_success() -> None:
    runner = _load_runner()

    row = runner._timeout_row("pdd", "拼多多", "https://example.test/jobs", 180)

    assert row["target_path"] == "pev"
    assert row["status"] == "timed_out"
    assert row["bucket"] == "timed_out"
    assert row["timeout_sec"] == 180


def test_worker_eval_requires_body_gate_for_pev_pass() -> None:
    runner = _load_runner()

    assert runner._bucket_for({"coverage_verified": True}) == "pev_pass"
    # _run_pev_row applies the stronger gate; coverage alone is insufficient.
    assert runner._passes_pev_gate({
        "coverage_verified": True,
        "candidate_count": 2,
        "body_candidate_count": 1,
        "unique_listing_count": 2,
        "coverage": {"failed_detail_count": 0},
    })[0] is False
