"""Contract tests for the Worker/PEV ten-URL manual evaluator."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace


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

    row = runner._timeout_row(
        "pdd", "拼多多", "https://example.test/jobs", 180, mode="adapter",
    )

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


def test_worker_eval_timeout_terminates_windows_process_tree(monkeypatch) -> None:
    runner = _load_runner()
    calls: list[list[str]] = []

    class Child:
        pid = 12345

        def wait(self, timeout: int) -> None:
            assert timeout == 15

        def kill(self) -> None:
            raise AssertionError("Windows must use taskkill /T")

    monkeypatch.setattr(runner.os, "name", "nt")
    monkeypatch.setattr(
        runner.subprocess, "run",
        lambda args, **kwargs: calls.append(args) or SimpleNamespace(returncode=0),
    )

    runner._terminate_process_tree(Child())

    assert calls == [["taskkill", "/PID", "12345", "/T", "/F"]]


def test_worker_eval_clears_a_previous_single_site_result_before_running(tmp_path, monkeypatch) -> None:
    runner = _load_runner()
    row_path = tmp_path / "old-skill-result.json"
    row_path.write_text('{"status":"stale"}', encoding="utf-8")
    monkeypatch.setattr(runner, "_row_path", lambda _slug: row_path)

    runner._clear_row_file("xiaohongshu")

    assert not row_path.exists()


def test_skill_eval_seeds_the_explicit_company_as_task_context() -> None:
    runner = _load_runner()

    site_cfg = runner._skill_site_config("小红书", "https://example.test/jobs")

    assert site_cfg["url"] == "https://example.test/jobs"
    assert site_cfg["raw_fields"] == [{"field_name": "公司名称", "value": "小红书"}]
