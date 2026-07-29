"""Task-isolated artifacts for the job-discovery Skill runtime.

The repository copy of ``skill/job-discovery`` is immutable input.  Every
discovery task gets a private clone so evidence, screenshots and tool traces
cannot be overwritten by concurrent workers or accidentally attached to a
different task.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil


SKILL_SOURCE = Path(__file__).resolve().parents[4] / "skill" / "job-discovery"


@dataclass(frozen=True)
class SkillArtifact:
    """One auditable Skill output file."""

    path: Path
    relative_path: str
    evidence_type: str


class SkillArtifactStore:
    """Creates and enumerates a task's private Skill working directory."""

    def __init__(self, task_id: str, root: Path, *, skill_source: Path = SKILL_SOURCE) -> None:
        if not task_id or task_id in {".", ".."} or any(char in task_id for char in "\\/"):
            raise ValueError("task_id must be a single non-empty path segment")
        self.task_id = task_id
        self.root = root.resolve()
        self.skill_source = skill_source.resolve()
        self.skill_dir = self.root / task_id / "skill" / "job-discovery"

    def prepare(self) -> Path:
        """Clone the bundled Skill excluding prior output and bytecode."""
        if not (self.skill_source / "SKILL.md").is_file():
            raise FileNotFoundError(f"job-discovery Skill not found: {self.skill_source}")
        if not self.skill_dir.exists():
            self.skill_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(
                self.skill_source,
                self.skill_dir,
                ignore=shutil.ignore_patterns("output", "__pycache__", "*.pyc"),
            )
        return self.skill_dir

    def artifact_uri(self, path: Path) -> str:
        """Return a stable local URI only for files owned by this task."""
        resolved = path.resolve()
        try:
            resolved.relative_to(self.skill_dir.resolve())
        except ValueError as exc:
            raise ValueError("artifact path is outside this task's Skill directory") from exc
        return resolved.as_uri()

    def iter_evidence(self) -> list[SkillArtifact]:
        """Enumerate auditable output files, never inputs or source code."""
        output = self.skill_dir / "output" / "evidence"
        if not output.is_dir():
            return []
        artifacts: list[SkillArtifact] = []
        for path in sorted(candidate for candidate in output.rglob("*") if candidate.is_file()):
            relative_path = path.relative_to(self.skill_dir).as_posix()
            artifacts.append(
                SkillArtifact(
                    path=path,
                    relative_path=relative_path,
                    evidence_type=_evidence_type(path),
                )
            )
        return artifacts


def _evidence_type(path: Path) -> str:
    if path.name == "tool_trace.jsonl":
        return "skill_tool_trace"
    if path.name in {"browse_metadata.json", "coverage_gate_result.json"}:
        return "skill_runtime_metadata"
    if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
        return "skill_screenshot"
    return "skill_page_text"
