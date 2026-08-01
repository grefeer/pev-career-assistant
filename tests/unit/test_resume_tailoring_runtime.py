"""Unit tests for the resume-tailoring runtime (deterministic orchestrator).

The generate/validate scripts are replaced with a fake ``subprocess.run`` that
writes the same output files the real scripts would, so the runtime's result
assembly is exercised without an LLM. Mirrors ``test_company_research_runtime``.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from backend.app.services.resume_tailoring.runtime import ResumeTailoringRuntime


def _settings(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(resume_tailoring_artifact_root=str(tmp_path))


def _patch_scripts(
    monkeypatch,
    *,
    gen_result: dict,
    val_result: dict | None = None,
    write_gen: bool = True,
) -> None:
    """Replace generate/validate subprocesses with fakes that write their output."""

    def fake_run(args, **kwargs):
        cwd = Path(kwargs["cwd"])
        script = Path(args[1]).name
        if script == "generate.py":
            if write_gen:
                draft_dir = cwd / "output" / "evidence"
                draft_dir.mkdir(parents=True, exist_ok=True)
                (draft_dir / "draft_diffs.json").write_text(
                    json.dumps(gen_result, ensure_ascii=False), encoding="utf-8"
                )
            return SimpleNamespace(
                stdout=json.dumps({"status": gen_result.get("status"), "diff_count": len(gen_result.get("diffs", []))}),
                stderr="", returncode=0,
            )
        if script == "validate.py":
            (cwd / "output").mkdir(parents=True, exist_ok=True)
            (cwd / "output" / "validation.json").write_text(
                json.dumps(val_result or {}, ensure_ascii=False), encoding="utf-8"
            )
            return SimpleNamespace(stdout=json.dumps(val_result or {}), stderr="", returncode=0)
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(
        "backend.app.services.job_discovery.skill_runtime.subprocess.run", fake_run
    )


_JOB = {"title": "AI Engineer", "requirements": ["python"]}
_FACTS = {"projects": "AI app", "skills": "python"}
_DIFFS = [{"op": "highlight", "section": "projects", "fact_ref": "projects"}]


def test_run_succeeds_and_carries_diffs(tmp_path: Path, monkeypatch) -> None:
    _patch_scripts(
        monkeypatch,
        gen_result={"status": "ok", "diffs": _DIFFS, "agent_version": "1.0.0"},
        val_result={"status": "ok", "diff_count": 1},
    )
    runtime = ResumeTailoringRuntime(_settings(tmp_path))
    result = runtime.run(
        report_id="r-1", job_snapshot=_JOB, profile_facts=_FACTS,
    )
    assert result.status == "succeeded"
    assert result.succeeded is True
    assert result.diffs == _DIFFS
    assert result.agent_version == "1.0.0"
    assert result.block_reason is None
    assert result.evidence_refs[0]["evidence_type"] == "resume_draft_diffs"


def test_run_succeeds_with_zero_diffs(tmp_path: Path, monkeypatch) -> None:
    _patch_scripts(
        monkeypatch,
        gen_result={"status": "ok", "diffs": [], "agent_version": "1.0.0"},
        val_result={"status": "ok", "diff_count": 0},
    )
    runtime = ResumeTailoringRuntime(_settings(tmp_path))
    result = runtime.run(report_id="r-2", job_snapshot=_JOB, profile_facts=_FACTS)
    assert result.status == "succeeded"
    assert result.diffs == []


def test_run_generation_failed_is_failed(tmp_path: Path, monkeypatch) -> None:
    _patch_scripts(
        monkeypatch,
        gen_result={"status": "failed", "code": "missing_api_key",
                    "last_error": "no key", "agent_version": "1.0.0"},
    )
    runtime = ResumeTailoringRuntime(_settings(tmp_path))
    result = runtime.run(report_id="r-3", job_snapshot=_JOB, profile_facts=_FACTS)
    assert result.status == "failed"
    assert result.last_error == "no key"
    assert result.agent_version == "1.0.0"


def test_run_generation_parse_error_is_failed(tmp_path: Path, monkeypatch) -> None:
    _patch_scripts(
        monkeypatch,
        gen_result={"status": "failed", "code": "draft_generation_parse_error",
                    "last_error": "no json", "agent_version": "1.0.0"},
    )
    runtime = ResumeTailoringRuntime(_settings(tmp_path))
    result = runtime.run(report_id="r-4", job_snapshot=_JOB, profile_facts=_FACTS)
    assert result.status == "failed"
    assert result.last_error == "no json"


def test_run_missing_draft_file_is_failed(tmp_path: Path, monkeypatch) -> None:
    _patch_scripts(
        monkeypatch, gen_result={"status": "ok", "diffs": _DIFFS}, write_gen=False,
    )
    runtime = ResumeTailoringRuntime(_settings(tmp_path))
    result = runtime.run(report_id="r-5", job_snapshot=_JOB, profile_facts=_FACTS)
    assert result.status == "failed"
    assert result.last_error == "missing draft_diffs.json"


def test_run_validation_failure_is_needs_manual_review(tmp_path: Path, monkeypatch) -> None:
    _patch_scripts(
        monkeypatch,
        gen_result={"status": "ok", "diffs": _DIFFS, "agent_version": "1.0.0"},
        val_result={"status": "failed", "code": "draft_validation_invalid_fact_ref", "index": 0,
                    "last_error": "bad ref"},
    )
    runtime = ResumeTailoringRuntime(_settings(tmp_path))
    result = runtime.run(report_id="r-6", job_snapshot=_JOB, profile_facts=_FACTS)
    assert result.status == "needs_manual_review"
    assert result.succeeded is False
    assert result.block_reason == "invalid_diff"
    assert result.validation_code == "draft_validation_invalid_fact_ref"
    assert result.validation_index == 0
    # Diffs are preserved so the human can fix them in place.
    assert result.diffs == _DIFFS


def test_run_skips_validation_when_disabled(tmp_path: Path, monkeypatch) -> None:
    _patch_scripts(
        monkeypatch,
        gen_result={"status": "ok", "diffs": _DIFFS, "agent_version": "1.0.0"},
        val_result={"status": "failed", "code": "draft_validation_invalid_op"},
    )
    runtime = ResumeTailoringRuntime(_settings(tmp_path))
    result = runtime.run(report_id="r-7", job_snapshot=_JOB, profile_facts=_FACTS, validate=False)
    assert result.status == "succeeded"
    assert result.diffs == _DIFFS


def test_run_invoke_exception_is_failed(tmp_path: Path, monkeypatch) -> None:
    runtime = ResumeTailoringRuntime(_settings(tmp_path))

    def boom(*, skill_dir: Path, validate: bool) -> None:
        raise RuntimeError("generate exploded")

    runtime._invoke = boom  # type: ignore[method-assign]
    result = runtime.run(report_id="r-8", job_snapshot=_JOB, profile_facts=_FACTS)
    assert result.status == "failed"
    assert result.last_error == "generate exploded"


def test_run_artifact_publish_failure_is_needs_manual_review(
    tmp_path: Path, monkeypatch,
) -> None:
    _patch_scripts(
        monkeypatch,
        gen_result={"status": "ok", "diffs": _DIFFS, "agent_version": "1.0.0"},
        val_result={"status": "ok", "diff_count": 1},
    )

    class FailingStore:
        def put(self, **kwargs):
            raise OSError("minio down")

    runtime = ResumeTailoringRuntime(_settings(tmp_path), object_store=FailingStore())
    result = runtime.run(report_id="r-9", job_snapshot=_JOB, profile_facts=_FACTS)
    assert result.status == "needs_manual_review"
    assert result.block_reason == "artifact_error"
    assert result.diffs == _DIFFS  # preserved for the manual reviewer


def test_run_publishes_evidence_when_object_store_present(
    tmp_path: Path, monkeypatch,
) -> None:
    _patch_scripts(
        monkeypatch,
        gen_result={"status": "ok", "diffs": _DIFFS, "agent_version": "1.0.0"},
        val_result={"status": "ok", "diff_count": 1},
    )

    class CollectingStore:
        def __init__(self) -> None:
            self.keys: list[str] = []

        def put(self, *, key: str, plaintext: bytes, content_type: str):
            self.keys.append(key)
            return object()

    store = CollectingStore()
    runtime = ResumeTailoringRuntime(_settings(tmp_path), object_store=store)
    result = runtime.run(report_id="r-10", job_snapshot=_JOB, profile_facts=_FACTS)
    assert result.status == "succeeded"
    assert any(key.startswith("resume-tailoring/r-10/") for key in store.keys)
