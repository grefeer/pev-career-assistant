from __future__ import annotations

from pathlib import Path

from backend.app.services.job_discovery.skill_artifacts import SkillArtifactStore


def test_artifacts_are_isolated_by_task(tmp_path: Path) -> None:
    first = SkillArtifactStore("task-a", tmp_path).prepare()
    second = SkillArtifactStore("task-b", tmp_path).prepare()

    assert first != second
    assert first.name == "job-discovery"
    assert (first / "SKILL.md").is_file()
    assert not (first / "output").exists()


def test_evidence_only_returns_task_scoped_files(tmp_path: Path) -> None:
    store = SkillArtifactStore("task-a", tmp_path)
    skill_dir = store.prepare()
    evidence = skill_dir / "output" / "evidence"
    evidence.mkdir(parents=True)
    page = evidence / "page_001.txt"
    page.write_text("job evidence", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("must not leak", encoding="utf-8")

    artifacts = store.iter_evidence()

    assert [artifact.path for artifact in artifacts] == [page]
    assert store.artifact_uri(page).startswith("file:")
    try:
        store.artifact_uri(outside)
    except ValueError:
        pass
    else:
        raise AssertionError("outside artifact must be rejected")
