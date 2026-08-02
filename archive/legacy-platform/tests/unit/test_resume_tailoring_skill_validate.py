"""Unit tests for the resume-tailoring ``validate.py`` skill script.

Loaded as an importable module (mirrors ``test_company_research_browse``). The
validation logic is pure and is tested directly; ``main`` is exercised via the
CLI with temp files.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_VAL_PATH = (
    Path(__file__).resolve().parents[2]
    / "skill"
    / "resume-tailoring"
    / "scripts"
    / "validate.py"
)


@pytest.fixture(scope="module")
def val():
    spec = importlib.util.spec_from_file_location("resume_tailoring_validate", _VAL_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


_FACTS = {"projects": "AI app", "skills": "python"}
_EVIDENCE = {"projects": ["e1", "e2"], "skills": ["e3"]}


# ═══════════════════════════════════════════════════════════════════
# validate_diffs - happy path + every error code
# ═══════════════════════════════════════════════════════════════════

def test_validate_ok_returns_diffs(val):
    diffs = [
        {"op": "highlight", "section": "projects", "fact_ref": "projects", "evidence_ids": ["e1"]},
        {"op": "omit", "section": "skills", "fact_ref": "skills"},
    ]
    assert val.validate_diffs(diffs, _FACTS, _EVIDENCE) is diffs


def test_validate_ok_without_evidence(val):
    diffs = [{"op": "reorder", "section": "projects", "fact_ref": "projects"}]
    assert val.validate_diffs(diffs, _FACTS, None) is diffs


def test_validate_missing_op(val):
    with pytest.raises(val.DraftValidationError) as exc:
        val.validate_diffs([{"section": "projects", "fact_ref": "projects"}], _FACTS, None)
    assert exc.value.error_code == "draft_validation_missing_op"
    assert exc.value.index == 0


def test_validate_invalid_op(val):
    with pytest.raises(val.DraftValidationError) as exc:
        val.validate_diffs([{"op": "bogus", "section": "x", "fact_ref": "projects"}], _FACTS, None)
    assert exc.value.error_code == "draft_validation_invalid_op"


def test_validate_empty_section(val):
    with pytest.raises(val.DraftValidationError) as exc:
        val.validate_diffs([{"op": "highlight", "section": "", "fact_ref": "projects"}], _FACTS, None)
    assert exc.value.error_code == "draft_validation_empty_section"


def test_validate_missing_section(val):
    with pytest.raises(val.DraftValidationError) as exc:
        val.validate_diffs([{"op": "highlight", "fact_ref": "projects"}], _FACTS, None)
    assert exc.value.error_code == "draft_validation_empty_section"


def test_validate_invalid_fact_ref(val):
    with pytest.raises(val.DraftValidationError) as exc:
        val.validate_diffs([{"op": "highlight", "section": "x", "fact_ref": "nope"}], _FACTS, None)
    assert exc.value.error_code == "draft_validation_invalid_fact_ref"


def test_validate_invalid_evidence(val):
    with pytest.raises(val.DraftValidationError) as exc:
        val.validate_diffs(
            [{"op": "highlight", "section": "projects", "fact_ref": "projects", "evidence_ids": ["unknown"]}],
            _FACTS, _EVIDENCE,
        )
    assert exc.value.error_code == "draft_validation_invalid_evidence"


def test_validate_skips_falsy_evidence_lists(val):
    # An evidence_refs value that is an empty list is skipped (no valid ids added),
    # so a diff that cites NO evidence_ids still validates.
    diffs = [{"op": "highlight", "section": "projects", "fact_ref": "projects"}]
    val.validate_diffs(diffs, _FACTS, {"projects": []})


def test_validate_reports_first_failure_index(val):
    diffs = [
        {"op": "highlight", "section": "projects", "fact_ref": "projects"},
        {"op": "bogus", "section": "x", "fact_ref": "projects"},
    ]
    with pytest.raises(val.DraftValidationError) as exc:
        val.validate_diffs(diffs, _FACTS, None)
    assert exc.value.index == 1


# ═══════════════════════════════════════════════════════════════════
# main() end-to-end
# ═══════════════════════════════════════════════════════════════════

def _write(tmp_path: Path, name: str, data) -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def test_main_valid(val, tmp_path, capsys):
    diffs = _write(tmp_path, "diffs.json", {"diffs": [{"op": "highlight", "section": "projects", "fact_ref": "projects"}]})
    facts = _write(tmp_path, "facts.json", _FACTS)
    evidence = _write(tmp_path, "ev.json", _EVIDENCE)
    out = tmp_path / "validation.json"

    rc = val.main([
        "--input", str(diffs), "--facts", str(facts), "--evidence", str(evidence), "--out", str(out),
    ])

    assert rc == 0
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written == {"status": "ok", "diff_count": 1}
    assert json.loads(capsys.readouterr().out)["status"] == "ok"


def test_main_reads_diffs_from_stdin(val, tmp_path, monkeypatch, capsys):
    facts = _write(tmp_path, "facts.json", _FACTS)
    out = tmp_path / "validation.json"
    monkeypatch.setattr("sys.stdin", type("S", (), {"read": lambda self: json.dumps([{"op": "omit", "section": "skills", "fact_ref": "skills"}])})())

    rc = val.main(["--facts", str(facts), "--out", str(out)])

    assert rc == 0
    assert json.loads(out.read_text(encoding="utf-8"))["status"] == "ok"


def test_main_validation_failure_reports_code(val, tmp_path, capsys):
    diffs = _write(tmp_path, "diffs.json", [{"op": "bogus", "section": "x", "fact_ref": "projects"}])
    facts = _write(tmp_path, "facts.json", _FACTS)
    out = tmp_path / "validation.json"

    rc = val.main(["--input", str(diffs), "--facts", str(facts), "--out", str(out)])

    assert rc == 0
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["status"] == "failed"
    assert written["code"] == "draft_validation_invalid_op"
    assert written["index"] == 0


def test_main_bad_diffs_shape_is_bad_input(val, tmp_path, capsys):
    diffs = _write(tmp_path, "diffs.json", {"not_a_list": True})
    facts = _write(tmp_path, "facts.json", _FACTS)
    out = tmp_path / "validation.json"

    rc = val.main(["--input", str(diffs), "--facts", str(facts), "--out", str(out)])

    assert rc == 0
    assert json.loads(out.read_text(encoding="utf-8"))["code"] == "bad_input"


def test_main_facts_not_object_is_bad_input(val, tmp_path, capsys):
    diffs = _write(tmp_path, "diffs.json", [{"op": "highlight", "section": "x", "fact_ref": "projects"}])
    facts = _write(tmp_path, "facts.json", ["not", "an", "object"])
    out = tmp_path / "validation.json"

    rc = val.main(["--input", str(diffs), "--facts", str(facts), "--out", str(out)])

    assert rc == 0
    assert json.loads(out.read_text(encoding="utf-8"))["code"] == "bad_input"


def test_main_evidence_not_object_is_bad_input(val, tmp_path, capsys):
    diffs = _write(tmp_path, "diffs.json", [{"op": "highlight", "section": "x", "fact_ref": "projects"}])
    facts = _write(tmp_path, "facts.json", _FACTS)
    evidence = _write(tmp_path, "ev.json", ["not", "an", "object"])
    out = tmp_path / "validation.json"

    rc = val.main(["--input", str(diffs), "--facts", str(facts), "--evidence", str(evidence), "--out", str(out)])

    assert rc == 0
    assert json.loads(out.read_text(encoding="utf-8"))["code"] == "bad_input"


def test_main_missing_facts_file_is_bad_input(val, tmp_path, capsys):
    diffs = _write(tmp_path, "diffs.json", [{"op": "highlight", "section": "x", "fact_ref": "projects"}])
    out = tmp_path / "validation.json"

    rc = val.main(["--input", str(diffs), "--facts", str(tmp_path / "nope.json"), "--out", str(out)])

    assert rc == 0
    assert json.loads(out.read_text(encoding="utf-8"))["code"] == "bad_input"
