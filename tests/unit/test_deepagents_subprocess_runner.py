from __future__ import annotations

import subprocess
from pathlib import Path

from backend.app.services.deepagents_runtime.tools.skill_graphs.subprocess_runner import (
    SKILL_DIR,
    run_skill_script,
)


def test_skill_dir_resolves_to_skill_package() -> None:
    assert (SKILL_DIR / "SKILL.md").exists()
    assert (SKILL_DIR / "scripts" / "browse.py").exists()


def test_run_skill_script_rejects_unknown_scripts() -> None:
    def never_runs(*args, **kwargs):
        raise AssertionError("must not run")

    out = run_skill_script("rm", runner=never_runs)
    assert "ERROR" in out and "not allowed" in out


def test_run_skill_script_passes_through_stdout() -> None:
    captured = {}

    def fake_runner(
        script_path: Path,
        parts: list[str],
        *,
        cwd: Path,
        stdin: str | None,
        timeout: int,
    ) -> str:
        captured["path"] = script_path
        captured["cwd"] = cwd
        captured["parts"] = parts
        captured["stdin"] = stdin
        return "script stdout"

    out = run_skill_script("normalize", "--title 测试", stdin='{"x": 1}', runner=fake_runner)
    assert out == "script stdout"
    assert captured["parts"] == ["--title", "测试"]
    assert captured["cwd"] == SKILL_DIR
    assert captured["stdin"] == '{"x": 1}'


def test_run_skill_script_times_out_gracefully() -> None:
    def timing_out(*args, **kwargs):
        raise subprocess.TimeoutExpired("browse.py", timeout=900)

    out = run_skill_script("browse", runner=timing_out)
    assert "ERROR" in out and "timed out" in out


def test_run_skill_script_missing_script_file(monkeypatch, tmp_path) -> None:
    import backend.app.services.deepagents_runtime.tools.skill_graphs.subprocess_runner as sr

    def never_runs(*args, **kwargs):
        raise AssertionError("must not run")

    monkeypatch.setattr(sr, "SKILL_DIR", tmp_path)
    out = run_skill_script("browse", runner=never_runs)
    assert "ERROR" in out and "not found" in out


def test_run_skill_script_unparsable_cli_args() -> None:
    def never_runs(*args, **kwargs):
        raise AssertionError("must not run")

    out = run_skill_script("normalize", '"unbalanced', runner=never_runs)
    assert "ERROR" in out and "parse" in out


def test_run_skill_script_oserror_graceful() -> None:
    def failing_start(*args, **kwargs):
        raise OSError("boom")

    out = run_skill_script("browse", runner=failing_start)
    assert "ERROR" in out and "could not start" in out


def test_default_runner_runs_real_skill_script() -> None:
    out = run_skill_script("normalize", "--title 测试")
    assert "ERROR" not in out
    assert out  # the real zero-dependency script produced stdout


def test_default_runner_handles_empty_stdout(monkeypatch, tmp_path) -> None:
    import backend.app.services.deepagents_runtime.tools.skill_graphs.subprocess_runner as sr

    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "normalize.py").write_text("print('', end='')", encoding="utf-8")
    monkeypatch.setattr(sr, "SKILL_DIR", tmp_path)
    assert run_skill_script("normalize") == ""


def test_default_runner_merges_stderr(monkeypatch, tmp_path) -> None:
    import backend.app.services.deepagents_runtime.tools.skill_graphs.subprocess_runner as sr

    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "normalize.py").write_text(
        "import sys; print('boom', file=sys.stderr)", encoding="utf-8"
    )
    monkeypatch.setattr(sr, "SKILL_DIR", tmp_path)
    out = run_skill_script("normalize")
    assert "[stderr]" in out and "boom" in out
