"""Deterministic resume-tailoring runtime.

Reuses the job-discovery skill plumbing (the per-task cloned
``SkillArtifactStore`` and the allowlisted ``run_skill_script`` tool) with its
own orchestrator: write the generation context -> run the ``generate`` LLM script
-> (optionally) run the ``validate`` grounding script -> assemble a result.  No
deep agent, no browser, no coverage gate.  The skill is a parallel artifact: it
does not touch the backend resume store (``ResumeDraftService``) and never
auto-applies diffs - the human always applies the final resume (security gate
#1).  A generation failure surfaces as ``failed``; diffs that fail grounding
validation surface as ``needs_manual_review`` so the human can fix them in place.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from backend.app.config import Settings
from backend.app.services.job_discovery.skill_artifacts import SkillArtifactStore
from backend.app.services.job_discovery.skill_runtime import (
    SkillToolPolicy,
    _read_json,
    _script_tool,
)
from backend.app.services.job_discovery.skill_spec import get_skill_spec
from backend.app.services.resume_tailoring.schemas import ResumeTailoringResult

#: LLM generation can be slower than a page fetch; give it a generous but
#: bounded window so a hung model cannot stall the worker indefinitely.
_DEFAULT_SCRIPT_TIMEOUT_SECONDS = 300

#: Where the generate script writes its product (under ``output/evidence/`` so
#: ``SkillArtifactStore.iter_evidence`` publishes it as auditable evidence).
_DRAFT_PATH = Path("output") / "evidence" / "draft_diffs.json"


class ResumeTailoringRuntime:
    """Run one resume-tailoring task in an isolated bundled Skill clone.

    The only executable capabilities are the allowlisted ``generate`` and
    ``validate`` scripts; all result assembly is deterministic Python over the
    JSON they write.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        artifact_root: Path | None = None,
        object_store: Any | None = None,
    ) -> None:
        self.settings = settings
        self.spec = get_skill_spec("resume-tailoring")
        configured_root = getattr(
            settings, "resume_tailoring_artifact_root", "var/resume-tailoring-skill"
        )
        self.artifact_root = artifact_root or Path(configured_root)
        self.object_store = object_store

    def run(
        self,
        *,
        report_id: str,
        job_snapshot: dict[str, Any],
        profile_facts: dict[str, Any],
        preferences: dict[str, Any] | None = None,
        match_analysis: dict[str, Any] | None = None,
        evidence_refs: dict[str, Any] | None = None,
        validate: bool = True,
    ) -> ResumeTailoringResult:
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
            evidence_refs=evidence_refs,
        )
        try:
            self._invoke(skill_dir=skill_dir, validate=validate)
        except Exception as exc:
            # Agent-infrastructure failures are not resume data.
            return ResumeTailoringResult(
                status="failed",
                summary=f"resume-tailoring runtime failed: {type(exc).__name__}",
                last_error=str(exc)[:500],
            )
        result = self._result_from_artifacts(
            skill_dir=skill_dir, validate_ran=validate
        )
        if self.object_store is None:
            return result
        try:
            store.publish_evidence(self.object_store)
        except Exception:
            return ResumeTailoringResult(
                status="needs_manual_review",
                block_reason="artifact_error",
                summary="resume-tailoring artifacts could not be retained in encrypted object storage",
                diffs=result.diffs,
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
        profile_facts: dict[str, Any],
        preferences: dict[str, Any] | None,
        match_analysis: dict[str, Any] | None,
        evidence_refs: dict[str, Any] | None,
    ) -> None:
        """Write the generate input + validate facts/evidence into the clone."""
        out_root = skill_dir / "output"
        out_root.mkdir(parents=True, exist_ok=True)
        generation_input = {
            "job_snapshot": job_snapshot,
            "profile_facts": profile_facts,
            "preferences": preferences or {},
            "match_analysis": match_analysis or {},
        }
        (out_root / "input.json").write_text(
            json.dumps(generation_input, ensure_ascii=False), encoding="utf-8"
        )
        # validate.py needs facts + evidence as separate JSON objects.
        (out_root / "profile_facts.json").write_text(
            json.dumps(profile_facts, ensure_ascii=False), encoding="utf-8"
        )
        (out_root / "evidence_refs.json").write_text(
            json.dumps(evidence_refs or {}, ensure_ascii=False), encoding="utf-8"
        )

    # ------------------------------------------------------------------ invoke

    def _invoke(self, *, skill_dir: Path, validate: bool) -> None:
        """Run generate (and optionally validate) through allowlisted scripts."""
        timeout = getattr(
            self.settings,
            "resume_tailoring_script_timeout_seconds",
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
                "cli_args": "--input output/input.json --out output/evidence/draft_diffs.json",
            }
        )
        if validate:
            tool.invoke(
                {
                    "script": "validate",
                    "cli_args": (
                        "--input output/evidence/draft_diffs.json "
                        "--facts output/profile_facts.json "
                        "--evidence output/evidence_refs.json "
                        "--out output/validation.json"
                    ),
                }
            )

    # ------------------------------------------------------------- result build

    def _result_from_artifacts(
        self, *, skill_dir: Path, validate_ran: bool
    ) -> ResumeTailoringResult:
        gen = _read_json(skill_dir / _DRAFT_PATH)
        if not gen:
            return ResumeTailoringResult(
                status="failed",
                summary="resume-tailoring generate produced no output",
                last_error="missing draft_diffs.json",
                evidence_refs=_evidence_refs(skill_dir),
            )
        gen_status = str(gen.get("status") or "")
        agent_version = gen.get("agent_version")
        evidence = _evidence_refs(skill_dir)

        if gen_status != "ok":
            code = gen.get("code")
            return ResumeTailoringResult(
                status="failed",
                summary=f"resume-tailoring generation failed: {code}",
                last_error=str(gen.get("last_error"))[:500]
                if gen.get("last_error")
                else None,
                agent_version=agent_version,
                evidence_refs=evidence,
            )

        diffs = gen.get("diffs") or []
        if validate_ran:
            val = _read_json(skill_dir / "output" / "validation.json") or {}
            if str(val.get("status") or "") != "ok":
                return ResumeTailoringResult(
                    status="needs_manual_review",
                    block_reason="invalid_diff",
                    summary="generated diffs failed grounding validation",
                    diffs=diffs,
                    evidence_refs=evidence,
                    validation_code=val.get("code"),
                    validation_index=val.get("index"),
                    agent_version=agent_version,
                )

        return ResumeTailoringResult(
            status="succeeded",
            summary=f"generated {len(diffs)} diff operation(s)",
            diffs=diffs,
            evidence_refs=evidence,
            agent_version=agent_version,
        )


def _evidence_refs(skill_dir: Path) -> list[dict[str, Any]]:
    """Audit metadata for the generated draft (the skill's auditable product)."""
    path = skill_dir / _DRAFT_PATH
    if not path.is_file():
        return []
    return [
        {
            "evidence_type": "resume_draft_diffs",
            "content_hash": hashlib.sha256(path.read_bytes()).hexdigest(),
            "relative_path": _DRAFT_PATH.as_posix(),
        }
    ]
