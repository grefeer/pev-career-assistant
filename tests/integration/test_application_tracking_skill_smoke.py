"""End-to-end smoke test for the application-tracking SKILL.

Unlike the LLM-driven skill smokes (resume-tailoring / interview-prep), this
skill is deterministic and stdlib-only: no LLM, no network, no credentials. So
the smoke runs unconditionally and exercises the real ``track.py`` script
through the real ``run_skill_script`` tool against a cloned skill directory -
the exact path an agent takes. It is the "does the skill actually land
end-to-end" check the importlib unit tests cannot cover (they never spawn the
subprocess or clone the source).
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from backend.app.services.job_discovery.skill_artifacts import SkillArtifactStore
from backend.app.services.job_discovery.skill_runtime import (
    SkillToolPolicy,
    _script_tool,
)
from backend.app.services.job_discovery.skill_spec import get_skill_spec


def _invoke(tool, cli_args: str) -> dict:
    raw = tool.invoke({"script": "track", "cli_args": cli_args})
    assert not raw.startswith("ERROR:"), f"track {cli_args!r} failed: {raw}"
    # The tool returns stdout (+ optional stderr tail); the script prints one JSON line.
    return json.loads(raw.splitlines()[0])


def test_skill_track_runs_end_to_end_through_run_skill_script(tmp_path: Path) -> None:
    """The real track.py runs through the allowlisted script tool on a cloned skill dir."""
    spec = get_skill_spec("application-tracking")
    store = SkillArtifactStore(
        "smoke-1",
        tmp_path,
        run_id=uuid4().hex,
        skill_name=spec.name,
        skill_source=spec.source_path,
    )
    skill_dir = store.prepare()
    assert (skill_dir / "scripts" / "track.py").is_file()

    tool = _script_tool(
        skill_dir, SkillToolPolicy(script_timeout_seconds=30),
        allowed_scripts=spec.allowed_scripts,
    )

    # 1. validate-transition: a legal forward move.
    result = _invoke(tool, "validate-transition --from screening --to interview")
    assert result == {
        "status": "ok",
        "valid": True,
        "from": "screening",
        "to": "interview",
        "from_terminal": False,
        "to_terminal": False,
        "reason": "legal transition",
    }

    # 2. validate-transition: an illegal skip (saved -> offer).
    result = _invoke(tool, "validate-transition --from saved --to offer")
    assert result["valid"] is False
    assert "saved -> offer" in result["reason"]

    # 3. allowed-transitions: terminal offer can only go to withdrawn.
    result = _invoke(tool, "allowed-transitions --status offer")
    assert result["terminal"] is True
    assert result["transitions"] == ["withdrawn"]

    # 4. normalize-status: case-insensitive normalization (uppercase -> lowercase).
    result = _invoke(tool, "normalize-status --status INTERVIEW")
    assert result["normalized"] == "interview"
    assert result["valid"] is True

    # 5. list-statuses: full lifecycle + terminal split.
    result = _invoke(tool, "list-statuses")
    assert result["statuses"] == [
        "saved", "applied", "screening", "interview", "offer", "rejected", "withdrawn",
    ]
    assert result["terminal"] == ["offer", "rejected", "withdrawn"]
    assert result["non_terminal"] == ["saved", "applied", "screening", "interview"]

    # 6. --out persists the full result under output/ (audit artifact).
    out_result = _invoke(
        tool, "validate-transition --from saved --to applied --out output/evidence/t.json"
    )
    assert out_result["valid"] is True
    persisted = json.loads((skill_dir / "output" / "evidence" / "t.json").read_text(encoding="utf-8"))
    assert persisted["valid"] is True
    assert persisted["from"] == "saved"

    # 7. unknown status degrades to a structured error (exit 0, no crash).
    err_result = _invoke(tool, "allowed-transitions --status onboarding")
    assert err_result["status"] == "error"
    assert err_result["code"] == "unknown_status"


def test_skill_track_rejects_an_unallowed_script(tmp_path: Path) -> None:
    """A script outside application-tracking's allowlist is refused (security)."""
    spec = get_skill_spec("application-tracking")
    store = SkillArtifactStore(
        "smoke-2",
        tmp_path,
        run_id=uuid4().hex,
        skill_name=spec.name,
        skill_source=spec.source_path,
    )
    skill_dir = store.prepare()
    tool = _script_tool(
        skill_dir, SkillToolPolicy(script_timeout_seconds=30),
        allowed_scripts=spec.allowed_scripts,
    )

    raw = tool.invoke({"script": "browse", "cli_args": "https://example.com --out output/evidence"})
    assert raw == "ERROR: unsupported Skill script 'browse'"
