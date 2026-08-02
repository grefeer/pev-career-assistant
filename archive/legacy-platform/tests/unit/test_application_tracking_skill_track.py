"""Unit tests for the application-tracking ``track.py`` skill script.

Loaded as an importable module (mirrors ``test_*_skill_generate``). The script
is stdlib-only (no LLM, no network), so every subcommand is exercised directly
through ``main()`` and the pure state-machine helpers directly.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_TRACK_PATH = (
    Path(__file__).resolve().parents[2]
    / "skill"
    / "application-tracking"
    / "scripts"
    / "track.py"
)


@pytest.fixture(scope="module")
def track():
    spec = importlib.util.spec_from_file_location("application_tracking_track", _TRACK_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# ═══════════════════════════════════════════════════════════════════
# normalize_status
# ═══════════════════════════════════════════════════════════════════

def test_normalize_status_canonical(track):
    assert track.normalize_status("interview") == "interview"


def test_normalize_status_case_insensitive_and_trimmed(track):
    assert track.normalize_status(" Interview ") == "interview"
    assert track.normalize_status("INTERVIEW") == "interview"


def test_normalize_status_unknown_returns_none(track):
    assert track.normalize_status("onboarding") is None


def test_normalize_status_empty_and_blank_return_none(track):
    assert track.normalize_status("") is None
    assert track.normalize_status("   ") is None


def test_normalize_status_non_string_returns_none(track):
    assert track.normalize_status(None) is None  # type: ignore[arg-type]
    assert track.normalize_status(123) is None  # type: ignore[arg-type]


# ═══════════════════════════════════════════════════════════════════
# is_terminal / allowed_transitions / is_valid_transition
# ═══════════════════════════════════════════════════════════════════

def test_is_terminal_for_each_status(track):
    assert track.is_terminal("offer") is True
    assert track.is_terminal("rejected") is True
    assert track.is_terminal("withdrawn") is True
    assert track.is_terminal("saved") is False
    assert track.is_terminal("applied") is False
    assert track.is_terminal("screening") is False
    assert track.is_terminal("interview") is False


def test_is_terminal_unknown_is_false(track):
    assert track.is_terminal("onboarding") is False


def test_allowed_transitions_for_pipeline_states(track):
    assert track.allowed_transitions("saved") == frozenset({"applied", "withdrawn"})
    assert track.allowed_transitions("applied") == frozenset(
        {"screening", "rejected", "withdrawn"}
    )
    assert track.allowed_transitions("screening") == frozenset(
        {"interview", "rejected", "withdrawn"}
    )
    assert track.allowed_transitions("interview") == frozenset(
        {"offer", "rejected", "withdrawn"}
    )
    assert track.allowed_transitions("offer") == frozenset({"withdrawn"})


def test_allowed_transitions_terminal_is_empty(track):
    assert track.allowed_transitions("rejected") == frozenset()
    assert track.allowed_transitions("withdrawn") == frozenset()


def test_allowed_transitions_unknown_is_empty(track):
    assert track.allowed_transitions("onboarding") == frozenset()


def test_is_valid_transition_truth_table(track):
    # legal forward moves
    assert track.is_valid_transition("saved", "applied") is True
    assert track.is_valid_transition("screening", "interview") is True
    assert track.is_valid_transition("interview", "offer") is True
    assert track.is_valid_transition("offer", "withdrawn") is True
    # withdrawn from anywhere non-terminal + offer
    assert track.is_valid_transition("saved", "withdrawn") is True
    assert track.is_valid_transition("applied", "withdrawn") is True
    assert track.is_valid_transition("interview", "withdrawn") is True
    # illegal skips / backward / out-of-terminal
    assert track.is_valid_transition("saved", "offer") is False
    assert track.is_valid_transition("applied", "saved") is False
    assert track.is_valid_transition("offer", "applied") is False
    assert track.is_valid_transition("rejected", "withdrawn") is False


# ═══════════════════════════════════════════════════════════════════
# main(): validate-transition
# ═══════════════════════════════════════════════════════════════════

def _run(track, argv, capsys):
    rc = track.main(argv)
    out = capsys.readouterr().out
    return rc, json.loads(out)


def test_main_validate_transition_legal(track, capsys):
    rc, result = _run(
        track, ["validate-transition", "--from", "screening", "--to", "interview"], capsys
    )
    assert rc == 0
    assert result == {
        "status": "ok",
        "valid": True,
        "from": "screening",
        "to": "interview",
        "from_terminal": False,
        "to_terminal": False,
        "reason": "legal transition",
    }


def test_main_validate_transition_illegal_skip(track, capsys):
    rc, result = _run(
        track, ["validate-transition", "--from", "saved", "--to", "offer"], capsys
    )
    assert rc == 0
    assert result["status"] == "ok"
    assert result["valid"] is False
    assert result["from"] == "saved"
    assert result["to"] == "offer"
    assert "saved -> offer" in result["reason"]


def test_main_validate_transition_out_of_terminal(track, capsys):
    rc, result = _run(
        track, ["validate-transition", "--from", "offer", "--to", "applied"], capsys
    )
    assert rc == 0
    assert result["valid"] is False
    assert result["from_terminal"] is True


def test_main_validate_transition_unknown_from(track, capsys):
    rc, result = _run(
        track, ["validate-transition", "--from", "onboarding", "--to", "applied"], capsys
    )
    assert rc == 0
    assert result == {
        "status": "error",
        "code": "unknown_from_status",
        "message": "unknown source status: 'onboarding'",
    }


def test_main_validate_transition_unknown_to(track, capsys):
    rc, result = _run(
        track, ["validate-transition", "--from", "saved", "--to", "onboarding"], capsys
    )
    assert rc == 0
    assert result["status"] == "error"
    assert result["code"] == "unknown_to_status"


def test_main_validate_transition_writes_out_file(track, tmp_path, capsys):
    out_file = tmp_path / "output" / "evidence" / "transition.json"
    rc, result = _run(
        track,
        [
            "validate-transition", "--from", "saved", "--to", "applied",
            "--out", str(out_file),
        ],
        capsys,
    )
    assert rc == 0
    assert result["valid"] is True
    written = json.loads(out_file.read_text(encoding="utf-8"))
    assert written["valid"] is True
    assert written["from"] == "saved"


# ═══════════════════════════════════════════════════════════════════
# main(): allowed-transitions
# ═══════════════════════════════════════════════════════════════════

def test_main_allowed_transitions_pipeline(track, capsys):
    rc, result = _run(track, ["allowed-transitions", "--status", "screening"], capsys)
    assert rc == 0
    assert result == {
        "status": "ok",
        "status_value": "screening",
        "terminal": False,
        "transitions": ["interview", "rejected", "withdrawn"],
    }


def test_main_allowed_transitions_terminal_offer(track, capsys):
    rc, result = _run(track, ["allowed-transitions", "--status", "offer"], capsys)
    assert rc == 0
    assert result["terminal"] is True
    assert result["transitions"] == ["withdrawn"]


def test_main_allowed_transitions_terminal_rejected_empty(track, capsys):
    rc, result = _run(track, ["allowed-transitions", "--status", "rejected"], capsys)
    assert rc == 0
    assert result["terminal"] is True
    assert result["transitions"] == []


def test_main_allowed_transitions_unknown(track, capsys):
    rc, result = _run(track, ["allowed-transitions", "--status", "onboarding"], capsys)
    assert rc == 0
    assert result == {
        "status": "error",
        "code": "unknown_status",
        "message": "unknown status: 'onboarding'",
    }


def test_main_allowed_transitions_normalizes_input(track, capsys):
    rc, result = _run(track, ["allowed-transitions", "--status", " INTERVIEW "], capsys)
    assert rc == 0
    assert result["status_value"] == "interview"


# ═══════════════════════════════════════════════════════════════════
# main(): normalize-status
# ═══════════════════════════════════════════════════════════════════

def test_main_normalize_status_valid(track, capsys):
    rc, result = _run(track, ["normalize-status", "--status", " Interview "], capsys)
    assert rc == 0
    assert result == {
        "status": "ok",
        "input": " Interview ",
        "normalized": "interview",
        "valid": True,
    }


def test_main_normalize_status_invalid(track, capsys):
    rc, result = _run(track, ["normalize-status", "--status", "onboarding"], capsys)
    assert rc == 0
    assert result["normalized"] is None
    assert result["valid"] is False


# ═══════════════════════════════════════════════════════════════════
# main(): list-statuses
# ═══════════════════════════════════════════════════════════════════

def test_main_list_statuses(track, capsys):
    rc, result = _run(track, ["list-statuses"], capsys)
    assert rc == 0
    assert result["statuses"] == [
        "saved", "applied", "screening", "interview", "offer", "rejected", "withdrawn",
    ]
    assert result["terminal"] == ["offer", "rejected", "withdrawn"]
    assert result["non_terminal"] == ["saved", "applied", "screening", "interview"]


def test_main_list_statuses_writes_out_file(track, tmp_path, capsys):
    out_file = tmp_path / "output" / "statuses.json"
    rc, result = _run(track, ["list-statuses", "--out", str(out_file)], capsys)
    assert rc == 0
    written = json.loads(out_file.read_text(encoding="utf-8"))
    assert len(written["statuses"]) == 7


# ═══════════════════════════════════════════════════════════════════
# main(): argparse guard
# ═══════════════════════════════════════════════════════════════════

def test_main_without_subcommand_exits(track):
    # subparsers(required=True) prints usage and exits 2.
    with pytest.raises(SystemExit) as exc:
        track.main([])
    assert exc.value.code == 2


def test_main_missing_required_arg_exits(track):
    with pytest.raises(SystemExit) as exc:
        track.main(["validate-transition", "--from", "saved"])  # --to missing
    assert exc.value.code == 2
