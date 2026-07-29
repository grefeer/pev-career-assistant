"""Bounded ``create_deep_agent + Skill + tool + subagent`` discovery runtime."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import sys
import time
from uuid import uuid4
from typing import Any

from langchain_core.tools import StructuredTool

from backend.app.config import Settings
from backend.app.services.job_discovery.schemas import (
    DiscoveryRunResult,
    DiscoveryTaskInput,
    NormalizedJobCandidate,
    PageEvidence,
)
from backend.app.services.job_discovery.skill_artifacts import SkillArtifactStore


_ALLOWED_SCRIPTS = {
    "browse", "validate", "normalize", "deduplicate", "ocr_image", "state",
    "read_evidence", "write_candidates", "coverage_gate",
}


@dataclass(frozen=True)
class SkillRuntimeResult:
    result: DiscoveryRunResult
    trace_steps: list[dict[str, Any]]
    artifact_root: Path
    coverage_verified: bool


@dataclass
class SkillToolPolicy:
    """Per-run limits for the only executable Agent capability."""

    max_browse_calls: int = 2
    max_coverage_gate_calls: int = 1
    max_pages: int = 20
    max_candidates: int = 10
    browse_calls: int = 0
    coverage_gate_calls: int = 0


class SkillDiscoveryRuntime:
    """Runs one task in an isolated bundled Skill clone.

    The only executable capability exposed to the model is an allowlisted
    helper-script tool. Candidate and evidence persistence are deliberately
    performed by the Worker, from files below this task's artifact root.
    """

    def __init__(
        self, settings: Settings, *, artifact_root: Path | None = None,
        object_store: Any | None = None,
    ) -> None:
        self.settings = settings
        configured_root = getattr(settings, "job_discovery_skill_artifact_root", "var/job-discovery-skill")
        self.artifact_root = artifact_root or Path(configured_root)
        self.object_store = object_store

    def run(self, task: DiscoveryTaskInput, *, task_id: str) -> SkillRuntimeResult:
        store = SkillArtifactStore(task_id, self.artifact_root, run_id=uuid4().hex)
        skill_dir = store.prepare()
        try:
            self._invoke(task=task, skill_dir=skill_dir)
        except Exception as exc:  # agent infrastructure errors are not candidate data
            return SkillRuntimeResult(
                result=DiscoveryRunResult(status="failed", block_reason="skill_runtime_error", summary=f"Skill runtime failed: {type(exc).__name__}"),
                trace_steps=_read_trace(skill_dir), artifact_root=skill_dir, coverage_verified=False,
            )
        outcome = _result_from_artifacts(
            task, store,
            max_candidates=self.settings.job_discovery_max_candidates_per_task,
        )
        if self.object_store is None:
            return outcome
        try:
            published = store.publish_evidence(self.object_store)
        except Exception:
            return SkillRuntimeResult(
                result=DiscoveryRunResult(
                    status="needs_manual_review", block_reason="artifact_upload_failed",
                    evidence=outcome.result.evidence, candidates=outcome.result.candidates,
                    summary="Skill artifacts could not be retained in encrypted object storage",
                ),
                trace_steps=outcome.trace_steps, artifact_root=store.skill_dir,
                coverage_verified=False,
            )
        for evidence in outcome.result.evidence:
            relative = (evidence.metadata or {}).get("relative_path")
            if not isinstance(relative, str):
                continue
            uri = published.get(skill_dir / relative)
            if uri:
                evidence.metadata = {**(evidence.metadata or {}), "storage_uri": uri}
        return outcome

    def _invoke(self, *, task: DiscoveryTaskInput, skill_dir: Path) -> None:
        """Invoke the validated DeepAgent topology for a single URL."""
        from deepagents import create_deep_agent
        from deepagents.backends import FilesystemBackend
        from deepagents.middleware.filesystem import FilesystemPermission
        from langchain_core.messages import HumanMessage
        from backend.app.services.job_discovery.llm_factory import build_job_discovery_llm

        tool = _script_tool(skill_dir, SkillToolPolicy(
            max_pages=self.settings.job_discovery_max_pages_per_task,
            max_candidates=self.settings.job_discovery_max_candidates_per_task,
        ))
        backend = FilesystemBackend(root_dir=str(skill_dir.parent), virtual_mode=True)
        permissions = [
            FilesystemPermission(operations=["read"], paths=["/job-discovery/**"]),
            FilesystemPermission(operations=["write"], paths=["/job-discovery/**"], mode="deny"),
        ]
        extractor = {
            "name": "jd_extractor",
            "description": "Extract one evidence page into its assigned output/candidates/page_NN.json using run_skill_script only.",
            "system_prompt": (
                "You extract JDs from exactly one assigned evidence page. Read it only with "
                "run_skill_script(read_evidence), then write real candidates using "
                "run_skill_script(write_candidates). Require responsibilities or requirements; "
                "do not browse, do not invent data, and finish with compact JSON."
            ),
        }
        agent = create_deep_agent(
            model=build_job_discovery_llm(self.settings), tools=[tool], backend=backend,
            skills=["/"], permissions=permissions, subagents=[extractor], name="skill_job_discovery",
            system_prompt=(
                "Use the loaded job-discovery Skill for exactly one public URL. Browse once, "
                "dispatch jd_extractor per evidence page, deduplicate, then run coverage_gate once. "
                "Do not bypass login/captcha/anti-bot; do not use URL adapters or strategy matching. "
                "Persist all candidates only through the Skill scripts. URL: " + task.source_url
            ),
        )
        agent.invoke({"messages": [HumanMessage(content=f"Discover campus-recruitment JDs from {task.source_url}.")]})


def _script_tool(skill_dir: Path, policy: SkillToolPolicy) -> StructuredTool:
    def run_skill_script(script: str, cli_args: str = "", stdin: str = "") -> str:
        started = time.monotonic()
        if script not in _ALLOWED_SCRIPTS:
            return f"ERROR: unsupported Skill script {script!r}"
        try:
            args = shlex.split(cli_args, posix=(sys.platform != "win32")) if cli_args else []
        except ValueError as exc:
            return f"ERROR: invalid cli_args: {exc}"
        if any(part == ".." or Path(part).is_absolute() for part in args if not part.startswith("http")):
            return "ERROR: Skill paths must be relative and stay under output/"
        if script == "browse":
            if policy.browse_calls >= policy.max_browse_calls:
                return "ERROR: browse call limit reached"
            policy.browse_calls += 1
            args = _bounded_browse_args(args, policy.max_pages)
        if script == "coverage_gate":
            if policy.coverage_gate_calls >= policy.max_coverage_gate_calls:
                return "ERROR: coverage_gate call limit reached"
            policy.coverage_gate_calls += 1
        if not _valid_output_args(script, args):
            return "ERROR: Skill output path is not allowed"
        script_path = skill_dir / "scripts" / f"{script}.py"
        if not script_path.is_file():
            return f"ERROR: Skill script missing: {script}"
        try:
            completed = subprocess.run(
                [sys.executable, str(script_path), *args], cwd=skill_dir, input=stdin or None,
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900,
            )
            output = (completed.stdout or "") + (("\n[stderr]\n" + completed.stderr[-2000:]) if completed.stderr else "")
            if completed.returncode:
                output += f"\n[exit code {completed.returncode}]"
        except subprocess.TimeoutExpired:
            output = f"ERROR: {script} timed out"
        if script == "browse":
            _write_json_from_output(skill_dir / "output" / "evidence" / "browse_metadata.json", output)
        elif script == "coverage_gate":
            _write_json_from_output(skill_dir / "output" / "evidence" / "coverage_gate_result.json", output)
        _append_trace(skill_dir, {"tool": script, "duration_ms": round((time.monotonic() - started) * 1000, 1), "status": "failed" if output.startswith("ERROR:") else "ok"})
        return output or "(tool returned no text)"

    return StructuredTool.from_function(run_skill_script, name="run_skill_script", description="Run an allowlisted job-discovery Skill script.")


def _result_from_artifacts(
    task: DiscoveryTaskInput, store: SkillArtifactStore, *, max_candidates: int,
) -> SkillRuntimeResult:
    skill_dir = store.skill_dir
    candidates = _read_candidates(skill_dir)
    evidence = _read_evidence(store, task.source_url)
    browse = _read_json(skill_dir / "output" / "evidence" / "browse_metadata.json")
    gate = _read_json(skill_dir / "output" / "evidence" / "coverage_gate_result.json")
    terminal = bool((browse or {}).get("terminal_evidence") or (browse or {}).get("terminal_signal"))
    gate_passed = bool((gate or {}).get("passed") or (gate or {}).get("coverage_verified"))
    page_extraction_complete = _all_evidence_pages_extracted(skill_dir)
    coverage_verified = terminal and gate_passed and page_extraction_complete
    if len(candidates) > max_candidates:
        result = DiscoveryRunResult(status="needs_manual_review", block_reason="candidate_limit_exceeded", evidence=evidence, candidates=[], summary="Skill candidate count exceeded configured task limit")
    elif not page_extraction_complete:
        result = DiscoveryRunResult(status="needs_manual_review", block_reason="page_extraction_incomplete", evidence=evidence, candidates=candidates, summary="At least one evidence page has no extractor artifact")
    elif not coverage_verified:
        result = DiscoveryRunResult(status="needs_manual_review", block_reason="coverage_unverified", evidence=evidence, candidates=candidates, summary="Skill artifacts did not prove complete coverage")
    elif candidates:
        result = DiscoveryRunResult(status="succeeded", evidence=evidence, candidates=candidates, summary="Skill discovery completed with verified coverage")
    else:
        result = DiscoveryRunResult(status="partial_success", evidence=evidence, candidates=[], summary="Skill discovery completed but found no publishable JD")
    return SkillRuntimeResult(result=result, trace_steps=_read_trace(skill_dir), artifact_root=skill_dir, coverage_verified=coverage_verified)


def _read_candidates(skill_dir: Path) -> list[NormalizedJobCandidate]:
    raw = _read_json(skill_dir / "output" / "candidates_merged.json")
    if not isinstance(raw, list):
        return []
    values: list[NormalizedJobCandidate] = []
    for item in raw:
        if not isinstance(item, dict) or not (item.get("responsibilities") or item.get("requirements")):
            continue
        values.append(NormalizedJobCandidate(**{key: item[key] for key in NormalizedJobCandidate.__dataclass_fields__ if key in item}))
    return values


def _all_evidence_pages_extracted(skill_dir: Path) -> bool:
    pages = sorted((skill_dir / "output" / "evidence" / "pages").glob("page_*.txt"))
    if not pages:
        # The coverage gate reports this separately as no_page_evidence.
        return True
    candidates_dir = skill_dir / "output" / "candidates"
    return all((candidates_dir / f"{page.stem}.json").is_file() for page in pages)


def _read_evidence(store: SkillArtifactStore, url: str) -> list[PageEvidence]:
    evidence: list[PageEvidence] = []
    for artifact in store.iter_evidence():
        try:
            text = artifact.path.read_text(encoding="utf-8", errors="replace") if artifact.path.suffix.lower() in {".txt", ".json", ".jsonl"} else ""
        except OSError:
            continue
        evidence.append(PageEvidence(evidence_type=artifact.evidence_type, url=url, content_hash=hashlib.sha256(artifact.path.read_bytes()).hexdigest(), text_excerpt=text[:4000] or None, metadata={"storage_uri": store.artifact_uri(artifact.path), "relative_path": artifact.relative_path}))
    return evidence


def _read_trace(skill_dir: Path) -> list[dict[str, Any]]:
    path = skill_dir / "output" / "evidence" / "tool_trace.jsonl"
    if not path.is_file():
        return []
    items: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(raw, dict):
            items.append({"tool": raw.get("tool", raw.get("script", "skill")), "status": raw.get("status", "ok"), "duration_ms": raw.get("duration_ms", 0), "params": {}})
    return items


def _append_trace(skill_dir: Path, entry: dict[str, Any]) -> None:
    path = skill_dir / "output" / "evidence" / "tool_trace.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_json_from_output(path: Path, output: str) -> None:
    """Persist the last structured helper response without model narration."""
    decoder = json.JSONDecoder()
    last: dict[str, Any] | None = None
    for index, char in enumerate(output):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(output[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            last = value
    if last is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(last, ensure_ascii=False), encoding="utf-8")


def _bounded_browse_args(args: list[str], max_pages: int) -> list[str]:
    """Clamp model-controlled page count to the task configuration."""
    bounded = list(args)
    if "--max-pages" in bounded:
        index = bounded.index("--max-pages")
        if index + 1 < len(bounded):
            try:
                bounded[index + 1] = str(min(max_pages, max(1, int(bounded[index + 1]))))
            except ValueError:
                bounded[index + 1] = str(max_pages)
    else:
        bounded.extend(["--max-pages", str(max_pages)])
    return bounded


def _valid_output_args(script: str, args: list[str]) -> bool:
    if "--out" not in args:
        return True
    index = args.index("--out")
    if index + 1 >= len(args):
        return False
    output = args[index + 1].replace("\\", "/")
    if script == "browse":
        return output == "output/evidence"
    if script == "deduplicate":
        return output == "output/candidates_merged.json"
    return output.startswith("output/")
