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


def test_artifacts_are_isolated_by_run_for_the_same_task(tmp_path: Path) -> None:
    first = SkillArtifactStore("task-a", tmp_path, run_id="attempt-1").prepare()
    (first / "output").mkdir()
    (first / "output" / "stale.json").write_text("stale", encoding="utf-8")

    second = SkillArtifactStore("task-a", tmp_path, run_id="attempt-2").prepare()

    assert second != first
    assert not (second / "output" / "stale.json").exists()


def test_evidence_can_be_published_to_an_injected_object_store(tmp_path: Path) -> None:
    class FakeObjectStore:
        def __init__(self) -> None:
            self.keys: list[str] = []

        def put(self, *, key: str, plaintext: bytes, content_type: str):
            self.keys.append(key)
            return object()

    store = SkillArtifactStore("task-a", tmp_path, run_id="attempt-1")
    skill_dir = store.prepare()
    evidence = skill_dir / "output" / "evidence"
    evidence.mkdir(parents=True)
    page = evidence / "page_001.txt"
    page.write_text("JD", encoding="utf-8")

    published = store.publish_evidence(FakeObjectStore())

    assert published[page].startswith("object://job-discovery/task-a/attempt-1/")
