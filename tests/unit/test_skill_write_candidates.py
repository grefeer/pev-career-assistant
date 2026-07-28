from __future__ import annotations

import importlib.util
from pathlib import Path


_SCRIPT = Path(__file__).resolve().parents[2] / "skill" / "job-discovery" / "scripts" / "write_candidates.py"
_SPEC = importlib.util.spec_from_file_location("skill_write_candidates", _SCRIPT)
assert _SPEC and _SPEC.loader
_WRITER = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_WRITER)


def test_writer_accepts_a_candidate_with_a_real_jd_body() -> None:
    assert _WRITER._valid_candidate({
        "title": "算法工程师", "company_name": "示例公司", "responsibilities": "负责模型训练",
    })


def test_writer_rejects_title_only_rows_even_when_they_look_like_jobs() -> None:
    assert not _WRITER._valid_candidate({
        "title": "算法工程师", "company_name": "示例公司",
    })


def test_writer_rejects_rows_missing_the_company_identity() -> None:
    assert not _WRITER._valid_candidate({
        "title": "算法工程师", "responsibilities": "负责模型训练",
    })


def test_writer_refuses_non_page_candidate_output_path(monkeypatch, capsys, tmp_path: Path) -> None:
    monkeypatch.setattr(_WRITER.sys, "argv", ["write_candidates.py", "--out", "output/candidates/temp_fix.json"])
    monkeypatch.setattr(_WRITER.sys, "stdin", type("Input", (), {"read": lambda self: "[]"})())
    monkeypatch.setattr(_WRITER, "_SKILL_ROOT", tmp_path)
    monkeypatch.setattr(_WRITER, "_ALLOWED_ROOT", tmp_path / "output")

    assert _WRITER.main() == 0
    assert "must be output/candidates/page_NN.json" in capsys.readouterr().out
