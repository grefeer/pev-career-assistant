from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from backend.app.services.job_discovery.schemas import DiscoveryTaskInput
from backend.app.services.job_discovery.skill_runtime import (
    SkillDiscoveryRuntime,
    SkillToolPolicy,
    _script_tool,
)


def _task() -> DiscoveryTaskInput:
    return DiscoveryTaskInput(
        source_id="source", raw_record_id="raw", external_record_id="external",
        source_key="manual", source_url="https://example.com/jobs", url_hash="hash",
        record_fields=[],
    )


def test_runtime_maps_verified_artifacts_to_result(tmp_path: Path) -> None:
    settings = SimpleNamespace(job_discovery_skill_artifact_root=str(tmp_path), job_discovery_max_candidates_per_task=10)
    runtime = SkillDiscoveryRuntime(settings)

    def fake_invoke(*, task: DiscoveryTaskInput, skill_dir: Path) -> None:
        evidence_dir = skill_dir / "output" / "evidence"
        evidence_dir.mkdir(parents=True)
        (evidence_dir / "pages").mkdir()
        (evidence_dir / "pages" / "page_001.txt").write_text("JD body", encoding="utf-8")
        (evidence_dir / "tool_trace.jsonl").write_text(
            json.dumps({"script": "browse", "duration_ms": 12, "status": "ok"}) + "\n",
            encoding="utf-8",
        )
        (evidence_dir / "browse_metadata.json").write_text(
            json.dumps({"terminal_evidence": "last_page_disabled"}), encoding="utf-8",
        )
        (evidence_dir / "coverage_gate_result.json").write_text(
            json.dumps({"coverage_verified": True}), encoding="utf-8",
        )
        candidates_dir = skill_dir / "output" / "candidates"
        candidates_dir.mkdir()
        (candidates_dir / "page_001.json").write_text("[]", encoding="utf-8")
        (skill_dir / "output" / "candidates_merged.json").write_text(json.dumps([{
            "title": "工程师", "company_name": "示例", "responsibilities": "负责开发",
            "requirements": "熟悉 Python", "locations": ["上海"],
            "evidence_refs": [{"evidence_type": "browsed_detail_page"}],
        }]), encoding="utf-8")

    runtime._invoke = fake_invoke  # type: ignore[method-assign]
    outcome = runtime.run(_task(), task_id="task-1")

    assert outcome.result.status == "succeeded"
    assert outcome.result.candidates[0].responsibilities == "负责开发"
    assert outcome.trace_steps[0]["tool"] == "browse"
    assert outcome.result.evidence[0].metadata["storage_uri"].startswith("file:")


def test_runtime_requires_positive_coverage_marker(tmp_path: Path) -> None:
    settings = SimpleNamespace(job_discovery_skill_artifact_root=str(tmp_path), job_discovery_max_candidates_per_task=10)
    runtime = SkillDiscoveryRuntime(settings)

    def fake_invoke(*, task: DiscoveryTaskInput, skill_dir: Path) -> None:
        output = skill_dir / "output"
        output.mkdir()
        (output / "candidates_merged.json").write_text("[]", encoding="utf-8")

    runtime._invoke = fake_invoke  # type: ignore[method-assign]
    outcome = runtime.run(_task(), task_id="task-2")

    assert outcome.result.status == "needs_manual_review"
    assert outcome.result.block_reason == "coverage_unverified"


def test_tool_policy_rejects_excess_browse_and_external_output(
    tmp_path: Path, monkeypatch,
) -> None:
    skill_dir = tmp_path / "skill"
    scripts = skill_dir / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "browse.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(
        "backend.app.services.job_discovery.skill_runtime.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(stdout="{}", stderr="", returncode=0),
    )
    tool = _script_tool(skill_dir, SkillToolPolicy(max_browse_calls=2))

    assert not tool.invoke({"script": "browse", "cli_args": "https://example.com --out output/evidence"}).startswith("ERROR:")
    assert not tool.invoke({"script": "browse", "cli_args": "https://example.com --out output/evidence"}).startswith("ERROR:")
    assert tool.invoke({"script": "browse", "cli_args": "https://example.com --out output/evidence"}).startswith("ERROR:")
    assert tool.invoke({"script": "browse", "cli_args": "https://example.com --out C:/outside"}).startswith("ERROR:")


