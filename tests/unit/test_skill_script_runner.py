from __future__ import annotations

from pathlib import Path

from backend.app.services.agent_runtime.skill_script_runner import (
    RunSkillScriptInput,
    SkillScriptRunner,
)


def test_runner_executes_only_a_relative_python_script(tmp_path: Path) -> None:
    script = tmp_path / "scripts" / "ok.py"
    script.parent.mkdir()
    script.write_text(
        "import sys\n"
        "print('stdout:' + ','.join(sys.argv[1:]))\n"
        "print('api_key=stdout-secret', file=sys.stdout)\n"
        "print('stderr-line', file=sys.stderr)\n",
        encoding="utf-8",
    )

    result = SkillScriptRunner(tmp_path).run(
        RunSkillScriptInput(script_path="scripts/ok.py", arguments=["a", "b"])
    )

    assert result.status == "succeeded"
    assert "stdout:a,b" in result.stdout
    assert "stdout-secret" not in result.stdout
    assert "api_key[redacted]" in result.stdout
    assert "stderr-line" in result.stderr


def test_runner_rejects_traversal_and_non_python_paths(tmp_path: Path) -> None:
    runner = SkillScriptRunner(tmp_path)

    traversal = runner.run(RunSkillScriptInput(script_path="../outside.py"))
    non_python = runner.run(RunSkillScriptInput(script_path="SKILL.md"))

    assert traversal.error_code == "script_path_outside_skill"
    assert non_python.error_code == "script_must_be_python"


def test_runner_times_out_and_redacts_secret_like_stderr(tmp_path: Path) -> None:
    script = tmp_path / "slow.py"
    script.write_text(
        "import time\n"
        "print('Bearer abc123', file=__import__('sys').stderr)\n"
        "time.sleep(2)\n",
        encoding="utf-8",
    )

    result = SkillScriptRunner(tmp_path).run(
        RunSkillScriptInput(script_path="slow.py", timeout_seconds=1)
    )

    assert result.status == "failed"
    assert result.error_code == "script_timeout"
    assert "abc123" not in result.stderr
    assert "Bearer [redacted]" in result.stderr
