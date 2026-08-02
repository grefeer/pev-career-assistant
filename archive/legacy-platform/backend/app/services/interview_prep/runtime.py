"""Deterministic interview-prep runtime.

Reuses the job-discovery skill plumbing (the per-task cloned
``SkillArtifactStore`` and the allowlisted ``run_skill_script`` tool) with its
own orchestrator: write the generation context -> run the ``generate`` LLM script
-> assemble a result.  No deep agent, no browser, no grounding step (the five
content sections are study material, not fact references).  The skill is a
parallel artifact: it does not touch the backend interview-prep store
(``InterviewPrepService``) and never auto-submits anything (security gate #1) -
it is read-only study material.  A generation failure surfaces as ``failed``;
an artifact-publish failure after a successful generation surfaces as
``needs_manual_review`` with the content preserved for the reviewer.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from backend.app.config import Settings
from backend.app.services.interview_prep.schemas import InterviewPrepResult
from backend.app.services.job_discovery.skill_artifacts import SkillArtifactStore
from backend.app.services.job_discovery.skill_runtime import (
    SkillToolPolicy,
    _read_json,
    _script_tool,
)
from backend.app.services.job_discovery.skill_spec import get_skill_spec

#: LLM generation can be slower than a page fetch; give it a generous but
#: bounded window so a hung model cannot stall the worker indefinitely.
_DEFAULT_SCRIPT_TIMEOUT_SECONDS = 300

#: Where the generate script writes its product (under ``output/evidence/`` so
#: ``SkillArtifactStore.iter_evidence`` publishes it as auditable evidence).
_KIT_PATH = Path("output") / "evidence" / "prep_kit.json"


class InterviewPrepRuntime:
    """Run one interview-prep task in an isolated bundled Skill clone.

    The only executable capability is the allowlisted ``generate`` script; all
    result assembly is deterministic Python over the JSON it writes.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        artifact_root: Path | None = None,
        object_store: Any | None = None,
    ) -> None:
        self.settings = settings
        self.spec = get_skill_spec("interview-prep")
        configured_root = getattr(
            settings, "interview_prep_artifact_root", "var/interview-prep-skill"
        )
        self.artifact_root = artifact_root or Path(configured_root)
        self.object_store = object_store

    def run(
        self,
        *,
        report_id: str,
        job_snapshot: dict[str, Any],
        profile_facts: dict[str, Any] | None = None,
        preferences: dict[str, Any] | None = None,
        match_analysis: dict[str, Any] | None = None,
    ) -> InterviewPrepResult:
        store = SkillArtifactStore(
            report_id,
            self.artifact_root,
            run_id=uuid4().hex,
            skill_name=self.spec.name,
            skill_source=self.spec.source_path,
        )
        skill_dir = store.prepare()
        self._write_inputs(
            skill_dir,
            job_snapshot=job_snapshot,
            profile_facts=profile_facts,
            preferences=preferences,
            match_analysis=match_analysis,
        )
        try:
            self._invoke(skill_dir=skill_dir)
        except Exception as exc:
            # Agent-infrastructure failures are not prep content.
            return InterviewPrepResult(
                status="failed",
                summary=f"interview-prep runtime failed: {type(exc).__name__}",
                last_error=str(exc)[:500],
            )
        result = self._result_from_artifacts(skill_dir=skill_dir)
        if self.object_store is None:
            return result
        try:
            store.publish_evidence(self.object_store)
        except Exception:
            return InterviewPrepResult(
                status="needs_manual_review",
                block_reason="artifact_error",
                summary="interview-prep artifacts could not be retained in encrypted object storage",
                content=result.content,
                evidence_refs=result.evidence_refs,
                agent_version=result.agent_version,
            )
        return result

    # ------------------------------------------------------------------ inputs

    def _write_inputs(
        self,
        skill_dir: Path,
        *,
        job_snapshot: dict[str, Any],
        profile_facts: dict[str, Any] | None,
        preferences: dict[str, Any] | None,
        match_analysis: dict[str, Any] | None,
    ) -> None:
        """Write the generate input into the clone."""
        out_root = skill_dir / "output"
        out_root.mkdir(parents=True, exist_ok=True)
        generation_input = {
            "job_snapshot": job_snapshot,
            "profile_facts": profile_facts or {},
            "preferences": preferences or {},
            "match_analysis": match_analysis or {},
        }
        (out_root / "input.json").write_text(
            json.dumps(generation_input, ensure_ascii=False), encoding="utf-8"
        )

    # ------------------------------------------------------------------ invoke

    def _invoke(self, *, skill_dir: Path) -> None:
        """Run generate through the allowlisted script."""
        timeout = getattr(
            self.settings,
            "interview_prep_script_timeout_seconds",
            _DEFAULT_SCRIPT_TIMEOUT_SECONDS,
        )
        tool = _script_tool(
            skill_dir,
            SkillToolPolicy(script_timeout_seconds=timeout),
            allowed_scripts=self.spec.allowed_scripts,
        )
        tool.invoke(
            {
                "script": "generate",
                "cli_args": "--input output/input.json --out output/evidence/prep_kit.json",
            }
        )

    # ------------------------------------------------------------- result build

    def _result_from_artifacts(self, *, skill_dir: Path) -> InterviewPrepResult:
        gen = _read_json(skill_dir / _KIT_PATH)
        if not gen:
            return InterviewPrepResult(
                status="failed",
                summary="interview-prep generate produced no output",
                last_error="missing prep_kit.json",
                evidence_refs=_evidence_refs(skill_dir),
            )
        gen_status = str(gen.get("status") or "")
        agent_version = gen.get("agent_version")
        evidence = _evidence_refs(skill_dir)

        if gen_status != "ok":
            code = gen.get("code")
            return InterviewPrepResult(
                status="failed",
                summary=f"interview-prep generation failed: {code}",
                last_error=str(gen.get("last_error"))[:500]
                if gen.get("last_error")
                else None,
                agent_version=agent_version,
                evidence_refs=evidence,
            )

        content = gen.get("content") or {}
        section_count = sum(len(v) for v in content.values() if isinstance(v, list))
        return InterviewPrepResult(
            status="succeeded",
            summary=f"generated interview-prep kit with {section_count} item(s)",
            content=content,
            evidence_refs=evidence,
            agent_version=agent_version,
        )


def _evidence_refs(skill_dir: Path) -> list[dict[str, Any]]:
    """Audit metadata for the generated kit (the skill's auditable product)."""
    path = skill_dir / _KIT_PATH
    if not path.is_file():
        return []
    return [
        {
            "evidence_type": "interview_prep_kit",
            "content_hash": hashlib.sha256(path.read_bytes()).hexdigest(),
            "relative_path": _KIT_PATH.as_posix(),
        }
    ]
