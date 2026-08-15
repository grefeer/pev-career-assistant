from __future__ import annotations

from pathlib import Path

from backend.app.services.agent_runtime.skill_script_runner import (
    RunSkillScriptInput,
    SkillScriptRunner,
)


def test_runner_executes_only_a_relative_python_script(tmp_path: Path) -> None:
    script = tmp_path / "scripts" / "normalize.py"
    script.parent.mkdir()
    script.write_text(
        "import sys\n"
        "print('stdout:' + ','.join(sys.argv[1:]))\n"
        "print('api_key=stdout-secret', file=sys.stdout)\n"
        "print('stderr-line', file=sys.stderr)\n",
        encoding="utf-8",
    )

    result = SkillScriptRunner(tmp_path).run(
        RunSkillScriptInput(script_path="scripts/normalize.py", arguments=["a", "b"])
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
    missing_but_allowed = runner.run(
        RunSkillScriptInput(script_path="scripts/unknown.py")
    )

    assert traversal.error_code == "script_path_outside_skill"
    assert non_python.error_code == "script_must_be_python"
    assert missing_but_allowed.error_code == "script_not_found"


def test_runner_executes_any_python_file_under_the_skill_dir(tmp_path: Path) -> None:
    script = tmp_path / "anti_crawl" / "helper.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('anti-crawl-ok')\n", encoding="utf-8")

    result = SkillScriptRunner(tmp_path).run(
        RunSkillScriptInput(script_path="anti_crawl/helper.py")
    )

    assert result.status == "succeeded"
    assert "anti-crawl-ok" in result.stdout


def test_runner_honors_narrowed_allowed_script_dirs(tmp_path: Path) -> None:
    allowed = tmp_path / "scripts" / "ok.py"
    allowed.parent.mkdir(parents=True)
    allowed.write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / "tools").mkdir()

    runner = SkillScriptRunner(tmp_path, allowed_script_dirs=("scripts",))

    in_dir = runner.run(RunSkillScriptInput(script_path="scripts/ok.py"))
    out_dir = runner.run(RunSkillScriptInput(script_path="tools/denied.py"))

    assert in_dir.status == "succeeded"
    assert out_dir.error_code == "script_not_allowlisted"


def test_runner_times_out_and_redacts_secret_like_stderr(tmp_path: Path) -> None:
    script = tmp_path / "scripts" / "normalize.py"
    script.parent.mkdir()
    script.write_text(
        "import time\n"
        "print('Bearer abc123', file=__import__('sys').stderr)\n"
        "time.sleep(2)\n",
        encoding="utf-8",
    )

    result = SkillScriptRunner(tmp_path).run(
        RunSkillScriptInput(script_path="scripts/normalize.py", timeout_seconds=1)
    )

    assert result.status == "failed"
    assert result.error_code == "script_timeout"
    assert "abc123" not in result.stderr
    assert "Bearer [redacted]" in result.stderr


def test_runner_does_not_pass_application_secrets_to_scripts(
    tmp_path: Path, monkeypatch
) -> None:
    script = tmp_path / "scripts" / "normalize.py"
    script.parent.mkdir()
    script.write_text(
        "import os\n"
        "print(os.environ.get('APP_AUTH_SECRET', 'missing'))\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("APP_AUTH_SECRET", "should-not-cross-process-boundary")

    result = SkillScriptRunner(tmp_path).run(
        RunSkillScriptInput(script_path="scripts/normalize.py")
    )

    assert result.status == "succeeded"
    assert result.stdout.strip() == "missing"
