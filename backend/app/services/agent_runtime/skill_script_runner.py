"""Bounded execution of Python helpers owned by the active Skill package."""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

from pydantic import BaseModel, Field, field_validator


_MAX_OUTPUT_CHARS = 12_000
_MAX_ARGUMENTS = 32
_SECRET_RE = re.compile(
    r"(?i)(bearer\s+|api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|authorization)"
    r"\s*[:=]?\s*([^\s,;]+)"
)


class RunSkillScriptInput(BaseModel):
    """A relative Python helper path and bounded command-line arguments."""

    model_config = {"extra": "forbid"}

    script_path: str = Field(min_length=1, max_length=240)
    arguments: list[str] = Field(default_factory=list, max_length=_MAX_ARGUMENTS)
    timeout_seconds: int = Field(default=30, ge=1, le=120)

    @field_validator("script_path")
    @classmethod
    def normalize_script_path(cls, value: str) -> str:
        return value.strip().replace("\\", "/")

    @field_validator("arguments")
    @classmethod
    def normalize_arguments(cls, values: list[str]) -> list[str]:
        if any("\x00" in value for value in values):
            raise ValueError("arguments must not contain NUL bytes")
        return values


class RunSkillScriptOutput(BaseModel):
    """Safe, bounded subprocess result returned to the active Agent."""

    model_config = {"extra": "forbid"}

    status: str
    script_path: str
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    error_code: str | None = None


class SkillScriptRunner:
    """Execute only Python files below one provisioned Skill directory."""

    def __init__(self, skill_dir: Path, *, python_executable: str | None = None) -> None:
        self._skill_dir = skill_dir.resolve()
        self._python_executable = python_executable or sys.executable

    @property
    def skill_dir(self) -> Path:
        return self._skill_dir

    def run(self, payload: RunSkillScriptInput) -> RunSkillScriptOutput:
        relative_path = Path(payload.script_path)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            return self._failure(payload.script_path, "script_path_outside_skill")
        if relative_path.suffix.lower() != ".py":
            return self._failure(payload.script_path, "script_must_be_python")

        script = (self._skill_dir / relative_path).resolve()
        if not _is_within(script, self._skill_dir):
            return self._failure(payload.script_path, "script_path_outside_skill")
        if not script.is_file():
            return self._failure(payload.script_path, "script_not_found")

        environment = os.environ.copy()
        environment["PYTHONUTF8"] = "1"
        # Make package-local imports work for helpers under scripts/ while
        # keeping the process rooted at the active Skill directory. Existing
        # caller paths are intentionally not inherited into the subprocess.
        environment["PYTHONPATH"] = str(self._skill_dir)
        process: subprocess.Popen[str] | None = None
        try:
            process = subprocess.Popen(
                [self._python_executable, str(script), *payload.arguments],
                cwd=self._skill_dir,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
            stdout, stderr = process.communicate(timeout=payload.timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            if process is not None:
                _terminate_process_tree(process.pid)
                stdout, stderr = process.communicate()
            else:
                stdout, stderr = exc.stdout, exc.stderr
            return RunSkillScriptOutput(
                status="failed",
                script_path=payload.script_path,
                stdout=_redact(_bounded_text(stdout)),
                stderr=_redact(_bounded_text(stderr)),
                timed_out=True,
                error_code="script_timeout",
            )
        except OSError:
            return self._failure(payload.script_path, "script_launch_failed")

        return RunSkillScriptOutput(
            status="succeeded" if process is not None and process.returncode == 0 else "failed",
            script_path=payload.script_path,
            exit_code=process.returncode if process is not None else None,
            stdout=_redact(_bounded_text(stdout)),
            stderr=_redact(_bounded_text(stderr)),
            error_code=None if process is not None and process.returncode == 0 else "script_exit_nonzero",
        )

    @staticmethod
    def _failure(script_path: str, error_code: str) -> RunSkillScriptOutput:
        return RunSkillScriptOutput(
            status="failed",
            script_path=script_path,
            error_code=error_code,
        )


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _bounded_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    if len(value) <= _MAX_OUTPUT_CHARS:
        return value
    return value[:_MAX_OUTPUT_CHARS] + "\n[output truncated]"


def _redact(value: str) -> str:
    return _SECRET_RE.sub(lambda match: f"{match.group(1)}[redacted]", value)


def _terminate_process_tree(pid: int) -> None:
    """Terminate a helper and browser descendants on Windows."""
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            check=False,
        )
    else:
        try:
            os.killpg(pid, 9)
        except (OSError, ProcessLookupError):
            pass
