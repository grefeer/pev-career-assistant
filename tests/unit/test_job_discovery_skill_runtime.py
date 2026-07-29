from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from backend.app.services.job_discovery.schemas import DiscoveryTaskInput
from backend.app.services.job_discovery.skill_runtime import SkillDiscoveryRuntime


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
