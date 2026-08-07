"""Whitelisted execution channel for the job-discovery skill scripts.

Security: this is the ONLY way skill scripts may run (spec §4.2).  It is
deliberately NOT LocalShellBackend (which grants arbitrary shell): only the
nine allowlisted scripts run, cwd is pinned to the skill directory so
relative ``output/`` paths resolve, and an injectable ``runner`` seam keeps
unit tests deterministic.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[6]
SKILL_DIR = _PROJECT_ROOT / "skill" / "job-discovery"

_ALLOWED_SCRIPTS = frozenset(
    {
        "browse",
        "validate",
        "normalize",
        "deduplicate",
        "ocr_image",
        "state",
        "read_evidence",
        "write_candidates",
        "coverage_gate",
    }
)
_SCRIPT_TIMEOUT_SEC = 900

# runner: (script_path, parts, *, cwd, stdin, timeout) -> stdout text
_ScriptRunner = Callable[[Path, list[str], Path, str | None, int], str]


def _default_runner(
    script_path: Path,
    parts: list[str],
    *,
    cwd: Path,
    stdin: str | None,
    timeout: int,
) -> str:
    cmd = [sys.executable, str(script_path), *parts]
    child_env = {
        **os.environ,
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
    }
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
        input=stdin,
        env=child_env,
    )
    out = proc.stdout or ""
    if proc.stderr:
        out += "\n[stderr]\n" + proc.stderr[-2000:]
    return out


def run_skill_script(
    script: str,
    cli_args: str = "",
    stdin: str = "",
    *,
    runner: _ScriptRunner | None = None,
    cwd: Path | str | None = None,
) -> str:
    """Run one allowlisted skill script; never raises, returns stdout/error.

    ``cwd`` (default ``SKILL_DIR``) redirects the script's own working
    directory — the run-scoped ``state_dir`` for ``state.py check/mark`` so
    each eval run writes its own ``output/state.json`` instead of the shared
    skill default.  Script paths always resolve under ``SKILL_DIR/scripts``.
    """
    if script not in _ALLOWED_SCRIPTS:
        return f"ERROR: script not allowed: {script}"
    script_path = SKILL_DIR / "scripts" / f"{script}.py"
    if not script_path.exists():
        return f"ERROR: script not found at {script_path}"
    try:
        parts = shlex.split(cli_args, posix=(os.name != "nt")) if cli_args else []
    except ValueError as exc:
        return f"ERROR: could not parse cli_args {cli_args!r}: {exc}"
    resolved_cwd = Path(cwd) if cwd is not None else SKILL_DIR
    try:
        return (runner or _default_runner)(
            script_path,
            parts,
            cwd=resolved_cwd,
            stdin=stdin if stdin else None,
            timeout=_SCRIPT_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        return f"ERROR: {script} timed out after {_SCRIPT_TIMEOUT_SEC}s"
    except OSError as exc:
        return f"ERROR: {script} could not start: {exc}"