def test_runtime_honors_configured_candidate_limit(tmp_path: Path) -> None:
    settings = SimpleNamespace(job_discovery_skill_artifact_root=str(tmp_path), job_discovery_max_candidates_per_task=1)
    runtime = SkillDiscoveryRuntime(settings)

    def fake_invoke(*, task: DiscoveryTaskInput, skill_dir: Path) -> None:
        evidence = skill_dir / "output" / "evidence"
        evidence.mkdir(parents=True)
        (evidence / "browse_metadata.json").write_text(json.dumps({"terminal_evidence": "end"}), encoding="utf-8")
        (evidence / "coverage_gate_result.json").write_text(json.dumps({"coverage_verified": True}), encoding="utf-8")
        (skill_dir / "output" / "candidates_merged.json").write_text(json.dumps([
            {"title": "A", "responsibilities": "body"}, {"title": "B", "responsibilities": "body"},
        ]), encoding="utf-8")

    runtime._invoke = fake_invoke  # type: ignore[method-assign]
    outcome = runtime.run(_task(), task_id="task-limit")

    assert outcome.result.status == "needs_manual_review"
    assert outcome.result.block_reason == "candidate_limit_exceeded"


def test_llm_factory_is_available_without_importing_supervisor() -> None:
    from backend.app.services.job_discovery.llm_factory import build_job_discovery_llm

    assert callable(build_job_discovery_llm)


def test_runtime_rejects_coverage_when_an_evidence_page_has_no_extraction(tmp_path: Path) -> None:
    settings = SimpleNamespace(job_discovery_skill_artifact_root=str(tmp_path), job_discovery_max_candidates_per_task=10)
    runtime = SkillDiscoveryRuntime(settings)

    def fake_invoke(*, task: DiscoveryTaskInput, skill_dir: Path) -> None:
        evidence = skill_dir / "output" / "evidence" / "pages"
        evidence.mkdir(parents=True)
        (evidence / "page_001.txt").write_text("first", encoding="utf-8")
        (evidence / "page_002.txt").write_text("second", encoding="utf-8")
        meta = evidence.parent
        (meta / "browse_metadata.json").write_text(json.dumps({"terminal_evidence": "end"}), encoding="utf-8")
        (meta / "coverage_gate_result.json").write_text(json.dumps({"coverage_verified": True}), encoding="utf-8")
        candidates = skill_dir / "output" / "candidates"
        candidates.mkdir()
        (candidates / "page_001.json").write_text(json.dumps([{"title": "A", "responsibilities": "body"}]), encoding="utf-8")
        (skill_dir / "output" / "candidates_merged.json").write_text(json.dumps([{"title": "A", "responsibilities": "body"}]), encoding="utf-8")

    runtime._invoke = fake_invoke  # type: ignore[method-assign]
    outcome = runtime.run(_task(), task_id="task-pages")

    assert outcome.result.status == "needs_manual_review"
    assert outcome.result.block_reason == "page_extraction_incomplete"


def test_runtime_marks_manual_review_when_artifact_upload_fails(tmp_path: Path) -> None:
    class BrokenObjectStore:
        def put(self, **kwargs) -> None:
            raise OSError("MinIO unavailable")

    settings = SimpleNamespace(job_discovery_skill_artifact_root=str(tmp_path), job_discovery_max_candidates_per_task=10)
    runtime = SkillDiscoveryRuntime(settings, object_store=BrokenObjectStore())

    def fake_invoke(*, task: DiscoveryTaskInput, skill_dir: Path) -> None:
        evidence = skill_dir / "output" / "evidence" / "pages"
        evidence.mkdir(parents=True)
        (evidence / "page_001.txt").write_text("body", encoding="utf-8")
        meta = evidence.parent
        (meta / "browse_metadata.json").write_text(json.dumps({"terminal_evidence": "end"}), encoding="utf-8")
        (meta / "coverage_gate_result.json").write_text(json.dumps({"coverage_verified": True}), encoding="utf-8")
        candidates = skill_dir / "output" / "candidates"
        candidates.mkdir()
        (candidates / "page_001.json").write_text("[]", encoding="utf-8")
        (skill_dir / "output" / "candidates_merged.json").write_text(json.dumps([{"title": "A", "responsibilities": "body"}]), encoding="utf-8")

    runtime._invoke = fake_invoke  # type: ignore[method-assign]
    outcome = runtime.run(_task(), task_id="task-object-store")

    assert outcome.result.status == "needs_manual_review"
    assert outcome.result.block_reason == "artifact_upload_failed"
