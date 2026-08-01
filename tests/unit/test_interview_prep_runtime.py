"""Unit tests for the interview-prep runtime (deterministic orchestrator).

The generate script is replaced with a fake ``subprocess.run`` that writes the
same output file the real script would, so the runtime's result assembly is
exercised without an LLM. Mirrors ``test_resume_tailoring_runtime`` (simpler:
no validate step - generation success is the only success path).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from backend.app.services.interview_prep.runtime import InterviewPrepRuntime


def _settings(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(interview_prep_artifact_root=str(tmp_path))


def _patch_scripts(monkeypatch, *, gen_result: dict, write_gen: bool = True) -> None:
    """Replace the generate subprocess with a fake that writes its output file."""

    def fake_run(args, **kwargs):
        cwd = Path(kwargs["cwd"])
        script = Path(args[1]).name
        if script == "generate.py":
            if write_gen:
                kit_dir = cwd / "output" / "evidence"
                kit_dir.mkdir(parents=True, exist_ok=True)
                (kit_dir / "prep_kit.json").write_text(
                    json.dumps(gen_result, ensure_ascii=False), encoding="utf-8"
                )
            return SimpleNamespace(
                stdout=json.dumps(
                    {"status": gen_result.get("status"), "section_count": 0}
                ),
                stderr="",
                returncode=0,
            )
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(
        "backend.app.services.job_discovery.skill_runtime.subprocess.run", fake_run
    )


_JOB = {"title": "AI Engineer", "requirements": ["python"]}
_FACTS = {"projects": "AI app", "skills": "python"}
_CONTENT = {
    "technical_questions": ["q1", "q2"],
    "behavioral_questions": [],
    "talking_points": ["strength"],
    "topics_to_review": [],
    "questions_to_ask": ["a1"],
}


def test_run_succeeds_and_carries_content(tmp_path: Path, monkeypatch) -> None:
    _patch_scripts(
        monkeypatch,
        gen_result={"status": "ok", "content": _CONTENT, "agent_version": "1.0.0"},
    )
    runtime = InterviewPrepRuntime(_settings(tmp_path))
    result = runtime.run(
        report_id="r-1", job_snapshot=_JOB, profile_facts=_FACTS,
    )
    assert result.status == "succeeded"
    assert result.succeeded is True
    assert result.content == _CONTENT
    assert result.agent_version == "1.0.0"
    assert result.block_reason is None
    assert result.evidence_refs[0]["evidence_type"] == "interview_prep_kit"
    assert "4 item(s)" in result.summary  # q1,q2,strength,a1 = 4


def test_run_succeeds_with_zero_sections(tmp_path: Path, monkeypatch) -> None:
    # LLM produced content that normalized to all-empty would have raised
    # empty_content; emulate a minimal-but-non-empty kit (one section only).
    _patch_scripts(
        monkeypatch,
        gen_result={
            "status": "ok",
            "content": {"technical_questions": ["only-q"]},
            "agent_version": "1.0.0",
        },
    )
    runtime = InterviewPrepRuntime(_settings(tmp_path))
    result = runtime.run(report_id="r-2", job_snapshot=_JOB, profile_facts=_FACTS)
    assert result.status == "succeeded"
    assert result.content["technical_questions"] == ["only-q"]


def test_run_generation_failed_is_failed(tmp_path: Path, monkeypatch) -> None:
    _patch_scripts(
        monkeypatch,
        gen_result={
            "status": "failed",
            "code": "missing_api_key",
            "last_error": "no key",
            "agent_version": "1.0.0",
        },
    )
    runtime = InterviewPrepRuntime(_settings(tmp_path))
    result = runtime.run(report_id="r-3", job_snapshot=_JOB, profile_facts=_FACTS)
    assert result.status == "failed"
    assert result.last_error == "no key"
    assert result.agent_version == "1.0.0"


def test_run_generation_parse_error_is_failed(tmp_path: Path, monkeypatch) -> None:
    _patch_scripts(
        monkeypatch,
        gen_result={
            "status": "failed",
            "code": "interview_prep_parse_error",
            "last_error": "no json",
            "agent_version": "1.0.0",
        },
    )
    runtime = InterviewPrepRuntime(_settings(tmp_path))
    result = runtime.run(report_id="r-4", job_snapshot=_JOB, profile_facts=_FACTS)
    assert result.status == "failed"
    assert result.last_error == "no json"


def test_run_missing_kit_file_is_failed(tmp_path: Path, monkeypatch) -> None:
    _patch_scripts(
        monkeypatch, gen_result={"status": "ok", "content": _CONTENT}, write_gen=False,
    )
    runtime = InterviewPrepRuntime(_settings(tmp_path))
    result = runtime.run(report_id="r-5", job_snapshot=_JOB, profile_facts=_FACTS)
    assert result.status == "failed"
    assert result.last_error == "missing prep_kit.json"


def test_run_invoke_exception_is_failed(tmp_path: Path, monkeypatch) -> None:
    runtime = InterviewPrepRuntime(_settings(tmp_path))

    def boom(*, skill_dir: Path) -> None:
        raise RuntimeError("generate exploded")

    runtime._invoke = boom  # type: ignore[method-assign]
    result = runtime.run(report_id="r-6", job_snapshot=_JOB, profile_facts=_FACTS)
    assert result.status == "failed"
    assert result.last_error == "generate exploded"


def test_run_artifact_publish_failure_is_needs_manual_review(
    tmp_path: Path, monkeypatch,
) -> None:
    _patch_scripts(
        monkeypatch,
        gen_result={"status": "ok", "content": _CONTENT, "agent_version": "1.0.0"},
    )

    class FailingStore:
        def put(self, **kwargs):
            raise OSError("minio down")

    runtime = InterviewPrepRuntime(_settings(tmp_path), object_store=FailingStore())
    result = runtime.run(report_id="r-7", job_snapshot=_JOB, profile_facts=_FACTS)
    assert result.status == "needs_manual_review"
    assert result.succeeded is False
    assert result.block_reason == "artifact_error"
    assert result.content == _CONTENT  # preserved for the manual reviewer


def test_run_publishes_evidence_when_object_store_present(
    tmp_path: Path, monkeypatch,
) -> None:
    _patch_scripts(
        monkeypatch,
        gen_result={"status": "ok", "content": _CONTENT, "agent_version": "1.0.0"},
    )

    class CollectingStore:
        def __init__(self) -> None:
            self.keys: list[str] = []

        def put(self, *, key: str, plaintext: bytes, content_type: str):
            self.keys.append(key)
            return object()

    store = CollectingStore()
    runtime = InterviewPrepRuntime(_settings(tmp_path), object_store=store)
    result = runtime.run(report_id="r-8", job_snapshot=_JOB, profile_facts=_FACTS)
    assert result.status == "succeeded"
    assert any(key.startswith("interview-prep/r-8/") for key in store.keys)
