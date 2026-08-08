"""Unit tests for tests/manual/run_deepagents_parity.py (gate + report).

The parity runner is live-only (gitignored; force-added to git) — these
tests cover the SKIP path and the log-driven report generation with
fakes only: no live HTTP, no real skill scripts, no LLM.  The heavy
runtime imports (``enable_playwright_fallback`` /
``build_job_discovery_tool``) live inside ``main()``'s env-guarded branch,
so importing the module itself must never load them (review minor b), and
the report table rows are parsed from ``parity_run*.log`` content, never
hardcoded (review minor a).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_RUNNER_PATH = Path(__file__).resolve().parents[1] / "manual" / "run_deepagents_parity.py"


def _load_runner():
    """Import the gitignored manual runner by path (never collected by pytest)."""
    spec = importlib.util.spec_from_file_location(
        "run_deepagents_parity_under_test", _RUNNER_PATH
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_heavy_runtime_names_not_bound_at_module_level() -> None:
    # review minor b: the SKIP path's "no imports of live-only modules"
    # claim must hold — importing the runner module executes only stdlib
    # (the heavy runtime imports are inside main()'s env-guarded branch)
    runner = _load_runner()
    assert not hasattr(runner, "build_job_discovery_tool")
    assert not hasattr(runner, "enable_playwright_fallback")
    assert hasattr(runner, "render_parity_report")


def test_skip_path_returns_0_without_loading_runtime(monkeypatch, capsys) -> None:
    runner = _load_runner()
    monkeypatch.delenv("RUN_DEEPAGENTS_PARITY", raising=False)

    seen: list[str] = []
    real_import = __import__

    def spy_import(name, *args, **kwargs):
        if "deepagents_runtime" in name or "career_skills" in name:
            seen.append(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", spy_import)
    assert runner.main([]) == 0
    out = capsys.readouterr().out
    assert "RUN_DEEPAGENTS_PARITY=1 required" in out
    assert seen == []  # the heavy runtime never loads on the SKIP path


def test_report_reads_log_rows_never_hardcodes(tmp_path) -> None:
    # review minor a: the report table rows are derived from the log
    # files' own content — a crashed run shows as ERROR, not a fabricated
    # gate result (parity_run1.log was a traceback while the old report
    # claimed gate numbers for it)
    runner = _load_runner()
    (tmp_path / "parity_run1.log").write_text(
        'Traceback (most recent call last):\n'
        '  File "run_deepagents_parity.py", line 73, in <module>\n'
        "    raise SystemExit(main())\n"
        "AttributeError: 'list' object has no attribute 'get'\n",
        encoding="utf-8",
    )
    (tmp_path / "parity_run2.log").write_text(
        "baseline success=3 candidates=797 | ours success=1 candidates=1 "
        "coverage={'verified': False, 'reasons': ['missing_jd_body']}\n"
        "PARITY FAILED: regression vs B-mode baseline\n",
        encoding="utf-8",
    )
    (tmp_path / "parity_run3.log").write_text(
        "baseline success=3 candidates=797 | ours success=6 candidates=1 "
        "coverage={'verified': False}\n"
        "PARITY FAILED: coverage gate not verified\n",
        encoding="utf-8",
    )
    (tmp_path / "parity_run4.log").write_text(
        "RUN_DEEPAGENTS_PARITY=1 required (live LLM + Playwright)\n",
        encoding="utf-8",
    )
    (tmp_path / "parity_run5.log").write_text(
        "baseline success=3 candidates=797 | ours success=6 candidates=812 "
        "coverage={'verified': True, 'reasons': []}\n"
        "PARITY PASSED\n",
        encoding="utf-8",
    )
    table = runner.render_parity_report(tmp_path)
    # run 1 is a crash, honestly rendered — no gate numbers fabricated
    assert "parity_run1.log" in table and "ERROR (crashed)" in table
    assert "AttributeError" in table
    # gate rows carry the numbers read from the log lines
    assert "parity_run2.log" in table and "FAILED" in table
    assert "1/1" in table and "3/797" in table and "False" in table
    assert "parity_run3.log" in table
    # SKIP row comes from the SKIP log, PASSED row from the PASSED log
    assert "parity_run4.log" in table and "SKIP" in table
    assert "parity_run5.log" in table and "PASSED" in table
    assert "812" in table and "True" in table


def test_report_incomplete_log_without_verdict(tmp_path) -> None:
    runner = _load_runner()
    (tmp_path / "parity_run9.log").write_text(
        "baseline success=3 candidates=797 | ours success=6 candidates=1 "
        "coverage={'verified': False}\n",
        encoding="utf-8",
    )
    table = runner.render_parity_report(tmp_path)
    assert "parity_run9.log" in table
    assert "INCOMPLETE" in table


def test_report_empty_dir(tmp_path) -> None:
    runner = _load_runner()
    table = runner.render_parity_report(tmp_path)
    assert "parity_run" in table and "(no" in table  # no hardcoded rows


def test_main_report_mode_prints_table(tmp_path, monkeypatch, capsys) -> None:
    runner = _load_runner()
    (tmp_path / "parity_run1.log").write_text(
        "baseline success=3 candidates=797 | ours success=6 candidates=1 "
        "coverage={'verified': False}\n"
        "PARITY FAILED: regression vs B-mode baseline\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("RUN_DEEPAGENTS_PARITY", raising=False)
    assert runner.main(["--report", "--log-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "parity_run1.log" in out and "FAILED" in out
